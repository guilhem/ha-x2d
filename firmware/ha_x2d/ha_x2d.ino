#include <Arduino.h>
#include <SPI.h>
#include <USB.h>
#include <pico/unique_id.h>

#include "protocol.h"

namespace {

constexpr uint8_t PIN_MISO = 16;
constexpr uint8_t PIN_CS = 17;
constexpr uint8_t PIN_SCK = 18;
constexpr uint8_t PIN_MOSI = 19;
const SPISettings RADIO_SPI(4000000, MSBFIRST, SPI_MODE0);

ha_x2d::LineFramer input;
char output[ha_x2d::MAX_LINE_BYTES];
char device_id[17];
char session[17];

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

ha_x2d::RadioStatus probe_radio() {
  ha_x2d::RadioStatus result;
  uint8_t partnum, version, marcstate;
  if (!read_radio_status(0x30, partnum) ||
      !read_radio_status(0x31, version) ||
      !read_radio_status(0x35, marcstate)) return result;
  marcstate &= 0x1F;
  // The initial firmware leaves the chip in IDLE. Reject a floating SPI bus.
  if (partnum == 0 && (version == 0x04 || version == 0x14) && marcstate == 0x01) {
    result.detected = true;
    result.partnum = partnum;
    result.version = version;
    result.marcstate = marcstate;
  }
  return result;
}

void respond(ha_x2d::Request request, ha_x2d::RadioStatus radio = {}) {
  const size_t length = ha_x2d::encode(request, device_id, session, millis(),
                                       radio, output, sizeof(output));
  if (length) Serial.write(reinterpret_cast<const uint8_t*>(output), length);
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
}

void loop() {
  if (!Serial) {
    input.reset();
    while (Serial.available()) Serial.read();
    return;
  }
  while (Serial.available()) {
    const auto event = input.feed(static_cast<char>(Serial.read()));
    if (event == ha_x2d::LineFramer::Event::none) continue;
    if (event == ha_x2d::LineFramer::Event::too_long) {
      ha_x2d::Request request;
      request.error = "line_too_long";
      respond(request);
      continue;
    }
    const auto request = ha_x2d::decode(input.data(), input.length);
    respond(request, request.op == ha_x2d::Operation::status ? probe_radio()
                                                              : ha_x2d::RadioStatus{});
  }
}
