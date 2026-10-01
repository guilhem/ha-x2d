// Native check, not part of the Arduino build (main guarded below).
#if !defined(ARDUINO)
#include <assert.h>
#include <stdio.h>
#include <vector>
#include "capture_check.h"

int main() {
  using namespace ha_x2d::radio;
  Body body;
  assert(make_body(0xF73192, 4, 6641, &body)); // public vector only
  body.length = MAX_BODY_BYTES;
  for (size_t i = BODY_BYTES; i < body.length; ++i) body.bytes[i] = 0xFF;
  static Waveform wave;
  assert(encode_burst(body, tx_check::COPIES, &wave));
  assert(wave.chip(wave.chips() - 1)); // final falling edge must be visible
  constexpr size_t count = 131072;
  std::vector<uint32_t> words(count / 32, 0);
  for (uint64_t gap_ns : {uint64_t{0}, uint64_t{100000}}) {
  for (uint64_t phase_ns = 0; phase_ns < 2500; phase_ns += 100) {
  std::fill(words.begin(), words.end(), 0);
  uint64_t start_ns = 100000 + phase_ns;
  size_t sample_at = 0;
  for (uint8_t frame = 0; frame < tx_check::COPIES; ++frame) {
    const size_t begin = frame ? wave.frame_end(frame - 1) : 0;
    const size_t end = wave.frame_end(frame);
    for (; sample_at * 2500 < start_ns; ++sample_at) {}
    for (; sample_at * 2500 < start_ns + (end - begin) * 208500; ++sample_at) {
      const size_t chip = begin + (sample_at * 2500 - start_ns) / 208500;
      if (wave.chip(chip)) words[sample_at / 32] |= 1u << (sample_at % 32);
    }
    start_ns += (end - begin) * 208500 + gap_ns;
  }
  auto result = tx_check::analyze(wave, words.data(), count, 2500, 208500);
  assert(!result.error && result.frames[0].end_edge_observed);
  assert(!result.frames[1].end_edge_observed && result.frames[2].end_edge_observed);
  const double expected_gap = gap_ns / 1000.;
  assert(result.frames[1].gap_us > expected_gap - 5 && result.frames[1].gap_us < expected_gap + 5);
  assert(result.frames[2].gap_us > expected_gap - 5 && result.frames[2].gap_us < expected_gap + 5);
  const size_t flip = static_cast<size_t>(result.frames[0].start_sample + 42);
  words[flip / 32] ^= 1u << (flip % 32);
  assert(tx_check::analyze(wave, words.data(), count, 2500, 208500).error);
  words[flip / 32] ^= 1u << (flip % 32);
  words.back() = 1;
  assert(tx_check::analyze(wave, words.data(), count, 2500, 208500).error);
  words.back() = 0;
  assert(tx_check::analyze(wave, words.data(), sample_at - 100, 2500, 208500).error);
  assert(tx_check::analyze(wave, words.data(), count, 2500, 209000).error);
  }
  }
  puts("tx_check: continuous and gapped samples, full final high, glitches and truncation OK");
}
#endif
