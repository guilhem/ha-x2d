#pragma once

#include <Arduino.h>
#include <SPI.h>

namespace ha_x2d::rp2040 {

struct RadioBus {
  void begin_spi() { SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0)); }
  void end_spi() { SPI.endTransaction(); }
  void select(bool active) { digitalWrite(17, active ? LOW : HIGH); }
  bool miso_high() { return digitalRead(16) == HIGH; }
  uint8_t transfer(uint8_t value) { return SPI.transfer(value); }
  uint32_t now_us() { return micros(); }
  void delay_us(uint32_t value) { delayMicroseconds(value); }
  bool read_gpio(uint8_t pin) { return digitalRead(pin) == HIGH; }
  void data_write(bool high) { digitalWrite(20, high ? HIGH : LOW); }
  void data_output(bool output) { pinMode(20, output ? OUTPUT : INPUT); }
};

}  // namespace ha_x2d::rp2040
