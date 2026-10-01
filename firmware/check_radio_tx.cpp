// Native execution of the actual PIO words (not a separate timing model).
#include <assert.h>
#include <stdio.h>
#include <vector>
#include "ha_x2d/radio_tx.h"
using namespace ha_x2d::radio;
namespace tx = ha_x2d::radio::digital_tx;
struct Marker { uint32_t word; size_t tick; };
struct Run {
  std::vector<bool> pins;
  std::vector<Marker> markers;
  size_t consumed = 0, outputs = 0;
  bool marker_lost = false;
};
static Run run(const uint32_t* words, size_t count, size_t available,
               size_t stop_tick = SIZE_MAX, bool drain = true) {
  Run result;
  uint32_t x = 0, y = 0, osr = 0, isr = 0;
  bool pin = false;
  size_t pc = 0;
  for (size_t steps = 0; steps < count * 20 + 64; ++steps) {
    assert(pc < sizeof(tx::PROGRAM) / sizeof(tx::PROGRAM[0]));
    const uint16_t op = tx::PROGRAM[pc];
    const unsigned major = op >> 13, arg = (op >> 5) & 7, value = op & 31;
    size_t next = pc + 1;
    if (major == 0) {
      bool jump = false;
      if (arg == 0) jump = true;
      else if (arg == 1) jump = x == 0;
      else if (arg == 3) jump = y == 0;
      else if (arg == 6) jump = result.pins.size() >= stop_tick;
      else assert(false);
      if (jump) next = value;
    } else if (major == 2) {
      assert(arg == 2 && value == 6);
      isr = (isr << 6) | (y & 63);
    } else if (major == 3) {
      const unsigned bits = value ? value : 32;
      const uint32_t data = bits == 32 ? osr : osr & ((1u << bits) - 1);
      osr = bits == 32 ? 0 : osr >> bits;
      if (arg == 0) { pin = data & 1; ++result.outputs; }
      else if (arg == 1) x = data;
      else assert(false);
    } else if (major == 4) {
      assert(!(op & 0x60)); // no conditional or blocking PUSH/PULL
      if (op & 0x80) {
        if (result.consumed < available) osr = words[result.consumed++];
        else osr = x; // RP2040 PULL NOBLOCK fallback
      } else {
        if (!drain && result.markers.size() == 4) result.marker_lost = true;
        else result.markers.push_back({isr, result.pins.size()});
        isr = 0; // PUSH clears ISR even if RX FIFO was full
      }
    } else if (major == 5) {
      const unsigned source = op & 7, operation = (op >> 3) & 3;
      uint32_t data = 0;
      if (source == 1) data = x;
      else if (source == 2) data = y;
      else assert(source == 3);
      if (operation == 1) data = ~data;
      else assert(operation == 0);
      if (arg == 6) isr = data;
      else if (arg == 2) y = data;
      else assert(false);
    } else if (major == 7) {
      if (arg == 0) pin = value & 1;
      else if (arg == 1) x = value;
      else if (arg == 2) y = value;
      else assert(false);
    } else assert(false);
    result.pins.insert(result.pins.end(), 1 + ((op >> 8) & 31), pin);
    if (next == pc) {
      result.pins.insert(result.pins.end(), 100, pin);
      return result;
    }
    pc = next;
  }
  assert(false);
  return result;
}
static void check_levels(const Waveform& wave, const Run& result, size_t chips) {
  for (size_t tick = 0; tick < chips * tx::CHIP_CYCLES; ++tick)
    assert(result.pins[2 + tick] == wave.chip(tick / tx::CHIP_CYCLES));
  for (size_t tick = 2 + chips * tx::CHIP_CYCLES; tick < result.pins.size(); ++tick)
    assert(!result.pins[tick]);
}
static void check_complete(const Waveform& wave, const Run& result, uint8_t frames) {
  assert(!result.marker_lost && result.markers.size() == frames);
  assert(result.consumed == wave.frame_end(frames - 1));
  assert(result.outputs == result.consumed); // no extra valid output/padding
  check_levels(wave, result, result.consumed);
  for (uint8_t frame = 0; frame < frames; ++frame) {
    const uint32_t expected = uint32_t{frame + 1u} |
        (frame + 1 == frames ? tx::TERMINAL_MASK : 0);
    assert(result.markers[frame].word == expected);
    // Marker after last chip's FULL period, even when that final chip is high.
    assert(result.markers[frame].tick == 2 + wave.frame_end(frame) * tx::CHIP_CYCLES + 3);
  }
}
static void check_missing(const Waveform& wave, const uint32_t* words, size_t missing) {
  const Run result = run(words, wave.chips(), missing);
  assert(result.consumed == missing && result.outputs == missing + 1);
  check_levels(wave, result, missing);
  size_t complete = 0;
  while (complete < wave.copies() && wave.frame_end(complete) <= missing) ++complete;
  assert(result.markers.size() == complete + 1);
  for (size_t i = 0; i < complete; ++i) {
    assert(result.markers[i].word == i + 1);
    assert(result.markers[i].tick == 2 + wave.frame_end(i) * tx::CHIP_CYCLES + 3);
  }
  assert(result.markers.back().word == tx::FAULT_MARKER);
}
static void check_wave(const Waveform& wave, bool exhaustive) {
  static uint32_t words[MAX_CHIPS];
  for (auto& word : words) word = 0xDEADBEEF;
  assert(tx::pack_burst(wave, words) == wave.chips());
  if (wave.chips() < MAX_CHIPS) assert(words[wave.chips()] == 0xDEADBEEF);
  check_complete(wave, run(words, wave.chips(), wave.chips()), wave.copies());
  for (uint8_t frame = 0; frame < wave.copies(); ++frame) {
    const size_t end = wave.frame_end(frame), tick = 2 + end * tx::CHIP_CYCLES;
    check_complete(wave, run(words, wave.chips(), wave.chips(), tick - 6), frame + 1);
    // A request after the latch sample waits for the next complete frame.
    const uint8_t later = frame + 1 < wave.copies() ? frame + 2 : frame + 1;
    check_complete(wave, run(words, wave.chips(), wave.chips(), tick - 4), later);
    check_missing(wave, words, end - 1);
    if (end < wave.chips()) check_missing(wave, words, end);
  }
  if (exhaustive)
    for (size_t missing = 0; missing < wave.chips(); ++missing) check_missing(wave, words, missing);
  if (wave.copies() > 4) {
    const auto lost = run(words, wave.chips(), wave.chips(), SIZE_MAX, false);
    assert(lost.marker_lost && lost.markers.size() == 4);
    // No terminal marker survived: software must fault, never guess 32 copies.
    assert(lost.markers.back().word == 4);
  }
}
int main() {
  static_assert(MAX_CHIPS * sizeof(uint32_t) == 41024, "review static RAM bound");
  static Waveform wave;
  Body body{};
  for (uint8_t length : {uint8_t{12}, uint8_t{13}, uint8_t{15}}) {
    body.length = length;
    for (uint8_t value : {uint8_t{0}, uint8_t{0xFF}, uint8_t{0x1F}, uint8_t{0x55}}) {
      for (auto& byte : body.bytes) byte = value;
      for (uint8_t copies : {uint8_t{1}, uint8_t{2}, uint8_t{32}}) {
        assert(encode_burst(body, copies, &wave));
        check_wave(wave, copies < 3);
      }
    }
  }
  for (bool level : {false, true})
    for (size_t chips : {size_t{1}, size_t{31}, size_t{32}, size_t{33}, MAX_CHIPS}) {
      wave.clear();
      for (size_t i = 0; i < chips; ++i) assert(wave.push(level));
      assert(wave.mark_frame_end());
      check_wave(wave, chips < 100);
    }
  static uint32_t words[MAX_CHIPS];
  wave.clear();
  assert(!tx::pack_burst(wave, words));
  assert(wave.push(true));
  assert(!tx::pack_burst(wave, words));
  wave.clear();
  assert(wave.mark_frame_end());
  assert(!tx::pack_burst(wave, words));
  assert(!tx::clock_divider_256(0, 208500));
  assert(!tx::clock_divider_256(125000000, 0));
  assert(!tx::clock_divider_256(125000000, 1));
  assert(!tx::clock_divider_256(UINT32_MAX, UINT32_MAX));
  for (uint32_t hz : {125000000u, 133000000u, 200000000u}) {
    const uint32_t divider = tx::clock_divider_256(hz, 208500);
    const double ns = double(divider) * tx::CHIP_CYCLES * 1e9 / (double(hz) * 256);
    assert(ns > 208499 && ns < 208501);
    assert(divider != tx::clock_divider_256(hz, 208000));
  }
  puts("radio_tx: actual PIO continuous 16-cycle chips; STOP all boundaries; full final period; underrun/marker loss; 15-byte/32-copy RAM bound OK");
}
