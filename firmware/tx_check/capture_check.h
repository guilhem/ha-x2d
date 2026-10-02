#pragma once
#include <x2d/radio_codec.h>

namespace tx_check {
constexpr uint8_t COPIES = 3;

inline bool sample(const uint32_t* words, size_t index) {
  // PIO IN shifts right: first of each 32 samples ends up in bit zero.
  return (words[index / 32] >> (index % 32)) & 1;
}

struct Frame {
  double start_sample = 0, end_sample = 0, gap_us = 0;
  double chip_ns_min = 1e9, chip_ns_max = 0, chip_ns_sum = 0;
  double max_edge_error_ns = 0;
  uint32_t edges = 0;
  bool end_edge_observed = false;
};
struct Analysis {
  const char* error = nullptr;
  Frame frames[COPIES];
  size_t trailing_low_samples = 0;
};

// Match every expected transition, each chip center, the low gaps and tail.
// Edge tolerance is two capture samples. Both PIOs use the SAME clk_sys: this
// measures digital shape/relative timing, not the quartz's absolute accuracy.
inline Analysis analyze(const x2d::radio::Waveform& wave,
                        const uint32_t* words, size_t count,
                        double sample_ns, double chip_ns) {
  Analysis out;
  if (!words || count < 32 || wave.copies() != COPIES ||
      !(sample_ns > 0) || !(chip_ns > sample_ns * 4)) {
    out.error = "invalid_capture";
    return out;
  }
  const double period = chip_ns / sample_ns;
  size_t cursor = 0;
  double previous_end = 0;
  for (uint8_t frame = 0; frame < COPIES; ++frame) {
    Frame& result = out.frames[frame];
    const size_t begin = frame ? wave.frame_end(frame - 1) : 0;
    const size_t end = wave.frame_end(frame), chips = end - begin;
    if (end <= begin || end > wave.chips()) { out.error = "invalid_frame"; return out; }
    size_t first_high = 0;
    while (first_high < chips && !wave.chip(begin + first_high)) ++first_high;
    while (cursor < count && !sample(words, cursor)) ++cursor;
    if (!cursor || cursor == count || first_high == chips) {
      out.error = "missing_start_edge"; return out;
    }
    result.start_sample = double(cursor) - first_high * period;
    if (result.start_sample + 2 < previous_end || result.start_sample < 2) {
      out.error = "overlapping_frames"; return out;
    }
    result.gap_us = frame ? (result.start_sample - previous_end) * sample_ns / 1000 : 0;
    size_t edge = cursor, prior_chip = first_high;
    bool level = true;
    for (size_t chip = first_high + 1; chip <= chips; ++chip) {
      const bool next_level = chip < chips && wave.chip(begin + chip);
      if (next_level == level) continue;
      size_t next_edge = edge + 1;
      while (next_edge < count && sample(words, next_edge) == level) ++next_edge;
      if (next_edge == count) { out.error = "missing_edge"; return out; }
      double error = (double(next_edge) - (result.start_sample + chip * period)) * sample_ns;
      if (error < 0) error = -error;
      if (error > result.max_edge_error_ns) result.max_edge_error_ns = error;
      if (error > sample_ns * 2) { out.error = "edge_timing_mismatch"; return out; }
      const double measured = (next_edge - edge) * sample_ns / (chip - prior_chip);
      if (measured < result.chip_ns_min) result.chip_ns_min = measured;
      if (measured > result.chip_ns_max) result.chip_ns_max = measured;
      result.chip_ns_sum += measured;
      ++result.edges;
      edge = next_edge;
      prior_chip = chip;
      level = next_level;
      if (chip == chips) {
        result.end_edge_observed = true;
        result.end_sample = next_edge;
      }
    }
    const double predicted_end = result.start_sample + chips * period;
    if (predicted_end + 2 >= double(count)) { out.error = "truncated_frame"; return out; }
    for (size_t chip = 0; chip < chips; ++chip) {
      const size_t center = static_cast<size_t>(result.start_sample + (chip + 0.5) * period);
      if (sample(words, center) != wave.chip(begin + chip)) {
        out.error = "chip_mismatch"; return out;
      }
    }
    if (!result.end_edge_observed) {
      result.end_sample = predicted_end;  // low last chip merges with low gap
      // A predicted boundary can round into the next copy by up to two
      // samples. Leave that window to the next copy's measured start edge.
      for (size_t index = edge; double(index) < predicted_end - 2; ++index)
        if (sample(words, index)) { out.error = "extra_edge"; return out; }
    }
    previous_end = result.end_sample;
    cursor = static_cast<size_t>(previous_end - (result.end_edge_observed ? 0 : 2));
  }
  out.trailing_low_samples = count - cursor;
  if (out.trailing_low_samples < 32) { out.error = "missing_low_tail"; return out; }
  for (; cursor < count; ++cursor)
    if (sample(words, cursor)) { out.error = "high_after_final_frame"; return out; }
  return out;
}
}  // namespace tx_check
