#include <Arduino.h>
#include <SPI.h>
#include <USB.h>
#include <pico/unique_id.h>
#include <hardware/flash.h>
#include <hardware/sync.h>

#include "protocol.h"
#include "journal.h"
#include "radio_runtime.h"
#include "radio_tx.h"

static_assert(ha_x2d::journal::SLOTS == ha_x2d::MAX_SHUTTERS, "slot contract mismatch");

extern uint8_t _FS_start, _FS_end;

namespace {

constexpr uint8_t PIN_MISO = 16;
constexpr uint8_t PIN_CS = 17;
constexpr uint8_t PIN_SCK = 18;
constexpr uint8_t PIN_MOSI = 19;
const SPISettings RADIO_SPI(4000000, MSBFIRST, SPI_MODE0);

ha_x2d::LineFramer input;
ha_x2d::OutputBuffer usb_output;
char output[ha_x2d::MAX_LINE_BYTES];
char device_id[17];
char session[17];
bool handshaken = false;
bool output_failed = false;
uint32_t highest_id = 0;
uint32_t event_sequence = 0, admitting_id = 0;
uint32_t connection_id = 0;
const char* admission_error = nullptr;
bool radio_configured = false;

// Default builds keep RF disabled. The commands-only build uses identities
// already paired in the journal; new enrollment remains a separate trial.
#ifndef HA_X2D_COMMANDS_TX
#define HA_X2D_COMMANDS_TX 0
#endif
static_assert(HA_X2D_COMMANDS_TX == 0 || HA_X2D_COMMANDS_TX == 1,
              "commands TX must be 0 or 1");
#ifndef HA_X2D_SUPERVISED_TX
#define HA_X2D_SUPERVISED_TX 0
#endif
static_assert(HA_X2D_SUPERVISED_TX == 0 || HA_X2D_SUPERVISED_TX == 1,
              "supervised TX must be 0 or 1");
#if HA_X2D_SUPERVISED_TX && !defined(HA_X2D_TRIAL_SUFFIX)
#error "Set the private observed identity suffix for this supervised trial"
#endif
#ifndef HA_X2D_TRIAL_SUFFIX
#define HA_X2D_TRIAL_SUFFIX 0
#endif
static_assert(HA_X2D_TRIAL_SUFFIX >= 0 && HA_X2D_TRIAL_SUFFIX <= 255,
              "trial identity suffix must be one byte");
#ifndef HA_X2D_TRIAL_EXPECTED_NEXT_COUNTER
#define HA_X2D_TRIAL_EXPECTED_NEXT_COUNTER 0
#endif
static_assert(HA_X2D_TRIAL_EXPECTED_NEXT_COUNTER == 0 ||
              HA_X2D_TRIAL_EXPECTED_NEXT_COUNTER == 2,
              "trial expected next counter must be 0 or 2");
constexpr bool TX_ENABLED = HA_X2D_COMMANDS_TX || HA_X2D_SUPERVISED_TX;
constexpr bool ENROLLMENT_ENABLED = HA_X2D_SUPERVISED_TX;

class BoardFlash final : public ha_x2d::journal::Flash {
 public:
  static constexpr uintptr_t ADDRESS = 0x101FF000;
  uint32_t size() const override {
    return reinterpret_cast<uintptr_t>(&_FS_start) == ADDRESS &&
           reinterpret_cast<uintptr_t>(&_FS_end) >= ADDRESS + ha_x2d::journal::REGION_BYTES
               ? ha_x2d::journal::REGION_BYTES : 0;
  }
  bool read(uint32_t offset, void* out, uint32_t length) override {
    if (!size() || offset > size() || length > size() - offset) return false;
    memcpy(out, reinterpret_cast<const void*>(ADDRESS + offset), length);
    return true;
  }
  bool erase_sector(uint32_t offset) override {
    if (!size() || offset >= size() || offset % FLASH_SECTOR_SIZE) return false;
    const uint32_t saved = save_and_disable_interrupts();
    rp2040.idleOtherCore();
    flash_range_erase(ADDRESS - XIP_BASE + offset, FLASH_SECTOR_SIZE);
    rp2040.resumeOtherCore();
    restore_interrupts(saved);
    return true;
  }
  bool program_page(uint32_t offset, const uint8_t* page) override {
    if (!size() || offset >= size() || offset % FLASH_PAGE_SIZE) return false;
    const uint32_t saved = save_and_disable_interrupts();
    rp2040.idleOtherCore();
    flash_range_program(ADDRESS - XIP_BASE + offset, page, FLASH_PAGE_SIZE);
    rp2040.resumeOtherCore();
    restore_interrupts(saved);
    return true;
  }
} flash;
ha_x2d::journal::Journal journal(flash);

struct CachedReply {
  ha_x2d::Request request;
  size_t length = 0;
  char bytes[ha_x2d::MAX_LINE_BYTES];
};
CachedReply replies[16];
size_t reply_index = 0;

void hex_id(const uint8_t* bytes, char* destination) {
  const char digits[] = "0123456789ABCDEF";
  for (size_t i = 0; i < 8; ++i) {
    destination[i * 2] = digits[bytes[i] >> 4];
    destination[i * 2 + 1] = digits[bytes[i] & 15];
  }
  destination[16] = '\0';
}

bool radio_ready() {
  const uint32_t start = micros();
  while (digitalRead(PIN_MISO) == HIGH) {
    if (static_cast<uint32_t>(micros() - start) >= 2000) return false;
  }
  return true;
}

void reset_radio() {
  // CC1101 manual reset sequence; no frequency or transmit registers are set.
  digitalWrite(PIN_CS, HIGH);
  delayMicroseconds(40);
  digitalWrite(PIN_CS, LOW);
  delayMicroseconds(10);
  digitalWrite(PIN_CS, HIGH);
  delayMicroseconds(40);

  SPI.beginTransaction(RADIO_SPI);
  digitalWrite(PIN_CS, LOW);
  if (radio_ready()) {
    SPI.transfer(0x30);  // SRES
    radio_ready();
  }
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

bool read_radio_status(uint8_t address, uint8_t& value) {
  SPI.beginTransaction(RADIO_SPI);
  digitalWrite(PIN_CS, LOW);
  if (!radio_ready()) {
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
    return false;
  }
  SPI.transfer(address | 0xC0);  // status register read
  value = SPI.transfer(0);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
  return true;
}

bool radio_strobe(uint8_t command) {
  SPI.beginTransaction(RADIO_SPI);
  digitalWrite(PIN_CS, LOW);
  const bool ready = radio_ready();
  if (ready) SPI.transfer(command);
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
  return ready;
}

bool read_radio_register(uint8_t address, uint8_t& value) {
  SPI.beginTransaction(RADIO_SPI);
  digitalWrite(PIN_CS, LOW);
  const bool ready = radio_ready();
  if (ready) { SPI.transfer(address | 0x80); value = SPI.transfer(0); }
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
  return ready;
}

bool write_radio_register(uint8_t address, uint8_t value) {
  SPI.beginTransaction(RADIO_SPI);
  digitalWrite(PIN_CS, LOW);
  const bool ready = radio_ready();
  if (ready) { SPI.transfer(address); SPI.transfer(value); }
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
  if (!ready) return false;
  uint8_t observed = 0;
  return read_radio_register(address, observed) && observed == value;
}

bool radio_wait_state(uint8_t expected) {
  const uint32_t start = micros();
  do {
    uint8_t state;
    if (!read_radio_status(0x35, state)) return false;
    if ((state & 31) == expected) return true;
    delayMicroseconds(50);
  } while (static_cast<uint32_t>(micros() - start) < 20000);
  return false;
}

bool configure_transmitter() {
  if (!TX_ENABLED) return false;
  pinMode(20, INPUT);
  if (!radio_strobe(0x36) || !radio_wait_state(1)) return false;
  constexpr uint32_t frequency =
      (uint64_t{868350000} * 65536 + 13000000) / 26000000;
  // TI SWRS061I: asynchronous OOK, no packet engine, GDO0 is TX input.
  // The frequency/modulation and PA value still require motor qualification.
  const uint8_t registers[][2] = {
      {0x00, 0x2E}, {0x01, 0x2E}, {0x02, 0x2E},
      {0x07, 0}, {0x08, 0x30}, {0x0A, 0}, {0x0B, 6}, {0x0C, 0},
      {0x0D, static_cast<uint8_t>(frequency >> 16)},
      {0x0E, static_cast<uint8_t>(frequency >> 8)},
      {0x0F, static_cast<uint8_t>(frequency)},
      {0x10, 0x87}, {0x11, 0x85}, {0x12, 0x30}, {0x13, 0}, {0x15, 0},
      {0x16, 0x11}, {0x17, 0x00}, {0x18, 0x08},
      {0x21, 0xB6}, {0x22, 0x11}, {0x2C, 0x81}, {0x2D, 0x35}};
  for (const auto& reg : registers)
    if (!write_radio_register(reg[0], reg[1])) return false;
  SPI.beginTransaction(RADIO_SPI);
  digitalWrite(PIN_CS, LOW);
  const bool ready = radio_ready();
  if (ready) { SPI.transfer(0x7E); SPI.transfer(0); SPI.transfer(0x50); }
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
  if (!ready) return false;
  SPI.beginTransaction(RADIO_SPI);
  digitalWrite(PIN_CS, LOW);
  const bool reading = radio_ready();
  uint8_t off = 1, on = 0;
  if (reading) { SPI.transfer(0xFE); off = SPI.transfer(0); on = SPI.transfer(0); }
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
  return reading && off == 0 && on == 0x50 && radio_strobe(0x33) && radio_wait_state(1);
}

class RadioOutput {
 public:
  bool start_burst(const ha_x2d::radio::Waveform& wave, uint32_t chip_ns,
                   uint32_t& started_ms) {
    if (!radio_configured) return false;
    uint8_t iocfg0, pktctrl0;
    if (!read_radio_register(0x02, iocfg0) || iocfg0 != 0x2E ||
        !read_radio_register(0x08, pktctrl0) || pktctrl0 != 0x30) {
      radio_configured = false;
      return false;
    }
    if (!transmitting_) {
      // PKTCTRL0/IOCFG0 were verified before GPIO ownership changes.
      digitalWrite(20, LOW);
      pinMode(20, OUTPUT);
      if (!radio_strobe(0x35) || !radio_wait_state(0x13)) {
        const bool idle = radio_strobe(0x36) && radio_wait_state(1);
        if (idle) pinMode(20, INPUT);
        else radio_configured = false;  // hold carrier-off low if SPI is lost
        return false;
      }
      transmitting_ = true;
    }
    if (!digital_.start_burst(wave, chip_ns, true)) {
      end_burst();  // a refused digital start must never leave CC1101 in TX
      return false;
    }
    started_ms = static_cast<uint32_t>(digital_.started_us() / 1000);
    burst_baseline_ = digital_.completed_frames();
    burst_copies_ = wave.copies();
    return true;
  }
  ha_x2d::radio::FrameState poll_burst(uint8_t& completed) {
    using ha_x2d::radio::digital_tx::State;
    const State state = digital_.poll();
    const uint32_t count = digital_.completed_frames() - burst_baseline_;
    if (count > burst_copies_) return ha_x2d::radio::FrameState::unknown;
    completed = count;
    if (state == State::running || state == State::stopping)
      return ha_x2d::radio::FrameState::busy;
    if (state == State::frame_boundary) return ha_x2d::radio::FrameState::complete;
    digitalWrite(20, LOW);
    pinMode(20, OUTPUT);  // verified async input: suppress carrier before SPI cleanup
    radio_configured = false;
    return ha_x2d::radio::FrameState::unknown;
  }
  void request_stop() { digital_.request_stop(); }
  void end_burst() {
    // Idle radio BEFORE releasing the data pin; even a partial DMA fault
    // cannot leave a carrier transmitting from a floating input.
    const bool idle = radio_strobe(0x36) && radio_wait_state(1);
    digital_.abort(idle);
    transmitting_ = false;
    if (!idle) {
      digitalWrite(20, LOW);
      pinMode(20, OUTPUT);  // async TX input/HiZ verified; do not leave it floating
      radio_configured = false;
    }
  }
  void service() { digital_.poll(); }
 private:
  ha_x2d::radio::digital_tx::Tx digital_;
  bool transmitting_ = false;
  uint32_t burst_baseline_ = 0;
  uint8_t burst_copies_ = 0;
} radio_output;

void radio_event(const ha_x2d::radio::TxEvent& event);
struct RadioHooks {
  bool profile(const ha_x2d::TxJob& job, ha_x2d::radio::TxProfile& profile) {
    if (!radio_configured || (job.enrollment && !ENROLLMENT_ENABLED)) return false;
    profile.copies = job.enrollment ? 24 : 25;
    profile.chip_ns = ha_x2d::radio::digital_tx::DEFAULT_CHIP_NS;
    return true;
  }
  bool build(const ha_x2d::TxJob& job, const ha_x2d::journal::Reservation& reserved,
             uint8_t phase,
             ha_x2d::radio::Body& body) {
    if (job.enrollment)
      return ha_x2d::radio::make_enrollment_body(reserved.identity, reserved.counter, phase, &body);
    const uint8_t action = job.action == ha_x2d::Action::open ? 0x81
                         : job.action == ha_x2d::Action::close ? 0x82 : 0x04;
    return ha_x2d::radio::make_body(reserved.identity, action, reserved.counter, &body);
  }
  void report(const ha_x2d::radio::TxEvent& event) { radio_event(event); }
} radio_hooks;
ha_x2d::radio::RadioRuntime<RadioOutput, RadioHooks> runtime(journal, radio_output, radio_hooks);

ha_x2d::RadioStatus probe_radio() {
  ha_x2d::RadioStatus result;
  uint8_t partnum, version, marcstate;
  if (!read_radio_status(0x30, partnum) ||
      !read_radio_status(0x31, version) ||
      !read_radio_status(0x35, marcstate)) return result;
  marcstate &= 0x1F;
  // Reject a floating SPI bus; status remains valid during a TX frame.
  if (partnum == 0 && (version == 0x04 || version == 0x14)) {
    result.detected = true;
    result.partnum = partnum;
    result.version = version;
    result.marcstate = marcstate;
  }
  return result;
}

void queue_output(const char* bytes, size_t length) {
  if (output_failed || !usb_output.append(bytes, length)) output_failed = true;
}

void flush_output() {
  const int available = Serial.availableForWrite();
  if (available <= 0 || !usb_output.size()) return;
  const size_t count = usb_output.contiguous() < static_cast<size_t>(available)
                           ? usb_output.contiguous() : static_cast<size_t>(available);
  usb_output.consume(Serial.write(reinterpret_cast<const uint8_t*>(usb_output.data()), count));
}

void send(JsonDocument& doc, const ha_x2d::Request* request = nullptr) {
  const size_t length = serializeJson(doc, output, sizeof(output) - 1);
  if (!length || length != measureJson(doc)) return;
  output[length] = '\n';
  if (request) {
    CachedReply& cached = replies[reply_index++ % 16];
    cached.request = *request;
    cached.length = length + 1;
    memcpy(cached.bytes, output, cached.length);
    highest_id = request->id;
  }
  queue_output(output, length + 1);
}

void radio_event(const ha_x2d::radio::TxEvent& event) {
  if (event.job.request_id == admitting_id && admitting_id) {
    admission_error = event.error ? event.error : "queue_full";
    return;  // rejected admission has a negative reply, no completion event
  }
  if (!event.job.request_id || !handshaken || output_failed ||
      event.job.connection_id != connection_id) return;
  JsonDocument doc;
  doc["v"] = ha_x2d::PROTOCOL_VERSION;
  doc["session"] = session;
  doc["seq"] = ++event_sequence;
  doc["event"] = "tx_result";
  doc["request_id"] = event.job.request_id;
  doc["shutter_id"] = event.job.shutter_id;
  doc["result"] = event.outcome;
  doc["completed_copies"] = event.completed_copies;
  if (event.error) doc["error"] = event.error;
  send(doc);
}

void error(const ha_x2d::Request& request, const char* reason, bool cache = false) {
  JsonDocument doc;
  doc["v"] = ha_x2d::PROTOCOL_VERSION;
  if (request.has_id) doc["id"] = request.id;
  else doc["id"] = nullptr;
  doc["ok"] = false;
  doc["error"] = reason;
  send(doc, cache ? &request : nullptr);
}

void generation(JsonObject object) {
  char value[17];
  if (journal.generation_hex(value)) object["generation"] = value;
  else object["generation"] = nullptr;
}

void shutter_record(JsonObject object, const ha_x2d::journal::Shutter& shutter) {
  object["shutter_id"] = shutter.shutter_id;
  object["state"] = shutter.state == ha_x2d::journal::SlotState::paired ? "paired" : "pending";
  switch (static_cast<ha_x2d::Action>(shutter.last_command)) {
    case ha_x2d::Action::open: object["last_command"] = "open"; break;
    case ha_x2d::Action::close: object["last_command"] = "close"; break;
    case ha_x2d::Action::stop: object["last_command"] = "stop"; break;
    default: object["last_command"] = nullptr;
  }
}

void respond(const ha_x2d::Request& request) {
  using ha_x2d::Operation;
  if (request.error) {
    if (handshaken) error(request, request.error);
    return;
  }
  if (request.op != Operation::hello) {
    if (!handshaken) return;
    if (strcmp(request.session, session)) { error(request, "stale_session"); return; }
  }
  for (const CachedReply& cached : replies) {
    if (!cached.length || cached.request.id != request.id) continue;
    if (cached.request.op != request.op || cached.request.shutter_id != request.shutter_id ||
        cached.request.action != request.action) { error(request, "duplicate_conflict"); return; }
    queue_output(cached.bytes, cached.length);
    return;
  }
  if (request.id <= highest_id) { error(request, "stale_request"); return; }

  JsonDocument doc;
  doc["v"] = ha_x2d::PROTOCOL_VERSION;
  doc["id"] = request.id;
  doc["ok"] = true;
  JsonObject result = doc["result"].to<JsonObject>();
  if (request.op == Operation::hello) {
    if (!handshaken) ++connection_id;
    handshaken = true;
    result["product"] = "ha-x2d";
    result["firmware"] = HA_X2D_SUPERVISED_TX ? "0.3.0-trial"
                         : HA_X2D_COMMANDS_TX ? "0.3.0-commands" : "0.3.0";
    result["device_id"] = device_id;
    result["session"] = session;
    result["max_line_bytes"] = ha_x2d::MAX_LINE_BYTES;
    result["max_shutters"] = ha_x2d::MAX_SHUTTERS;
    JsonArray caps = result["capabilities"].to<JsonArray>();
    caps.add("status"); caps.add("shutters");
    if (radio_configured) {
      caps.add("command");
      if (ENROLLMENT_ENABLED) {
        caps.add("provision"); caps.add("pair"); caps.add("confirm");
      }
    }
  } else if (request.op == Operation::status) {
    const auto radio = probe_radio();
    result["uptime_ms"] = millis();
    result["tx_enabled"] = radio_configured;
    JsonObject r = result["radio"].to<JsonObject>();
    r["detected"] = radio.detected;
    if (radio.detected) {
      r["partnum"] = radio.partnum; r["version"] = radio.version; r["marcstate"] = radio.marcstate;
    } else { r["partnum"] = nullptr; r["version"] = nullptr; r["marcstate"] = nullptr; }
    JsonObject storage = result["storage"].to<JsonObject>();
    switch (journal.state()) {
      case ha_x2d::journal::StorageState::empty: storage["state"] = "empty"; break;
      case ha_x2d::journal::StorageState::ready: storage["state"] = "ready"; break;
      case ha_x2d::journal::StorageState::full: storage["state"] = "full"; break;
      default: storage["state"] = "corrupt";
    }
    generation(storage);
  } else {
    if (journal.state() == ha_x2d::journal::StorageState::corrupt) {
      error(request, "storage_corrupt", true); return;
    }
    if (request.op == Operation::shutters) {
      generation(result);
      JsonArray slots = result["shutters"].to<JsonArray>();
      for (uint8_t i = 1; i <= ha_x2d::MAX_SHUTTERS; ++i) {
        const auto shutter = journal.shutter(i);
        if (shutter.state != ha_x2d::journal::SlotState::unused)
          shutter_record(slots.add<JsonObject>(), shutter);
      }
    } else if (request.op == Operation::provision &&
               journal.shutter(request.shutter_id).state != ha_x2d::journal::SlotState::unused) {
      generation(result);
      shutter_record(result, journal.shutter(request.shutter_id));
    } else if (request.op == Operation::command || request.op == Operation::pair) {
      if (!radio_configured || (request.op == Operation::pair && !ENROLLMENT_ENABLED)) {
        error(request, "profile_unverified", true); return;
      }
      if (request.op == Operation::pair) {
        uint32_t next;
        if (runtime.active() || runtime.pending()) { error(request, "maintenance_pending", true); return; }
        // Private trial: one attempt at the chosen next counter. The optional
        // C resume consumes 2/3; a reboot never restores consumed counters.
        if (request.shutter_id != 1 || !journal.next_counter(1, &next) ||
            next != HA_X2D_TRIAL_EXPECTED_NEXT_COUNTER) {
          error(request, "profile_unverified", true); return;
        }
      }
      ha_x2d::TxJob job;
      job.request_id = request.id;
      job.shutter_id = request.shutter_id;
      job.action = request.action;
      job.enrollment = request.op == Operation::pair;
      job.connection_id = connection_id;
      admitting_id = request.id;
      admission_error = nullptr;
      const bool accepted = runtime.submit(job, millis());
      admitting_id = 0;
      if (!accepted) { error(request, admission_error ? admission_error : "queue_full", true); return; }
      result["accepted"] = true;
      result["shutter_id"] = request.shutter_id;
    } else if (request.op == Operation::confirm) {
      if (!ENROLLMENT_ENABLED) { error(request, "profile_unverified", true); return; }
      if (runtime.active() || runtime.pending()) { error(request, "maintenance_pending", true); return; }
      const auto status = journal.confirm(request.shutter_id);
      if (status != ha_x2d::journal::Status::ok) {
        error(request, ha_x2d::journal::protocol_error(status), true); return;
      }
      generation(result);
      shutter_record(result, journal.shutter(request.shutter_id));
    } else if (request.op == Operation::provision && ENROLLMENT_ENABLED) {
      if (request.shutter_id != 1) { error(request, "profile_unverified", true); return; }
      if (runtime.active() || runtime.pending()) { error(request, "maintenance_pending", true); return; }
      // Seed 0 is an explicit supervised hypothesis, never production proof.
      // Verify the generated ID privately against the captured A/B before RF.
      ha_x2d::journal::NewController fresh;
      do {
        fresh.identity = ((rp2040.hwrand32() & 0xFFFF) << 8) | HA_X2D_TRIAL_SUFFIX;
      } while (!(fresh.identity & 0xFFFF00) || journal.find_identity(fresh.identity));
      fresh.first_counter = 0;
      do { fresh.generation = (uint64_t{rp2040.hwrand32()} << 32) | rp2040.hwrand32(); }
      while (!fresh.generation);
      const auto status = journal.provision(request.shutter_id, fresh);
      if (status != ha_x2d::journal::Status::ok) {
        error(request, ha_x2d::journal::protocol_error(status), true); return;
      }
      generation(result);
      shutter_record(result, journal.shutter(request.shutter_id));
    } else {
      // The default build neither allocates a radio identity nor emits RF.
      error(request, "profile_unverified", true); return;
    }
  }
  send(doc, &request);
}

}  // namespace

void setup() {
  pico_unique_board_id_t board_id;
  pico_get_unique_board_id(&board_id);
  hex_id(board_id.id, device_id);
  const uint64_t boot_random =
      (static_cast<uint64_t>(rp2040.hwrand32()) << 32) | rp2040.hwrand32();
  uint8_t boot_bytes[8];
  for (size_t i = 0; i < 8; ++i) boot_bytes[i] = boot_random >> (56 - 8 * i);
  hex_id(boot_bytes, session);

  USB.disconnect();
  USB.setManufacturer("ha-x2d");
  USB.setProduct("HA-X2D Gateway");
  USB.setSerialNumber(device_id);
  USB.connect();
  Serial.begin(115200);

  pinMode(PIN_CS, OUTPUT);
  digitalWrite(PIN_CS, HIGH);
  pinMode(PIN_MISO, INPUT);
  SPI.setRX(PIN_MISO);
  SPI.setTX(PIN_MOSI);
  SPI.setSCK(PIN_SCK);
  SPI.begin();
  reset_radio();
  journal.open();  // Read only; never format corrupt/unknown storage.
  radio_configured = configure_transmitter();
  runtime.set_enabled(radio_configured, ENROLLMENT_ENABLED);
}

void loop() {
  if (!Serial) {
    if (handshaken) { handshaken = false; runtime.disconnect(); }
    runtime.tick(millis());
    radio_output.service();
    input.reset();
    usb_output.clear();
    output_failed = false;
    handshaken = false;
    highest_id = 0;
    for (auto& cached : replies) cached.length = 0;
    while (Serial.available()) Serial.read();
    return;
  }
  flush_output();
  if (output_failed) {
    runtime.disconnect();
    runtime.tick(millis());
    radio_output.service();
    return;
  }
  for (unsigned budget = 0; budget < 256 && Serial.available(); ++budget) {
    const int value = Serial.read();
    if (value < 0) break;
    const auto event = input.feed(static_cast<char>(value));
    if (event == ha_x2d::LineFramer::Event::none) continue;
    if (event == ha_x2d::LineFramer::Event::too_long) {
      ha_x2d::Request request;
      request.error = "line_too_long";
      respond(request);
      continue;
    }
    const auto request = ha_x2d::decode(input.data(), input.length);
    respond(request);
    break;  // Service radio/output between complete request lines.
  }
  // ACK is queued before the runtime can produce its first terminal event.
  runtime.tick(millis());
  radio_output.service();
}
