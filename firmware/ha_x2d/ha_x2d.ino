#include <Arduino.h>
#include <SPI.h>
#include <USB.h>
#include <pico/unique_id.h>
#include <hardware/flash.h>
#include <hardware/sync.h>

#include <optional>

#include <x2d/cc1101.h>
#include "mysensors.h"
#include "ota_storage.h"
#include <x2d/journal.h>
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

// Public candidate for the demonstrated enrollment waveform. The release
// candidate still needs a physical association and command cycle on the motors.
// Runtime MySensors operations grant bounded attempts; no per-shutter build.
constexpr x2d::EnrollmentProfile ENROLLMENT_PROFILE{0x01};

class BoardFlash final : public x2d::journal::Flash {
 public:
  static constexpr uintptr_t ADDRESS = x2d::ota::JOURNAL_ADDRESS;
  uint32_t size() const override {
    return x2d::rp2040::valid_layout() &&
           ADDRESS + x2d::journal::REGION_BYTES == x2d::ota::STAGING_START
               ? x2d::journal::REGION_BYTES : 0;
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
x2d::journal::Journal journal(flash);

char device_id[17];  // the USB core keeps this pointer (setSerialNumber): never a local

x2d::rp2040::RadioBus radio_bus;
x2d::cc1101::Driver<x2d::rp2040::RadioBus, x2d::cc1101::Mode::gateway> radio(radio_bus);

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
  bool start_burst(const x2d::radio::Waveform& wave, uint32_t chip_ns,
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
  x2d::radio::FrameState poll_burst(uint8_t& completed) {
    using x2d::radio::digital_tx::State;
    const State state = digital_.poll();
    const uint32_t count = digital_.completed_frames() - burst_baseline_;
    if (count > burst_copies_) return x2d::radio::FrameState::unknown;
    completed = count;
    if (state == State::running || state == State::stopping)
      return x2d::radio::FrameState::busy;
    if (state == State::frame_boundary) return x2d::radio::FrameState::complete;
    radio.hold_data_low();  // suppress carrier before SPI cleanup
    return x2d::radio::FrameState::unknown;
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
  x2d::radio::digital_tx::Tx digital_;
  uint32_t burst_baseline_ = 0;
  uint8_t burst_copies_ = 0;
} radio_output;

// Runtime and association policy live in the shared controller. Only board
// entropy and the displayed firmware version belong in this adapter.
// Referenced as the sketch version so linker GC retains the complete product
// marker in every OTA-capable image (diagnostic sketches have no such marker).
const char FIRMWARE_ID[] = "HA-X2D YD-RP2040 OTA/1:0.6.0-rc1";
struct Policy {
  uint32_t random_u32() { return rp2040.hwrand32(); }
  const char* firmware() {
    return FIRMWARE_ID + sizeof(x2d::ota::IMAGE_MARKER) - 1;
  }
} policy;

using Gateway = x2d::mysensors::Gateway<RadioOutput, Policy>;
x2d::rp2040::OTAStorage ota_storage;
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
  radio.configure_transmitter();  // verifies the profile while remaining in IDLE
  gateway.emplace(journal, radio_output, policy, device_id, &ota_storage);
  gateway->begin(true, true,
                 x2d::radio::digital_tx::DEFAULT_CHIP_NS, ENROLLMENT_PROFILE);
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
  // Once a boot command is committed, connection loss cannot cancel it. Give
  // the host up to 3s to acknowledge receipt of ota_staged. An empty gateway
  // buffer only proves bytes entered USB, not that the host received them.
  static bool committed = false;
  static uint32_t committed_at = 0;
  if (gateway->ota_committed()) {
    if (!committed) { committed = true; committed_at = millis(); }
    if (gateway->reboot_requested() || millis() - committed_at >= 3000) {
      rp2040.reboot();
    }
  }
}
