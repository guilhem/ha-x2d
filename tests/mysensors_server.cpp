// Host MySensors serial gateway for the Python/Home Assistant PTY tests: the
// dongle's MySensors adapter (x2d::mysensors::Gateway) over simulated flash and radio,
// with stdin/stdout as the byte transport. It parses no MySensors line itself;
// the Python side only forwards bytes and observes RF through the report below.
//
//   mysensors_server [--control-fd=N] [--burst-ms=N] [--paired=N] [--journal=FILE]
//                    [--device-id=HEX16] [--generation=HEX16] [--identity-base=HEX6]
//                    [--seed=N] [--no-tx] [--enrollment] [--enrollment-suffix=HEX2]
//                    [--fault=none|unavailable|start|poll]
//                    [--ota-file=PATH] [--ota-prefix=PATH]
//                    [--ota-active=PATH] [--ota-reboot]
//                    [--prepare-legacy-reset]
//
//   --paired=N     slots 1..N confirmed on first use of EMPTY storage (default 16,
//                  0 = leave the journal empty). A populated journal is never
//                  reprovisioned.
//   --journal=FILE flash image kept across server restarts; a missing file is a
//                  new (empty) journal. Without it the flash dies with the process.
//   --identity-base=HEX6  radio identity of slot N is BASE | N<<8 | N (default A00000);
//                  slot N's first counter is 100*N.
//   --burst-ms=N   duration of a full 25-copy burst (default 80).
//   --fault=...    radio faults: unavailable (available() is false), start
//                  (start_burst refuses), poll (the burst reports unknown).
//   --enrollment-suffix  EnrollmentProfile identity suffix (default 01).
//                  This simulated fixture does not qualify a motor profile.
//   --ota-file     enable TEST-ONLY OTA storage; save the 16-byte-padded image
//                  here and acknowledge staging without rebooting the simulator.
//   --ota-prefix   reference image/prefix: read its first 0x3000 bytes BEFORE
//                  receiving OTA. Without it, byte i is (37*i + 11) & 255,
//                  matching check_ota's synthetic image. Requires --ota-file.
//   --prepare-legacy-reset  TEST-ONLY: commit the initial reset marker in an
//                  existing v1 file and exit before maintenance, simulating a
//                  power cut before legacy sectors are erased. Emits no RF.
//
// stdin/stdout carry serial bytes only. EOF on stdin exits after the output is
// flushed. --control-fd is an inherited bidirectional stream socket; without it
// the link is connected at start and can only end with EOF.
//   bridge -> server  'C'  host opened the port: gateway->connected(); echoed.
//                     'D'  link lost: gateway->disconnected(), buffered and unread
//                          input discarded, then echoed. Every stdout byte written
//                          earlier is already in the pipe, so the bridge drains
//                          up to the echo and may then reconnect.
//                     'H'/'R' hold/release the radio (burst progress freezes);
//                          echoed.
//   server -> bridge  'B'  simulated USB reset; reconnect with 'C' before any
//                          fresh active config/application evidence.
//                     'F'  gateway failed (output overflow); sent once per link.
//
// RF report on stderr, one line per event. A decoded burst names the journal
// identity, action byte and counter the radio would have sent:
//   x2d-sim: ready paired=16 generation=0123456789ABCDEF
//   x2d-sim: burst start identity=A00301 action=81 counter=100 copies=25
//   x2d-sim: burst stop requested
//   x2d-sim: burst end completed=25 stopped=0
// The simulated clock is monotonic real time shared by adapter and radio.
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <chrono>
#include <optional>
#include <string>
#include <vector>

#include "mysensors.h"
#include "image_layout.h"

using namespace x2d;

namespace {

void report(const char* format, ...) __attribute__((format(printf, 1, 2)));
void report(const char* format, ...) {
  char line[256];
  va_list args;
  va_start(args, format);
  vsnprintf(line, sizeof(line), format, args);
  va_end(args);
  fprintf(stderr, "x2d-sim: %s\n", line);
  fflush(stderr);
}

// Flash that survives restarts: the image is loaded once and every successful
// erase/program is written through. NOR rules as MemoryFlash: programming only
// erased pages.
class FileFlash final : public journal::Flash {
 public:
  explicit FileFlash(const std::string& path) {
    memset(data_, 0xFF, sizeof(data_));
    if (path.empty()) return;
    fd_ = open(path.c_str(), O_RDWR | O_CREAT, 0644);
    if (fd_ < 0) {
      fprintf(stderr, "cannot open the journal file %s\n", path.c_str());
      exit(1);
    }
    const ssize_t got = pread(fd_, data_, sizeof(data_), 0);
    if (got == 0) {  // new file only; an existing truncated journal is not empty
      if (pwrite(fd_, data_, sizeof(data_), 0) != static_cast<ssize_t>(sizeof(data_))) {
        fprintf(stderr, "cannot initialize the journal file %s\n", path.c_str());
        exit(1);
      }
    } else if (got != static_cast<ssize_t>(sizeof(data_))) {
      fputs("truncated simulated journal; refusing to format it\n", stderr);
      exit(1);
    }
  }
  ~FileFlash() override { if (fd_ >= 0) close(fd_); }
  uint32_t size() const override { return journal::REGION_BYTES; }
  bool read(uint32_t offset, void* out, uint32_t length) override {
    if (offset > journal::REGION_BYTES || length > journal::REGION_BYTES - offset) return false;
    memcpy(out, data_ + offset, length);
    return true;
  }
  bool erase_sector(uint32_t offset) override {
    if (offset % journal::SECTOR_BYTES || offset >= journal::REGION_BYTES) return false;
    memset(data_ + offset, 0xFF, journal::SECTOR_BYTES);
    return write_through(offset, journal::SECTOR_BYTES);
  }
  bool program_page(uint32_t offset, const uint8_t* page) override {
    if (offset % journal::PAGE_BYTES || offset >= journal::REGION_BYTES) return false;
    for (uint32_t i = 0; i < journal::PAGE_BYTES; ++i)
      if (data_[offset + i] != 0xFF) return false;
    memcpy(data_ + offset, page, journal::PAGE_BYTES);
    return write_through(offset, journal::PAGE_BYTES);
  }

 private:
  bool write_through(uint32_t offset, uint32_t length) {
    return fd_ < 0 ||
           pwrite(fd_, data_ + offset, length, offset) == static_cast<ssize_t>(length);
  }
  uint8_t data_[journal::REGION_BYTES];
  int fd_ = -1;
};

// Decodes the first copy of a command burst back to its body (the inverse of
// radio::encode_burst) so the report names what would be on the air.
bool decode_first_copy(const radio::Waveform& wave, radio::ParsedBody& parsed) {
  size_t at = 0;
  bool bit = false;
  auto cell = [&] {
    if (at + 1 >= wave.chips()) return false;
    bit = wave.chip(at) != wave.chip(at + 1);  // biphase mark: a 1 flips mid-bit
    at += 2;
    return true;
  };
  for (int i = 0; i < 8; ++i) if (!cell() || bit) return false;    // preamble zeros
  for (int i = 0; i < 6; ++i) if (!cell() || !bit) return false;   // frame start
  if (!cell() || bit) return false;
  // The second enrollment gesture is 13 bytes; the first changes a fixed
  // header byte. Ordinary-command parse_body deliberately rejects both. Decode
  // the actual stuffed first frame, retaining its observed action and counter.
  uint8_t body[radio::MAX_BODY_BYTES] = {};
  unsigned ones = 0;
  size_t length = 0;
  const size_t body_end = wave.frame_end(0) - 18;  // eight ones and a zero trailer
  while (at < body_end && length < sizeof(body)) {
    const size_t i = length++;
    for (int j = 0; j < 8; ++j) {
      if (!cell()) return false;
      if (bit) body[i] |= static_cast<uint8_t>(1u << j);
      ones = bit ? ones + 1 : 0;
      if (ones == 5) {  // stuffed zero
        if (!cell() || bit) return false;
        ones = 0;
      }
    }
  }
  if (at != body_end || (length != 12 && length != 13)) return false;
  if (length == 12 && radio::parse_body(body, length, &parsed)) return true;
  const bool first = length == 12 && body[4] == 0x85 && body[7] == 0x02;
  const bool second = length == 13 && body[4] == 0x05 && body[7] == 0x20 && body[8] == 0x07;
  if ((!first && !second) || body[3] != 0x01 || body[5] != 0x98 || body[6] != 0x22 ||
      ((body[length - 2] << 8) | body[length - 1]) != radio::body_checksum(body, length)) return false;
  parsed.identity = (uint32_t{body[0]} << 16) | (uint32_t{body[1]} << 8) | body[2];
  parsed.action = body[7];
  const size_t counter_at = second ? 9 : 8;
  parsed.rolling_word = static_cast<uint16_t>(body[counter_at] | (body[counter_at + 1] << 8));
  parsed.counter = radio::rolling_decode(parsed.rolling_word, parsed.identity);
  return true;
}

// Radio backend: a burst advances one whole copy per copy_ms of clock time, and
// not at all while held. request_stop() parks after the next full copy.
class SimRadio {
 public:
  explicit SimRadio(const uint32_t& clock) : now_(clock) {}

  uint32_t copy_ms = 3;
  bool held = false, unavailable = false, fail_start = false, fail_poll = false;

  bool available() const { return !unavailable; }
  bool active() const { return running_; }
  bool start_burst(const radio::Waveform& wave, uint32_t chip_ns, uint32_t& started_ms) {
    if (running_ || !chip_ns || !wave.copies()) abort();
    if (fail_start) return false;
    wave_ = &wave;
    running_ = true;
    stop_ = false;
    elapsed_ = 0;
    last_ = now_;
    started_ms = now_;
    radio::ParsedBody parsed;
    if (decode_first_copy(wave, parsed))
      report("burst start identity=%06X action=%02X counter=%u copies=%u",
             static_cast<unsigned>(parsed.identity), parsed.action, parsed.counter, wave.copies());
    else report("burst start undecoded copies=%u", wave.copies());
    return true;
  }
  radio::FrameState poll_burst(uint8_t& completed) {
    if (!running_) abort();
    advance();
    completed_ = done();
    completed = completed_;
    if (fail_poll) { running_ = false; return radio::FrameState::unknown; }
    if (completed_ < (stop_ ? stop_at_ : wave_->copies())) return radio::FrameState::busy;
    running_ = false;
    return radio::FrameState::complete;
  }
  void request_stop() {
    if (!running_) abort();
    advance();
    if (!stop_) report("burst stop requested");
    stop_ = true;
    stop_at_ = done() < wave_->copies() ? done() + 1 : wave_->copies();
  }
  void end_burst() {
    if (running_) abort();
    report("burst end completed=%u stopped=%d", completed_, stop_ ? 1 : 0);
  }

 private:
  void advance() {
    if (!held) elapsed_ += now_ - last_;
    last_ = now_;
  }
  uint8_t done() const {
    const uint32_t copies = elapsed_ / copy_ms;
    return static_cast<uint8_t>(copies < wave_->copies() ? copies : wave_->copies());
  }
  const uint32_t& now_;
  const radio::Waveform* wave_ = nullptr;
  bool running_ = false, stop_ = false;
  uint32_t elapsed_ = 0, last_ = 0;
  uint8_t completed_ = 0, stop_at_ = 0;
};

// Test fixture only: a host file stands in for board flash and boot commands.
// Verify the real file readback against an independent reference prefix and
// ImageVerifier. --ota-reboot recreates the runtime from active bytes after
// commit; with a control FD it drops the link until the bridge reconnects.
class FileOTAStorage final : public ota::Storage {
 public:
  FileOTAStorage(SimRadio &radio, const std::string &path, const std::string &prefix, const std::string &active)
      : radio_(radio), path_(path), active_path_(active) {
    if (!active.empty()) {
      active_ = read_image(active);
      memcpy(prefix_, active_.data(), sizeof(prefix_));
      return;
    }
    for (size_t i = 0; i < sizeof(prefix_); ++i) prefix_[i] = static_cast<uint8_t>(37 * i + 11);
    if (prefix.empty()) { initialize_active(); return; }
    FILE *source = fopen(prefix.c_str(), "rb");
    if (!source) {
      fprintf(stderr, "cannot open OTA reference prefix %s\n", prefix.c_str());
      exit(1);
    }
    const size_t got = fread(prefix_, 1, sizeof(prefix_), source);
    const bool closed = fclose(source) == 0;
    if (got != sizeof(prefix_) || !closed) {
      fprintf(stderr, "OTA reference prefix must contain at least 0x3000 bytes: %s\n", prefix.c_str());
      exit(1);
    }
    initialize_active();
  }
  ~FileOTAStorage() override { abort(); }
  ota::FirmwareConfig running_config() const override {
    return ota::running_config(active_.data(), active_.size(), active_version_);
  }
  const std::string &sketch() const { return sketch_; }
  void reboot() {
    if (committed_) {
      active_ = read_image(path_);
      if (!active_path_.empty()) {
        FILE *out = fopen(active_path_.c_str(), "wb");
        if (!out || fwrite(active_.data(), 1, active_.size(), out) != active_.size() || fclose(out)) exit(1);
      }
      committed_ = created_ = false;
      expected_ = written_ = 0;
    } else abort();
  }
  bool begin(uint32_t bytes) override {
    if (committed_ || radio_.active() || bytes <= ota::APPLICATION_OFFSET + 8 ||
        bytes > ota::MAX_BLOCKS * ota::BLOCK_BYTES || bytes % ota::BLOCK_BYTES) return false;
    abort();
    file_ = fopen(path_.c_str(), "w+b");
    expected_ = bytes;
    created_ = file_ != nullptr;
    return created_;
  }
  bool write(const uint8_t *block, size_t length) override {
    if (!file_ || radio_.active() || length != ota::BLOCK_BYTES ||
        written_ > expected_ || length > expected_ - written_ ||
        fwrite(block, 1, length, file_) != length) return false;
    written_ += static_cast<uint32_t>(length);
    return true;
  }
  bool finish(uint16_t crc, uint16_t version) override {
    if (!file_ || radio_.active() || written_ != expected_ || fflush(file_) ||
        fsync(fileno(file_)) || fseek(file_, 0, SEEK_SET)) return false;
    ota::ImageVerifier verifier(prefix_, expected_, crc, version);
    uint8_t buffer[256];
    uint32_t offset = 0;
    while (offset < expected_) {
      const size_t length = expected_ - offset < sizeof(buffer) ? expected_ - offset : sizeof(buffer);
      if (fread(buffer, 1, length, file_) != length || !verifier.add(buffer, length)) return false;
      offset += static_cast<uint32_t>(length);
    }
    if (fgetc(file_) != EOF || ferror(file_) || !verifier.finish()) return false;
    const bool closed = fclose(file_) == 0;
    file_ = nullptr;
    if (!closed) return false;
    committed_ = true;
    report("ota staged bytes=%u crc=%04X file=%s", static_cast<unsigned>(expected_),
           static_cast<unsigned>(crc), path_.c_str());
    return true;
  }
  void abort() override {
    if (committed_) return;
    if (file_) { fclose(file_); file_ = nullptr; }
    if (created_) unlink(path_.c_str());
    created_ = false;
    expected_ = written_ = 0;
  }
 private:
  void initialize_active() {
    active_.assign(ota::APPLICATION_OFFSET + 128, 0xFF);
    memcpy(active_.data(), prefix_, sizeof(prefix_));
    uint8_t *app = active_.data() + ota::APPLICATION_OFFSET;
    const uint32_t vectors[] = {0x20042000, 0x10003021};
    memcpy(app, vectors, sizeof(vectors));
    const std::string identity = std::string(ota::IMAGE_MARKER) + std::to_string(ota::VERSION) + ":x2d-usb-sim";
    memcpy(app + 32, identity.c_str(), identity.size() + 1);
  }
  std::vector<uint8_t> read_image(const std::string &path) {
    FILE *file = fopen(path.c_str(), "rb");
    if (!file) exit(1);
    std::vector<uint8_t> bytes;
    uint8_t buffer[256];
    size_t got;
    while ((got = fread(buffer, 1, sizeof(buffer), file))) {
      bytes.insert(bytes.end(), buffer, buffer + got);
      if (bytes.size() > ota::MAX_BLOCKS * ota::BLOCK_BYTES) exit(1);
    }
    if (ferror(file) || fclose(file) || bytes.size() <= ota::APPLICATION_OFFSET + 8) exit(1);
    bytes.resize(ota::padded_image_size(bytes.size()), 0xFF);
    std::string text(reinterpret_cast<const char *>(bytes.data() + ota::APPLICATION_OFFSET),
                     bytes.size() - ota::APPLICATION_OFFSET);
    bool identified = false;
    size_t marker = 0;
    while ((marker = text.find(ota::IMAGE_MARKER, marker)) != std::string::npos) {
      marker += sizeof(ota::IMAGE_MARKER) - 1;
      size_t end = marker;
      uint32_t version = 0;
      while (end < text.size() && end - marker < 5 && text[end] >= '0' && text[end] <= '9')
        version = version * 10 + text[end++] - '0';
      // Actual firmware also contains the verifier's bare marker string.
      if (end == marker || end == text.size() || text[end] != ':' || version > 65535) continue;
      if (identified) exit(1);
      identified = true;
      active_version_ = static_cast<uint16_t>(version);
      const size_t stop = text.find('\0', end + 1);
      if (stop != std::string::npos && stop - end - 1 <= mysensors::PAYLOAD_BYTES)
        sketch_ = text.substr(end + 1, stop - end - 1);
    }
    const auto config = ota::running_config(bytes.data(), bytes.size(), active_version_);
    if (!identified || !config.blocks) exit(1);
    ota::ImageVerifier verifier(bytes.data(), bytes.size(), config.crc, active_version_);
    if (!verifier.add(bytes.data(), bytes.size()) || !verifier.finish()) exit(1);
    return bytes;
  }
  SimRadio &radio_;
  std::string path_, active_path_;
  std::vector<uint8_t> active_;
  std::string sketch_ = "x2d-usb-sim";
  uint16_t active_version_ = ota::VERSION;
  uint8_t prefix_[ota::APPLICATION_OFFSET]{};
  FILE *file_ = nullptr;
  uint32_t expected_ = 0, written_ = 0;
  bool created_ = false, committed_ = false;
};

// Firmware policy: deterministic entropy, so a run is reproducible.
struct Policy {
  uint32_t state = 1;
  std::string sketch = "x2d-usb-sim";
  uint32_t random_u32() {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    return state;
  }
  const char* firmware() { return sketch.c_str(); }
};

struct Options {
  int control = -1;
  uint32_t burst_ms = 80, paired = journal::SLOTS, seed = 1;
  uint64_t generation = 0x0123456789ABCDEFull;
  uint32_t identity_base = 0xA00000;
  std::string device_id = "0123456789ABCDEF", journal_file, ota_file, ota_prefix, ota_active;
  bool tx = true, enrollment = false, unavailable = false, fail_start = false, fail_poll = false,
       prepare_legacy_reset = false, ota_reboot = false;
  EnrollmentProfile profile{};
};

bool number(const std::string& text, uint64_t maximum, int base, uint64_t& out) {
  char* end = nullptr;
  errno = 0;
  const unsigned long long value = strtoull(text.c_str(), &end, base);
  if (text.empty() || *end || errno || value > maximum) return false;
  out = value;
  return true;
}

bool parse(int argc, char** argv, Options& options) {
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    const size_t eq = arg.find('=');
    const std::string name = arg.substr(0, eq);
    const bool has_value = eq != std::string::npos;
    const std::string value = has_value ? arg.substr(eq + 1) : "";
    uint64_t n = 0;
    if (name == "--no-tx" && !has_value) options.tx = false;
    else if (name == "--enrollment" && !has_value) options.enrollment = true;
    else if (name == "--prepare-legacy-reset" && !has_value) options.prepare_legacy_reset = true;
    else if (name == "--control-fd" && number(value, 1023, 10, n)) options.control = static_cast<int>(n);
    else if (name == "--burst-ms" && number(value, 600000, 10, n) && n) options.burst_ms = static_cast<uint32_t>(n);
    else if (name == "--paired" && number(value, journal::SLOTS, 10, n)) options.paired = static_cast<uint32_t>(n);
    else if (name == "--seed" && number(value, 0xFFFFFFFFu, 10, n) && n) options.seed = static_cast<uint32_t>(n);
    else if (name == "--generation" && hex16(value.c_str()) && number(value, ~0ull, 16, n) && n) options.generation = n;
    else if (name == "--identity-base" && number(value, 0xFF0000, 16, n) && !(n & 0xFFFF)) options.identity_base = static_cast<uint32_t>(n);
    else if (name == "--device-id") options.device_id = value;
    else if (name == "--journal" && has_value) options.journal_file = value;
    else if (name == "--ota-file" && has_value && !value.empty()) options.ota_file = value;
    else if (name == "--ota-reboot" && !has_value) options.ota_reboot = true;
    else if (name == "--ota-active" && has_value && !value.empty()) options.ota_active = value;
    else if (name == "--ota-prefix" && has_value && !value.empty()) options.ota_prefix = value;
    else if (name == "--fault" && value == "none") {}
    else if (name == "--fault" && value == "unavailable") options.unavailable = true;
    else if (name == "--fault" && value == "start") options.fail_start = true;
    else if (name == "--fault" && value == "poll") options.fail_poll = true;
    else if (name == "--enrollment-suffix" && number(value, 0xFF, 16, n))
      options.profile.identity_suffix = static_cast<uint8_t>(n);
    else return false;
  }
  return (options.ota_prefix.empty() && options.ota_active.empty() && !options.ota_reboot) || !options.ota_file.empty();
}

void provision_paired(journal::Journal& journal, const Options& options) {
  if (journal.open() != journal::StorageState::empty) return;
  if (!options.paired) return;  // lifecycle tests exercise the real fresh-start path
  if (journal.initialize(options.generation, static_cast<uint16_t>(options.identity_base >> 8)) != journal::Status::ok) {
    fputs("cannot initialize the simulated journal\n", stderr);
    exit(1);
  }
  for (uint8_t slot = 1; slot <= options.paired; ++slot) {
    while (journal.maintenance_due()) {
      if (journal.maintain() != journal::Status::ok) exit(1);
    }
    journal::NewController fresh;
    fresh.identity = options.identity_base | (uint32_t{slot} << 8) | slot;
    fresh.first_counter = static_cast<uint16_t>(100u * slot);
    fresh.generation = options.generation;
    if (journal.provision(slot, fresh) != journal::Status::ok ||
        journal.confirm(slot) != journal::Status::ok ||
        journal.set_service(slot, true) != journal::Status::ok) {
      fputs("cannot provision the simulated journal\n", stderr);
      exit(1);
    }
  }
  while (journal.maintenance_due()) {
    if (journal.maintain() != journal::Status::ok) exit(1);
  }
}

void reply(int control, char byte) {
  if (control >= 0 && write(control, &byte, 1) < 0) {}  // the bridge going away shows as EOF
}

}  // namespace

int main(int argc, char** argv) {
  Options options;
  if (!parse(argc, argv, options) || !hex16(options.device_id.c_str())) {
    fputs("usage: mysensors_server [--control-fd=N] [--burst-ms=N] [--paired=N] [--journal=FILE]\n"
          "       [--device-id=HEX16] [--generation=HEX16] [--identity-base=HEX6] [--seed=N]\n"
          "       [--no-tx] [--enrollment] [--prepare-legacy-reset]\n"
          "       [--ota-file=PATH] [--ota-prefix=PATH] (test storage, no reboot)\n"
          "       [--enrollment-suffix=HEX2] [--fault=none|unavailable|start|poll]\n",
          stderr);
    return 2;
  }
  signal(SIGPIPE, SIG_IGN);
  for (int fd : {1, options.control})
    if (fd >= 0) fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);

  const auto epoch = std::chrono::steady_clock::now();
  uint32_t clock = 0;
  auto update_clock = [&] {
    clock = static_cast<uint32_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
                                      std::chrono::steady_clock::now() - epoch).count());
  };

  FileFlash flash(options.journal_file);
  journal::Journal journal(flash);
  if (options.prepare_legacy_reset) {
    if (options.journal_file.empty() || journal.open() != journal::StorageState::legacy ||
        journal.initialize(options.generation, static_cast<uint16_t>(options.identity_base >> 8),
                           options.profile.identity_suffix) != journal::Status::ok) {
      fputs("legacy reset fixture requires an existing valid v1 journal\n", stderr);
      return 1;
    }
    return 0;  // no maintain(), controller, radio or serial bytes
  }
  provision_paired(journal, options);
  SimRadio radio(clock);
  radio.copy_ms = options.burst_ms >= 25 ? options.burst_ms / 25 : 1;
  radio.unavailable = options.unavailable;
  radio.fail_start = options.fail_start;
  radio.fail_poll = options.fail_poll;
  Policy policy;
  policy.state = options.seed;
  std::optional<FileOTAStorage> ota_storage;
  if (!options.ota_file.empty()) ota_storage.emplace(radio, options.ota_file, options.ota_prefix, options.ota_active);
  std::optional<mysensors::Gateway<SimRadio, Policy>> instance;
  auto restart = [&] {
    if (ota_storage) policy.sketch = ota_storage->sketch();
    instance.emplace(journal, radio, policy, options.device_id.c_str(), ota_storage ? &*ota_storage : nullptr);
    instance->begin(options.tx, options.enrollment, 208500, options.profile);
  };
  restart();
  auto &gateway = instance;
  char generation[17] = "none";
  journal.generation_hex(generation);
  unsigned paired = 0;
  for (uint8_t slot = 1; slot <= journal::SLOTS; ++slot)
    paired += journal.shutter(slot).state == journal::SlotState::paired;
  report("ready paired=%u generation=%s", paired, generation);

  char rx[256];  // like the firmware: a small window, one line per feed
  size_t rx_used = 0, rx_pos = 0;
  bool eof = false, failure_reported = false, link_up = options.control < 0;
  if (link_up) {
    update_clock();
    gateway->connected();
  }

  auto flush = [&] {
    while (gateway->output_size()) {
      const ssize_t put = write(1, gateway->output_data(), gateway->output_contiguous());
      if (put > 0) gateway->consume_output(static_cast<size_t>(put));
      else if (errno == EINTR) continue;
      else if (errno == EAGAIN) return true;
      else return false;  // the reader is gone
    }
    return true;
  };
  auto discard_input = [&] {
    rx_used = rx_pos = 0;
    for (;;) {  // drop both the RX window and bytes still queued by the old link
      pollfd stale{0, POLLIN, 0};
      char junk[4096];
      if (poll(&stale, 1, 0) <= 0 || !(stale.revents & (POLLIN | POLLHUP))) break;
      const ssize_t dropped = read(0, junk, sizeof(junk));
      if (dropped <= 0) {
        eof = eof || dropped == 0;
        break;
      }
    }
  };

  bool committed = false;
  uint32_t committed_at = 0;
  for (;;) {
    pollfd fds[2];
    nfds_t count = 0;
    int in_slot = -1, control_slot = -1;
    if (!eof && !gateway->failed() && rx_pos == rx_used) {
      in_slot = static_cast<int>(count);
      fds[count++] = {0, POLLIN, 0};
    }
    if (options.control >= 0) {
      control_slot = static_cast<int>(count);
      fds[count++] = {options.control, POLLIN, 0};
    }
    poll(fds, count, rx_pos < rx_used ? 0 : 1);

    if (control_slot >= 0 && (fds[control_slot].revents & (POLLIN | POLLHUP))) {
      char byte;
      const ssize_t got = read(options.control, &byte, 1);
      if (got == 0) return 0;  // bridge closed the control channel
      if (got == 1 && byte == 'C') {
        update_clock();
        link_up = true;
        gateway->connected();
        reply(options.control, 'C');
      } else if (got == 1 && byte == 'D') {
        update_clock();
        link_up = false;
        gateway->disconnected();
        discard_input();
        in_slot = -1;  // discard_input consumed the readiness reported by poll
        failure_reported = false;
        reply(options.control, 'D');
      } else if (got == 1 && (byte == 'H' || byte == 'R')) {
        radio.held = byte == 'H';
        reply(options.control, byte);
      } else if (got == 1) reply(options.control, '?');
    }
    if (in_slot >= 0 && (fds[in_slot].revents & (POLLIN | POLLHUP))) {
      const ssize_t got = read(0, rx, sizeof(rx));
      if (got > 0) {
        rx_used = static_cast<size_t>(got);
        rx_pos = 0;
      } else if (got == 0 || (errno != EINTR && errno != EAGAIN)) eof = true;
    }

    update_clock();
    // Like the firmware loop, nothing is offered to the adapter while the host
    // has the port closed: those bytes are lost, never queued.
    if (!link_up) rx_pos = rx_used;
    else if (rx_pos < rx_used) rx_pos += gateway->feed(rx + rx_pos, rx_used - rx_pos);
    gateway->tick(clock);  // after feed(): the reply is queued before any completion event
    if (!flush()) return 1;
    if (options.ota_reboot) {
      if (gateway->ota_committed() && !committed) { committed = true; committed_at = clock; }
      if (gateway->reboot_ready() || (committed && clock - committed_at >= 3000)) {
        gateway->disconnected();
        ota_storage->reboot();
        discard_input();
        restart();
        committed = failure_reported = false;
        report("ota reboot active_version=%u", ota_storage->running_config().version);
        if (options.control >= 0) { link_up = false; reply(options.control, 'B'); }
        else if (link_up) gateway->connected();
      }
    }
    if (gateway->failed() && !failure_reported) {
      failure_reported = true;
      reply(options.control, 'F');
    }
    if (eof && rx_pos == rx_used && !gateway->output_size()) return 0;
  }
}
