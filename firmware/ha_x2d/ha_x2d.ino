#include <Arduino.h>
#include <SPI.h>
#include <USB.h>
#include <pico/unique_id.h>
#include <hardware/flash.h>
#include <hardware/sync.h>

#include <optional>

#include <cc1101.h>
#include <mysensors.h>
#include <journal.h>
#include "radio_bus.h"
#include "radio_tx.h"

// RP2040 adapter for the portable x2d-core MySensors adapter: USB CDC bytes, the reserved
// flash journal region, the CC1101 bus and PIO/DMA transmitter, identity and
// entropy. Protocol, journal rules and scheduling live in the library.

extern uint8_t _FS_start, _FS_end;

namespace {

constexpr uint8_t PIN_MISO = 16;
constexpr uint8_t PIN_CS = 17;
constexpr uint8_t PIN_SCK = 18;
constexpr uint8_t PIN_MOSI = 19;

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

char device_id[17];  // the USB core keeps this pointer (setSerialNumber): never a local

ha_x2d::rp2040::RadioBus radio_bus;
ha_x2d::cc1101::Driver<ha_x2d::rp2040::RadioBus, ha_x2d::cc1101::Mode::gateway> radio(radio_bus);

void hex_id(const uint8_t* bytes, char* destination) {
  const char digits[] = "0123456789ABCDEF";
  for (size_t i = 0; i < 8; ++i) {
    destination[i * 2] = digits[bytes[i] >> 4];
    destination[i * 2 + 1] = digits[bytes[i] & 15];
  }
  destination[16] = '\0';
}

// Nonblocking PIO/DMA burst backend for the runtime. radio.configured() is the
// single verified-transmitter gate; any CC1101 or PIO fault clears it.
class RadioOutput {
 public:
  bool available() const { return radio.configured(); }
  bool start_burst(const ha_x2d::radio::Waveform& wave, uint32_t chip_ns,
                   uint32_t& started_ms) {
    // begin_tx verifies async OOK/no packet engine before it takes the data pin.
    if (!radio.begin_tx()) return false;
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
    radio.hold_data_low();  // suppress carrier before SPI cleanup
    return ha_x2d::radio::FrameState::unknown;
  }
  void request_stop() { digital_.request_stop(); }
  void end_burst() {
    // Idle radio BEFORE releasing the data pin; even a partial DMA fault
    // cannot leave a carrier transmitting from a floating input.
    const bool idle = radio.idle();
    digital_.abort(idle);
    if (idle) radio.release_data();
    else radio.hold_data_low();
  }
  void service() { digital_.poll(); }
 private:
  ha_x2d::radio::digital_tx::Tx digital_;
  uint32_t burst_baseline_ = 0;
  uint8_t burst_copies_ = 0;
} radio_output;

// Runtime and association policy live in the shared controller. Only board
// entropy and the displayed firmware version belong in this adapter.
struct Policy {
  uint32_t random_u32() { return rp2040.hwrand32(); }
  const char* firmware() {
    return HA_X2D_SUPERVISED_TX ? "0.4.0-trial" : HA_X2D_COMMANDS_TX ? "0.4.0-commands" : "0.4.0";
  }
} policy;

using Gateway = ha_x2d::mysensors::Gateway<RadioOutput, Policy>;
std::optional<Gateway> gateway;  // identity exists only after boot
char rx[256];                    // one small USB window; the gateway frames lines
size_t rx_used = 0, rx_pos = 0;

void flush_output() {
  const int available = Serial.availableForWrite();
  if (available <= 0 || !gateway->output_size()) return;
  const size_t count = gateway->output_contiguous() < static_cast<size_t>(available)
                           ? gateway->output_contiguous() : static_cast<size_t>(available);
  gateway->consume_output(Serial.write(reinterpret_cast<const uint8_t*>(gateway->output_data()), count));
}

}  // namespace

void setup() {
  pico_unique_board_id_t board_id;
  pico_get_unique_board_id(&board_id);
  hex_id(board_id.id, device_id);

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
  radio.reset();  // CC1101 manual reset; no frequency or transmit registers are set
  if (TX_ENABLED) radio.configure_transmitter();
  gateway.emplace(journal, radio_output, policy, device_id);
  const ha_x2d::PairingAuthorization trial{
      HA_X2D_SUPERVISED_TX ? uint8_t{1} : uint8_t{0},
      HA_X2D_TRIAL_SUFFIX, HA_X2D_TRIAL_EXPECTED_NEXT_COUNTER};
  gateway->begin(TX_ENABLED, ENROLLMENT_ENABLED,
                 ha_x2d::radio::digital_tx::DEFAULT_CHIP_NS, trial);
}

void loop() {
  if (!Serial) {
    gateway->disconnected();
    rx_used = rx_pos = 0;
    for (size_t i = 0; i < sizeof(rx) && Serial.available(); ++i) Serial.read();
  } else {
    gateway->connected();
    flush_output();
    if (!gateway->failed()) {
      if (rx_pos == rx_used) {
        rx_used = rx_pos = 0;
        while (rx_used < sizeof(rx) && Serial.available()) {
          const int value = Serial.read();
          if (value < 0) break;
          rx[rx_used++] = static_cast<char>(value);
        }
      }
      // One request line per pass: radio and output are serviced between lines.
      if (rx_pos < rx_used) rx_pos += gateway->feed(rx + rx_pos, rx_used - rx_pos);
    }
  }
  // ACK is queued before the runtime can produce its first terminal event.
  gateway->tick(millis());
  radio_output.service();
}
