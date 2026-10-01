#pragma once

#include <stdint.h>
#include <stddef.h>
#include <string.h>

namespace rx_debug {

constexpr uint32_t SAMPLE_HZ = 400000;
constexpr size_t BLOCK_WORDS = 4096;
constexpr size_t BLOCK_BYTES = BLOCK_WORDS * 4;
constexpr unsigned BLOCK_RING_BITS = 14;
static_assert(BLOCK_BYTES == (1u << BLOCK_RING_BITS), "DMA ring must match each buffer");
constexpr size_t BLOCK_SAMPLES = BLOCK_BYTES * 4;
constexpr size_t CHUNK_BYTES = 1536;
constexpr size_t MAX_OUTPUT_BYTES = 4096;  // includes LF
constexpr size_t MAX_INPUT_BYTES = 512;  // debug commands stay separate from USB v2

struct Block {
  volatile bool ready = false;
  volatile uint32_t index = 0, offset = 0;  // offset in bytes
};

// DMA completion ISR owns publication; foreground accesses mask interrupts.
// Data buffers are separate: a hardware busy/count check must also protect copy.
struct Capture {
  Block blocks[2];
  volatile uint32_t next_block = 0, epoch = 0;
  volatile uint64_t completed_samples = 0, dropped_samples = 0;
  volatile uint32_t overrun_blocks = 0, gap_events = 0, copy_retries = 0;

  void discard(unsigned slot, bool overrun = false) {
    Block& block = blocks[slot];
    if (block.ready) {
      dropped_samples += (BLOCK_BYTES - block.offset) * 4;
      if (overrun) ++overrun_blocks;
    }
    block.ready = false;
  }

  void complete(unsigned slot) {
    discard(slot ^ 1, true);  // chained DMA immediately reuses its peer
    blocks[slot].index = next_block++;
    blocks[slot].offset = 0;
    blocks[slot].ready = true;
    completed_samples += BLOCK_SAMPLES;
  }

  void consume(unsigned slot, size_t bytes) {
    blocks[slot].offset += bytes;
    if (blocks[slot].offset == BLOCK_BYTES) blocks[slot].ready = false;
  }

  void new_epoch() {
    discard(0); discard(1);
    next_block = 0;
    ++epoch;
  }
};

inline bool copy_stable(bool busy_before, uint32_t count_before,
                        bool busy_after, uint32_t count_after, bool completion_pending = false) {
  // Reads expose the live counter (0 after completion), writes set RELOAD only.
  // A pending completion also catches a complete reuse during the masked copy.
  return !busy_before && !busy_after && count_before == 0 && count_after == 0 &&
         !completion_pending;
}

inline uint64_t partial_samples(bool was_busy, bool gap, uint32_t remaining) {
  return was_busy && !gap && remaining <= BLOCK_WORDS ? (BLOCK_WORDS - remaining) * 16 : 0;
}

// PIO shifts right: earliest sample is bits 1:0 of the first little-endian byte.
inline uint8_t sample_at(const uint8_t* bytes, size_t index) {
  return (bytes[index / 4] >> (2 * (index % 4))) & 3;
}

inline uint64_t sample_offset_us(uint64_t index, uint32_t divider_256, uint32_t clock_hz) {
  const uint64_t cycles = index * divider_256;
  const uint64_t denominator = static_cast<uint64_t>(clock_hz) * 256;
  return (cycles / denominator) * 1000000 + (cycles % denominator) * 1000000 / denominator;
}

inline void hex_bytes(const uint8_t* bytes, size_t length, char* output) {
  const char digits[] = "0123456789abcdef";
  for (size_t i = 0; i < length; ++i) {
    output[i * 2] = digits[bytes[i] >> 4];
    output[i * 2 + 1] = digits[bytes[i] & 15];
  }
  output[length * 2] = '\0';
}

struct Config {
  bool ook = false;
  uint32_t frequency_hz = 868350000;
  uint32_t deviation_hz = 38086;
};

enum class Command { invalid, status, stop, rx };

// Bounded decimal parser: rejects signs, fractions, overflow and extra tokens.
inline bool number(const char*& p, const char* end, uint32_t& value) {
  while (p != end && (*p == ' ' || *p == '\t')) ++p;
  value = 0;
  const char* start = p;
  while (p != end && *p >= '0' && *p <= '9') {
    const uint32_t digit = *p++ - '0';
    if (value > (UINT32_MAX - digit) / 10) return false;
    value = value * 10 + digit;
  }
  return p != start && (p == end || *p == ' ' || *p == '\t');
}

inline Command parse(const char* line, size_t length, Config& result) {
  if (length == 6 && memcmp(line, "status", 6) == 0) return Command::status;
  if (length == 4 && memcmp(line, "stop", 4) == 0) return Command::stop;
  if (length < 9 || memcmp(line, "rx ", 3) != 0) return Command::invalid;
  Config candidate;
  if (memcmp(line + 3, "ook ", 4) == 0) candidate.ook = true;
  else if (memcmp(line + 3, "fsk ", 4) != 0) return Command::invalid;
  const char* p = line + 7;
  const char* end = line + length;
  if (!number(p, end, candidate.frequency_hz) ||
      !number(p, end, candidate.deviation_hz)) return Command::invalid;
  while (p != end && (*p == ' ' || *p == '\t')) ++p;
  if (p != end || candidate.frequency_hz < 863000000 ||
      candidate.frequency_hz > 870000000 ||
      (candidate.ook ? candidate.deviation_hz != 0 :
       candidate.deviation_hz < 1000 || candidate.deviation_hz > 200000))
    return Command::invalid;
  result = candidate;
  return Command::rx;
}

// CC1101 datasheet equations, 26 MHz crystal; round to nearest available value.
inline uint32_t frequency_word(uint32_t hz) {
  return ((static_cast<uint64_t>(hz) << 16) + 13000000) / 26000000;
}

inline uint8_t deviation_register(uint32_t hz) {
  uint8_t best = 0;
  uint64_t distance = UINT64_MAX;
  const uint64_t target = static_cast<uint64_t>(hz) * 131072;
  for (uint8_t e = 0; e < 8; ++e) {
    for (uint8_t m = 0; m < 8; ++m) {
      const uint64_t value = static_cast<uint64_t>((8 + m) << e) * 26000000;
      const uint64_t delta = value > target ? value - target : target - value;
      if (delta < distance) { distance = delta; best = (e << 4) | m; }
    }
  }
  return best;
}

}  // namespace rx_debug
