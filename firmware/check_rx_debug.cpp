#include "rx_debug/capture.h"
#include "protocol.h"

#include <assert.h>

int main() {
  rx_debug::Capture capture;
  capture.new_epoch();
  capture.complete(0);
  assert(capture.blocks[0].ready && capture.blocks[0].index == 0);
  capture.consume(0, rx_debug::CHUNK_BYTES);
  capture.complete(1);  // host is late: peer buffer is being overwritten
  assert(!capture.blocks[0].ready && capture.blocks[1].index == 1);
  assert(capture.overrun_blocks == 1 && capture.dropped_samples ==
      (rx_debug::BLOCK_BYTES - rx_debug::CHUNK_BYTES) * 4);
  capture.consume(1, rx_debug::BLOCK_BYTES);
  capture.complete(0);
  assert(capture.overrun_blocks == 1 && capture.completed_samples == rx_debug::BLOCK_SAMPLES * 3);
  capture.new_epoch();
  assert(!capture.blocks[0].ready && capture.next_block == 0 && capture.epoch == 2);
  assert(capture.dropped_samples == (rx_debug::BLOCK_BYTES * 2 - rx_debug::CHUNK_BYTES) * 4);

  // TRANS_COUNT reads live remaining (0 when complete), not the RELOAD value.
  assert(rx_debug::copy_stable(false, 0, false, 0));
  assert(!rx_debug::copy_stable(false, 0, true, rx_debug::BLOCK_WORDS));
  assert(!rx_debug::copy_stable(false, 0, false, rx_debug::BLOCK_WORDS));
  assert(!rx_debug::copy_stable(true, rx_debug::BLOCK_WORDS - 1, false, 0));
  assert(!rx_debug::copy_stable(false, 0, false, 0, true));
  assert(rx_debug::partial_samples(false, false, 0) == 0);  // completed ready block counted once
  assert(rx_debug::partial_samples(true, false, rx_debug::BLOCK_WORDS - 100) == 1600);
  assert(rx_debug::partial_samples(true, true, 0) == 0);  // ambiguous gap is not a guessed full block
  // Stop: queued loss plus the saved pre-abort count, without counting the idle peer.
  rx_debug::Capture stopped;
  stopped.complete(1);
  stopped.consume(1, 4608);  // HIL witness: 47,104 queued samples remain.
  const uint32_t remaining = 3072;  // snapshot before abort clears the hardware counter
  stopped.dropped_samples += rx_debug::partial_samples(true, false, remaining);
  stopped.dropped_samples += rx_debug::partial_samples(false, false, 0);
  stopped.discard(0); stopped.discard(1);
  assert(stopped.dropped_samples == 47104 + 16384);
  stopped.discard(0); stopped.discard(1);
  assert(stopped.dropped_samples == 47104 + 16384);
  alignas(rx_debug::BLOCK_BYTES) uint32_t buffers[2][rx_debug::BLOCK_WORDS]{};
  for (unsigned i = 0; i < 2; ++i) {
    const uintptr_t base = reinterpret_cast<uintptr_t>(buffers[i]);
    const uintptr_t mask = rx_debug::BLOCK_BYTES - 1;
    assert((base & mask) == 0);
    // Several unserviced DMA passes remain inside this buffer's hardware ring.
    for (size_t byte = 0; byte < rx_debug::BLOCK_BYTES * 3; byte += 4) {
      const uintptr_t address = (base & ~mask) | ((base + byte) & mask);
      assert(address >= base && address + 4 <= base + rx_debug::BLOCK_BYTES);
    }
  }
  uint32_t word = 0;
  for (unsigned i = 0; i < 16; ++i) word = (word >> 2) | ((i % 4) << 30);
  assert(word == 0xE4E4E4E4);
  uint8_t packed[4];
  for (unsigned i = 0; i < 4; ++i) packed[i] = word >> (i * 8);
  for (unsigned i = 0; i < 16; ++i) assert(rx_debug::sample_at(packed, i) == i % 4);
  char hex[9];
  rx_debug::hex_bytes(packed, sizeof(packed), hex);
  assert(strcmp(hex, "e4e4e4e4") == 0);  // includes carrier-low states 0 and 1
  assert(rx_debug::sample_offset_us(240000000, 85120, 133000000) == 600000000);
  assert(rx_debug::sample_offset_us(240000000, 128000, 200000000) == 600000000);
  assert(rx_debug::sample_offset_us(240000001, 85120, 133000000) == 600000002);
  assert(rx_debug::sample_offset_us(240000002, 85120, 133000000) == 600000005);

  ha_x2d::LineFramer framer(rx_debug::MAX_INPUT_BYTES);
  rx_debug::Config config;
  const char* command = "rx fsk 868350000 38086\r\n";
  for (const char* p = command; *p; ++p) framer.feed(*p);
  assert(rx_debug::parse(framer.data(), framer.length, config) == rx_debug::Command::rx);
  assert(!config.ook && config.frequency_hz == 868350000 && config.deviation_hz == 38086);
  assert(rx_debug::frequency_word(config.frequency_hz) == 0x2165E8);
  assert(rx_debug::frequency_word(868439941) == 0x2166CB);
  assert(rx_debug::deviation_register(38086) == 0x44);
  assert(rx_debug::deviation_register(19043) == 0x34);
  command = "rx ook 868439941 0";
  assert(rx_debug::parse(command, strlen(command), config) == rx_debug::Command::rx && config.ook);
  const char* invalid[] = {"rx fsk 868350000 0", "rx ook 868439941 1",
      "rx fsk 862999999 38086", "rx fsk 870000001 38086",
      "rx fsk 42949672960 38086", "rx fsk -1 38086", "rx fsk 868350000 38086 extra",
      "rx fsk 868350000", "rx ask 868350000 38086"};
  for (const char* value : invalid) {
    assert(rx_debug::parse(value, strlen(value), config) == rx_debug::Command::invalid);
    assert(config.ook && config.frequency_hz == 868439941);  // rejected input is inert
  }
  const char embedded[] = "rx fsk 868350000 38086\0extra";
  assert(rx_debug::parse(embedded, sizeof(embedded) - 1, config) == rx_debug::Command::invalid);
  assert(rx_debug::parse("status", 6, config) == rx_debug::Command::status);
  assert(rx_debug::parse("stop", 4, config) == rx_debug::Command::stop);
  for (size_t i = 0; i < 512; ++i) framer.feed('x');
  assert(framer.feed('\n') == ha_x2d::LineFramer::Event::too_long);
  for (const char* p = "status\n"; *p; ++p) framer.feed(*p);
  assert(rx_debug::parse(framer.data(), framer.length, config) == rx_debug::Command::status);

  // Largest payload plus conservative metadata space fits the declared limit.
  JsonDocument doc;
  doc["debug"] = 1;
  doc["type"] = "samples";
  doc["seq"] = UINT32_MAX;
  doc["config_id"] = UINT32_MAX;
  doc["loss"] = true;
  char payload[rx_debug::CHUNK_BYTES * 2 + 1];
  memset(payload, 'f', sizeof(payload) - 1);
  payload[sizeof(payload) - 1] = 0;
  doc["data_hex"] = payload;
  assert(measureJson(doc) + 800 <= rx_debug::MAX_OUTPUT_BYTES);
}
