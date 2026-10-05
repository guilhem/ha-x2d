#include <algorithm>
#include <cassert>
#include <string>
#include <vector>
#include "mysensors.h"
#include "image_layout.h"

using namespace x2d;

namespace {

struct Radio {
  bool running = false, stopping = false, boundary = false;
  uint32_t starts = 0, polls = 0;
  uint8_t copies = 0;
  bool available() const { return true; }
  bool start_burst(const radio::Waveform &wave, uint32_t, uint32_t &) {
    assert(!running);
    running = true;
    stopping = boundary = false;
    copies = wave.copies();
    ++starts;
    return true;
  }
  radio::FrameState poll_burst(uint8_t &completed) {
    assert(running);
    ++polls;
    completed = boundary ? stopping ? 1 : copies : 0;
    return boundary ? radio::FrameState::complete : radio::FrameState::busy;
  }
  void request_stop() { assert(running); stopping = true; }
  void end_burst() { assert(boundary); running = false; }
};

struct Policy {
  uint32_t random_u32() { return 42; }
  const char *firmware() { return "0.6.0-rc2"; }
};

void put_u32(uint8_t *bytes, uint32_t value) {
  for (unsigned i = 0; i < 4; ++i) bytes[i] = static_cast<uint8_t>(value >> (8 * i));
}
// Synthetic board image: a known immutable prefix, valid application vectors,
// marker, arbitrary code and FF padding. The simulated storage runs the actual
// portable ImageVerifier on readback; no physical flash operation is validated.
std::vector<uint8_t> image(uint16_t version = ota::VERSION) {
  std::vector<uint8_t> bytes(ota::APPLICATION_OFFSET + 69, 0xFF);
  for (size_t i = 0; i < ota::APPLICATION_OFFSET; ++i)
    bytes[i] = static_cast<uint8_t>(i * 37 + 11);
  put_u32(bytes.data() + ota::APPLICATION_OFFSET, 0x20040000);
  put_u32(bytes.data() + ota::APPLICATION_OFFSET + 4, 0x10003021);
  for (size_t i = ota::APPLICATION_OFFSET + 8; i < bytes.size(); ++i)
    bytes[i] = static_cast<uint8_t>(i * 13 + 7);
  memcpy(bytes.data() + ota::APPLICATION_OFFSET + 40, ota::IMAGE_MARKER, sizeof(ota::IMAGE_MARKER) - 1);
  char identity[7];
  snprintf(identity, sizeof(identity), "%u:", version);
  memcpy(bytes.data() + ota::APPLICATION_OFFSET + 40 + sizeof(ota::IMAGE_MARKER) - 1,
         identity, strlen(identity));
  bytes.resize(ota::padded_image_size(bytes.size()), 0xFF);
  return bytes;
}

struct Storage final : ota::Storage {
  Radio &radio;
  std::vector<uint8_t> bytes;
  uint32_t expected = 0, begins = 0, writes = 0, finishes = 0, aborts = 0;
  bool committed = false, fail_begin = false, fail_write = false, fail_finish = false;
  bool corrupt_readback = false;
  explicit Storage(Radio &r) : radio(r) {}
  ota::FirmwareConfig running_config() const override {
    const auto active = image(6);
    return ota::running_config(active.data(), active.size(), 6);
  }
  bool begin(uint32_t length) override {
    assert(!radio.running && !committed);
    ++begins;
    expected = length;
    bytes.clear();
    return !fail_begin;
  }
  bool write(const uint8_t *block, size_t length) override {
    assert(!radio.running && !committed && begins && length == ota::BLOCK_BYTES);
    ++writes;
    if (fail_write || bytes.size() + length > expected) return false;
    bytes.insert(bytes.end(), block, block + length);
    return true;
  }
  bool finish(uint16_t crc, uint16_t version) override {
    assert(!radio.running && !committed);
    ++finishes;
    if (corrupt_readback && !bytes.empty()) bytes.back() ^= 1;
    if (fail_finish || bytes.size() != expected ||
        bytes.size() < ota::APPLICATION_OFFSET + 8)
      return false;
    const auto original = image();
    ota::ImageVerifier verifier(original.data(), expected, crc, version);
    for (size_t offset = 0; offset < bytes.size(); offset += 256) {
      const size_t length = std::min(size_t{256}, bytes.size() - offset);
      if (!verifier.add(bytes.data() + offset, length)) return false;
    }
    if (!verifier.finish()) return false;
    committed = true;
    return true;
  }
  void abort() override {
    assert(!radio.running && !committed);
    ++aborts;
    bytes.clear();
  }
};

struct Rig {
  journal::MemoryFlash flash;
  journal::Journal journal{flash};
  Radio radio;
  Policy policy;
  Storage storage{radio};
  mysensors::Gateway<Radio, Policy> gateway;
  uint32_t now = 0;
  explicit Rig(bool available = true)
      : gateway(journal, radio, policy, "0123456789ABCDEF", available ? &storage : nullptr) {
    assert(journal.open() == journal::StorageState::empty);
    assert(journal.provision(1, {0x100001, 10, 1}) == journal::Status::ok);
    assert(journal.confirm(1) == journal::Status::ok);
    while (journal.maintenance_due()) assert(journal.maintain() == journal::Status::ok);
    assert(gateway.begin(true, true, 208500, {1}));
    gateway.connected();
    for (unsigned step = 0; gateway.presentation_pending() && step < 500; ++step) {
      gateway.tick(0);
      drain();
    }
    assert(!gateway.presentation_pending());
    drain();
  }
  std::string drain() {
    std::string out;
    while (gateway.output_size()) {
      out.append(gateway.output_data(), gateway.output_contiguous());
      gateway.consume_output(gateway.output_contiguous());
    }
    return out;
  }
  void feed(const std::string &text, size_t chunk = 0) {
    if (!chunk) chunk = text.size();
    size_t at = 0;
    while (at < text.size()) {
      const size_t used = gateway.feed(text.data() + at, std::min(chunk, text.size() - at));
      if (!used) break;
      at += used;
    }
  }
  void tick(uint32_t time) { now = time; gateway.tick(time); }
  uint32_t next() {
    uint32_t value = 0;
    assert(journal.next_counter(1, &value));
    return value;
  }
};

void has(const std::string &text, const std::string &part) {
  if (text.find(part) == std::string::npos) {
    fprintf(stderr, "Missing [%s] in [%s]\n", part.c_str(), text.c_str());
    abort();
  }
}
void no_completion(Rig &rig, const std::string &out) {
  assert(!rig.gateway.ota_committed() && !rig.gateway.reboot_requested() && !rig.storage.committed);
  assert(out.find("ota_staged") == std::string::npos);
}
std::string stream(uint8_t subtype, const uint8_t *bytes, size_t size) {
  char hex[51];
  assert(size <= 25);
  ota::encode_hex(bytes, size, hex);
  return "1;255;4;0;" + std::to_string(subtype) + ";" + hex + "\n";
}
std::string config(uint16_t version, uint16_t blocks, uint16_t crc, uint16_t type = ota::FIRMWARE_TYPE) {
  uint8_t bytes[8];
  ota::put_u16(bytes, type);
  ota::put_u16(bytes + 2, version);
  ota::put_u16(bytes + 4, blocks);
  ota::put_u16(bytes + 6, crc);
  return stream(ota::CONFIG_RESPONSE, bytes, sizeof(bytes));
}
std::string config(const std::vector<uint8_t> &bytes, uint16_t version = 7) {
  return config(version, static_cast<uint16_t>(bytes.size() / 16), ota::crc16(bytes.data(), bytes.size()));
}
std::string response(const std::vector<uint8_t> &bytes, uint16_t index,
                     uint16_t version = 7, uint16_t type = ota::FIRMWARE_TYPE) {
  uint8_t block[22];
  ota::put_u16(block, type);
  ota::put_u16(block + 2, version);
  ota::put_u16(block + 4, index);
  assert(size_t{index} * 16 + 16 <= bytes.size());
  memcpy(block + 6, bytes.data() + size_t{index} * 16, 16);
  return stream(ota::BLOCK_RESPONSE, block, sizeof(block));
}
std::string request(uint16_t index, uint16_t version = 7) {
  uint8_t bytes[6];
  ota::put_u16(bytes, ota::FIRMWARE_TYPE);
  ota::put_u16(bytes + 2, version);
  ota::put_u16(bytes + 4, index);
  return stream(ota::BLOCK_REQUEST, bytes, sizeof(bytes));
}
void offer(Rig &rig, const std::vector<uint8_t> &bytes) {
  rig.feed(config(bytes), 3);
  assert(rig.gateway.updating() && rig.storage.begins == 0);
  rig.tick(rig.now);
  assert(rig.storage.begins == 1);
  has(rig.drain(), request(0));
}
void transfer(Rig &rig, const std::vector<uint8_t> &bytes) {
  for (uint16_t i = 0; i < bytes.size() / 16; ++i) {
    std::string line = response(bytes, i);
    line.insert(line.size() - 1, "\r");
    rig.feed(line, 3);  // every integer, payload and CRLF is split across USB reads
    const auto out = rig.drain();
    assert(rig.storage.writes == uint32_t{i} + 1);
    if (size_t{i} + 1 < bytes.size() / 16) has(out, request(i + 1));
    else assert(out.empty());
  }
  assert(rig.storage.finishes == 0 && !rig.gateway.ota_committed());
}

void discovery_and_commit() {
  assert(ota::crc16(reinterpret_cast<const uint8_t *>("123456789"), 9) == 0x4B37);
  Rig rig;
  const auto writes = rig.flash.programs();
  rig.feed("1;255;4;0;0;\r\n", 1);
  const auto announced = rig.storage.running_config();
  uint8_t words[10];
  ota::put_u16(words, announced.type); ota::put_u16(words + 2, announced.version);
  ota::put_u16(words + 4, announced.blocks); ota::put_u16(words + 6, announced.crc);
  ota::put_u16(words + 8, announced.bootloader_version);
  assert(rig.drain() == "1;255;3;0;9;ota_id:0123456789ABCDEF\n" + stream(0, words, 10));
  assert(!rig.gateway.updating() && rig.storage.begins == 0);
  const auto bytes = image();
  assert(bytes.back() == 0xFF);
  offer(rig, bytes);
  rig.feed("2;1;1;1;29;1\n2;1;1;1;30;1\n2;1;1;1;31;1\n1;17;1;1;2;1\n1;20;1;1;2;1\n");
  rig.tick(rig.now);  // queued authoritative refusal is still serviced during OTA
  const auto blocked = rig.drain();
  has(blocked, "Mise a jour en cours");
  assert(blocked.find(";1;1;") == std::string::npos);  // no actuator receipt echo
  assert(rig.next() == 10 && rig.flash.programs() == writes && rig.radio.starts == 0);
  transfer(rig, bytes);
  rig.feed(response(bytes, static_cast<uint16_t>(bytes.size() / 16 - 1)));
  rig.feed(config(bytes));
  assert(rig.drain().empty() && rig.storage.writes == bytes.size() / 16);
  rig.tick(1);
  assert(rig.storage.bytes == bytes && rig.storage.finishes == 1);
  assert(rig.gateway.ota_committed() && rig.gateway.updating() && !(rig.gateway.output_size() == 0));
  assert(rig.drain() == "1;255;3;0;9;ota_staged\n");
  assert((rig.gateway.output_size() == 0) && !rig.gateway.reboot_requested());
  rig.feed("0;255;3;0;13;\n1;255;3;0;13;unexpected\n");
  assert(!rig.gateway.reboot_requested());
  rig.feed("1;255;3;0;13;\n", 1);
  assert(rig.gateway.reboot_requested());
  rig.tick(50000);
  assert(rig.storage.finishes == 1 && rig.storage.aborts == 0 && rig.next() == 10);
  rig.feed("2;1;1;0;29;1\n");
  rig.tick(50001);
  assert(rig.radio.starts == 0);
}

void active_announcement_and_standard_reboot() {
  Rig rig;
  const auto active = image(6);
  const auto current = rig.storage.running_config();
  assert(current.version == 6 && current.blocks == active.size() / 16 &&
         current.crc == ota::crc16(active.data(), active.size()) && current.bootloader_version == 1);
  uint8_t words[10];
  ota::put_u16(words, current.type); ota::put_u16(words + 2, current.version);
  ota::put_u16(words + 4, current.blocks); ota::put_u16(words + 6, current.crc);
  ota::put_u16(words + 8, current.bootloader_version);
  const auto announcement = stream(0, words, sizeof(words));
  rig.gateway.disconnected();
  rig.gateway.connected();
  const auto startup = rig.drain();
  has(startup, announcement);  // emitted before any application presentation
  for (unsigned step = 0; rig.gateway.presentation_pending(); ++step) {
    assert(step < 100);
    rig.tick(step);
    const auto line = rig.drain();
    assert(line.find(";255;4;0;0;") == std::string::npos);  // no virtual shutter OTA
  }
  rig.feed(config(active, 6));
  rig.tick(100);
  assert(!rig.gateway.updating() && rig.storage.begins == 0 && rig.drain().empty());
  rig.feed("2;255;3;0;19;\n");
  assert(rig.drain().find(";255;4;0;0;") == std::string::npos);
  rig.feed("1;255;3;0;19;\n");
  has(rig.drain(), announcement);
  for (const auto *invalid : {"0;255;3;0;13;\n", "255;255;3;0;13;\n", "2;255;3;0;13;\n",
                             "1;1;3;0;13;\n", "1;255;3;1;13;\n", "1;255;3;0;13;1\n"}) {
    rig.feed(invalid);
    assert(!rig.gateway.reboot_requested());
  }
  rig.feed("2;1;1;0;29;1\n");
  rig.tick(101);
  assert(rig.radio.running && rig.next() == 11);
  rig.feed("2;1;1;0;30;1\n1;255;3;0;13;\n");
  assert(rig.gateway.reboot_requested() && !rig.gateway.reboot_ready() && rig.radio.stopping);
  rig.feed(config(image()));  // no staging between reset request and actual reset
  rig.radio.boundary = true;
  rig.tick(102);
  assert(rig.gateway.reboot_ready() && rig.radio.starts == 1 && rig.next() == 11 && rig.storage.begins == 0);
}

void bounded_retry_and_duplicates() {
  Rig rig;
  const auto bytes = image();
  rig.tick(0xFFFFFF00u);
  offer(rig, bytes);
  rig.tick(0xFFFFFF00u + 499u);
  assert(rig.drain().empty());
  rig.tick(0xFFFFFF00u + 500u);  // wrap-safe retry
  assert(rig.drain() == request(0));
  rig.feed(config(bytes));  // matching config requests the current block, no erase
  assert(rig.drain() == request(0) && rig.storage.begins == 1);
  rig.feed(response(bytes, 0));
  assert(rig.drain() == request(1));
  rig.feed(response(bytes, 0));
  assert(rig.drain() == request(1) && rig.storage.writes == 1);
  const auto began_at = rig.now;
  for (uint32_t attempt = 1; attempt < ota::MAX_ATTEMPTS; ++attempt) {
    rig.tick(began_at + attempt * ota::RETRY_MS);
    assert(rig.drain() == request(1));
    rig.feed(response(bytes, 0));
    assert(rig.drain() == request(1));
  }
  rig.tick(began_at + ota::MAX_ATTEMPTS * ota::RETRY_MS);
  const auto out = rig.drain();
  has(out, "ota_error:timeout");
  no_completion(rig, out);
  assert(!rig.gateway.updating() && rig.storage.aborts == 1 && rig.storage.writes == 1);
  rig.feed("2;1;1;0;29;1\n");
  rig.tick(rig.now + 1);
  assert(rig.radio.starts == 1 && rig.next() == 11);  // resume only freshly requested RF
}

void discovery_during_transfer_does_not_advertise_running_image() {
  Rig rig;
  const auto bytes = image();
  offer(rig, bytes);
  rig.feed(response(bytes, 0));
  assert(rig.drain() == request(1));
  const auto begins = rig.storage.begins;
  // Discovery can still be in flight when the controller serves the first
  // block. None of these paths may reannounce the old application identity.
  rig.feed("1;255;3;0;19;\n255;255;3;0;19;\n2;255;3;0;19;\n1;255;4;0;0;\n");
  rig.tick(10);
  auto output = rig.drain();
  assert(output.find(";255;4;0;0;") == std::string::npos);
  assert(output.find("ota_error:") == std::string::npos);
  assert(rig.gateway.updating() && rig.storage.writes == 1 && rig.storage.begins == begins);
  for (uint16_t index = 1; index < bytes.size() / 16; ++index) {
    rig.feed(response(bytes, index));
    output = rig.drain();
    if (size_t{index} + 1 < bytes.size() / 16) assert(output == request(index + 1));
    else assert(output.empty());
  }
  rig.tick(11);
  assert(rig.gateway.ota_committed() && rig.storage.aborts == 0 && rig.storage.begins == begins);
  assert(rig.drain() == "1;255;3;0;9;ota_staged\n");
  rig.feed("1;255;3;0;19;\n1;255;4;0;0;\n");
  assert(rig.drain().empty());
  rig.gateway.disconnected();
  rig.gateway.connected();  // a USB reopen before reset still runs the old app
  assert(rig.drain().find(";255;4;0;0;") == std::string::npos);
}

void wire_bounds() {
  Rig rig;
  rig.feed(config(0xFFFF, static_cast<uint16_t>(ota::MAX_BLOCKS / 8 * 8), 0x1234));
  rig.tick(0);
  assert(rig.storage.expected == 1048448);
  assert(rig.drain() == request(0, 0xFFFF));
  rig.gateway.disconnected();
  assert(rig.storage.aborts == 1);

  // Exercise the largest canonical image: 65528 blocks stop at index 65527,
  // with no wrapping or extra block request after completion.
  Rig largest;
  auto bytes = image();
  bytes.resize(ota::MAX_BLOCKS / 8 * 8 * ota::BLOCK_BYTES, 0xFF);
  offer(largest, bytes);
  transfer(largest, bytes);
  largest.tick(1);
  assert(largest.gateway.ota_committed() && largest.storage.writes == ota::MAX_BLOCKS / 8 * 8);
  assert(largest.drain() == "1;255;3;0;9;ota_staged\n");
}

void malformed_and_unrequested() {
  const auto bytes = image();
  for (const std::string &bad : {
       config(7, 0, 0), config(7, 768, 0), config(7, 776, 0, 0x1234),
       std::string("1;255;4;0;1;32580600\n"), response(bytes, 0),
       std::string("1;255;4;0;2;325806000000\n")}) {
    Rig rig;
    rig.feed(bad);
    const auto out = rig.drain();
    has(out, "ota_error:");
    no_completion(rig, out);
    assert(!rig.gateway.updating() && rig.storage.begins == 0);
  }
  Rig unavailable(false);
  unavailable.feed(config(bytes));
  has(unavailable.drain(), "ota_error:unavailable");
  assert(!unavailable.gateway.updating());

  for (const std::string &bad : {
       response(bytes, 1), response(bytes, 0, 8), response(bytes, 0, 7, 0x1234),
       std::string("1;255;4;0;3;325806000000FFFF\n"),
       std::string("1;255;4;0;3;325806000000ZZ\n"),
       config(7, 776, 3), config(8, 776, 0), config(7, 0, 0)}) {
    Rig rig;
    offer(rig, bytes);
    rig.feed(bad);
    const auto out = rig.drain();
    has(out, "ota_error:");
    no_completion(rig, out);
    assert(rig.storage.writes == 0 && rig.storage.aborts == 1 && !rig.gateway.updating());
  }
  Rig rig;
  offer(rig, bytes);
  rig.feed(response(bytes, 0));
  rig.drain();
  auto changed = bytes;
  changed[0] ^= 1;
  rig.feed(response(changed, 0));
  has(rig.drain(), "ota_error:duplicate");
  assert(rig.storage.writes == 1 && rig.storage.aborts == 1);
}

void crc_and_storage_validation() {
  for (unsigned fault = 0; fault < 8; ++fault) {
    Rig rig;
    auto bytes = image();
    if (fault == 1) bytes[0] ^= 1;  // valid wire CRC, wrong bootloader prefix
    if (fault == 2) put_u32(bytes.data() + ota::APPLICATION_OFFSET, 0xFFFFFFFF);
    if (fault == 3) put_u32(bytes.data() + ota::APPLICATION_OFFSET + 4, 0x10003020);
    if (fault == 4) put_u32(bytes.data() + ota::APPLICATION_OFFSET + 4, ota::FLASH_BASE + bytes.size() + 1);  // reset vector points beyond a truncated image
    if (fault == 6) memset(bytes.data() + ota::APPLICATION_OFFSET + 40, 0xFF, sizeof(ota::IMAGE_MARKER) - 1);
    if (fault == 7) bytes[ota::APPLICATION_OFFSET + 40 + sizeof(ota::IMAGE_MARKER) - 1] = '8';
    rig.storage.corrupt_readback = fault == 5;
    offer(rig, bytes);
    if (fault == 0) bytes.back() ^= 1;  // block corruption, config CRC unchanged
    transfer(rig, bytes);
    rig.tick(1);
    const auto out = rig.drain();
    has(out, fault ? "ota_error:image" : "ota_error:crc");
    no_completion(rig, out);
    assert(rig.storage.finishes == (fault ? 1u : 0u) && rig.storage.aborts == 1);
  }
  for (unsigned fault = 0; fault < 3; ++fault) {
    Rig rig;
    const auto bytes = image();
    rig.storage.fail_begin = fault == 0;
    rig.storage.fail_write = fault == 1;
    rig.storage.fail_finish = fault == 2;
    rig.feed(config(bytes));
    rig.tick(0);
    auto out = rig.drain();
    if (fault == 1) {
      rig.feed(response(bytes, 0));
      out += rig.drain();
    } else if (fault == 2) {
      transfer(rig, bytes);
      rig.tick(1);
      out += rig.drain();
    }
    has(out, fault == 0 ? "ota_error:begin" : fault == 1 ? "ota_error:write" : "ota_error:image");
    no_completion(rig, out);
    assert(rig.storage.aborts == 1 && !rig.gateway.updating());
  }
}

void disconnect_before_and_after_commit() {
  const auto bytes = image();
  for (unsigned phase = 0; phase < 4; ++phase) {
    Rig rig;
    rig.feed(config(bytes));
    if (phase) { rig.tick(0); rig.drain(); }
    if (phase == 2) { rig.feed(response(bytes, 0)); rig.drain(); }
    if (phase == 3) transfer(rig, bytes);  // last block received, commit not yet entered
    rig.gateway.disconnected();
    rig.tick(1);
    no_completion(rig, rig.drain());
    assert(rig.storage.finishes == 0 && rig.storage.aborts == (phase ? 1u : 0u));
    rig.gateway.connected();
    rig.drain();
    rig.tick(2);
    assert(!rig.gateway.updating() && rig.radio.starts == 0 && rig.next() == 10);
  }
  Rig rig;
  offer(rig, bytes);
  transfer(rig, bytes);
  rig.tick(1);  // successful finish, ota_staged still buffered
  rig.gateway.disconnected();
  rig.tick(2);
  assert(rig.gateway.ota_committed() && (rig.gateway.output_size() == 0) && rig.gateway.updating());
  assert(rig.storage.aborts == 0 && rig.storage.committed);
  rig.gateway.connected();
  rig.drain();
  rig.feed(config(bytes));
  rig.feed("2;1;1;0;29;1\n");
  rig.tick(3);
  assert(rig.radio.starts == 0 && rig.storage.begins == 1 && rig.storage.finishes == 1);
}

void rf_settles_before_flash() {
  const auto bytes = image();
  Rig rig;
  rig.feed("2;1;1;0;29;1\n");
  rig.tick(0);
  assert(rig.radio.running && rig.next() == 11);
  rig.feed("2;1;1;0;30;1\n");  // queued but not reserved
  rig.feed(config(bytes));
  rig.drain();
  assert(rig.radio.stopping && rig.storage.begins == 0);
  for (uint32_t time = 1; time < 50; ++time) {
    rig.tick(time);
    assert(rig.storage.begins == 0 && rig.radio.running);
  }
  assert(rig.radio.polls == 49);
  rig.radio.boundary = true;
  rig.tick(50);
  assert(!rig.radio.running && rig.storage.begins == 1 && rig.storage.writes == 0);
  has(rig.drain(), request(0));
  rig.feed(response(bytes, 0));
  rig.drain();
  assert(rig.storage.writes == 1 && rig.next() == 11);
  rig.tick(2550);
  has(rig.drain(), "ota_error:timeout");
  rig.tick(2551);
  assert(rig.radio.starts == 1 && rig.next() == 11);  // dropped CLOSE never replays

  Rig held;
  held.feed("2;1;1;0;29;1\n");
  held.tick(0);
  held.feed(config(bytes));
  held.drain();
  held.tick(2500);
  has(held.drain(), "ota_error:radio_timeout");
  assert(held.storage.begins == 0 && held.storage.aborts == 0);
  held.radio.boundary = true;
  held.tick(2501);
  assert(!held.radio.running && held.next() == 11);
}

void slow_host_and_commit_output_failure() {
  const auto bytes = image();
  for (unsigned phase = 0; phase < 3; ++phase) {
    Rig rig;
    offer(rig, bytes);
    if (phase) transfer(rig, bytes);
    if (phase == 2) rig.tick(1);
    for (unsigned i = 0; i < 1000 && !rig.gateway.failed(); ++i)
      rig.feed("1;255;3;0;2;\n");
    assert(rig.gateway.failed());
    rig.tick(2);
    const auto out = rig.drain();
    has(out, "serial_overflow");
    if (phase == 2) {
      assert(rig.gateway.ota_committed() && (rig.gateway.output_size() == 0) && rig.storage.aborts == 0);
    } else {
      no_completion(rig, out);
      assert(!rig.gateway.updating() && rig.storage.aborts == 1 && rig.storage.finishes == 0);
    }
  }
  Rig rig;
  offer(rig, bytes);
  transfer(rig, bytes);
  // Fill the output to leave insufficient space for ota_staged. The durable
  // finish remains committed even if its notification itself overflows.
  const std::string version_reply = "1;255;3;0;2;2.3.2\n";
  while (rig.gateway.output_size() + version_reply.size() <= OutputBuffer::CAPACITY)
    rig.feed("1;255;3;0;2;\n");
  assert(!rig.gateway.failed());
  rig.tick(1);
  assert(rig.gateway.failed() && rig.gateway.ota_committed() && rig.storage.committed);
  assert(rig.storage.aborts == 0 && rig.storage.finishes == 1);
  has(rig.drain(), "serial_overflow");
  assert((rig.gateway.output_size() == 0));
}

}  // namespace

int main() {
  discovery_during_transfer_does_not_advertise_running_image();
  active_announcement_and_standard_reboot();
  discovery_and_commit();
  bounded_retry_and_duplicates();
  wire_bounds();
  malformed_and_unrequested();
  crc_and_storage_validation();
  disconnect_before_and_after_commit();
  rf_settles_before_flash();
  slow_host_and_commit_output_failure();
  puts("ota: serial discovery, storage, CRC, retries, cancellation, RF quiescence and irreversible commit passed");
}
