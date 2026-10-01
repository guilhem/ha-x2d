#include <Arduino.h>
#include <SPI.h>
#include <USB.h>
#include <pico/unique_id.h>
#include <hardware/sync.h>
#include <hardware/pio.h>
#include <hardware/dma.h>
#include <hardware/clocks.h>
#include <hardware/irq.h>
#include <pico/time.h>

#include "protocol.h"  // shared diagnostic framer and pinned ArduinoJson
#include "capture.h"

namespace {

constexpr uint8_t MISO_PIN = 16, CS_PIN = 17, SCK_PIN = 18, MOSI_PIN = 19;
constexpr uint8_t DATA_PIN = 20, CARRIER_PIN = 21;
const SPISettings RADIO_SPI(4000000, MSBFIRST, SPI_MODE0);
// Only these radio strobes can be issued. No transmit strobe or FIFO writes.
enum class Strobe : uint8_t { reset = 0x30, calibrate = 0x33, rx = 0x34, idle = 0x36 };

rx_debug::Capture capture;
rx_debug::Config config;
ha_x2d::LineFramer input(rx_debug::MAX_INPUT_BYTES);
char output[rx_debug::MAX_OUTPUT_BYTES];
size_t output_size = 0, output_sent = 0;
uint32_t pending_samples = 0, sequence = 0, config_id = 0;
uint32_t spi_errors = 0, output_errors = 0, serial_read_errors = 0;
uint64_t reported_dropped = 0;
uint32_t reported_gaps = 0, chunk_sequence = 0;
uint32_t last_status_ms = 0;
bool receiving = false, connected = false, detected = false;
bool host_ready = false;
bool data_wire_ok = false, carrier_wire_ok = false;
char device_id[17], session[17];
PIO sample_pio = pio0;
int sample_sm = -1, dma_channels[2] = {-1, -1};
uint program_offset = 0;
alignas(rx_debug::BLOCK_BYTES) uint32_t dma_buffers[2][rx_debug::BLOCK_WORDS];
uint32_t system_clock_hz = 0, divider_256 = 0;
uint64_t epoch_start_us = 0;
volatile bool capture_fault = false, sampling = false;
volatile bool dma_armed = false;
bool sampler_ready = false;
volatile uint32_t expected_dma = 0, pio_stalls = 0, dma_chain_stalls = 0;

void hex_id(const uint8_t* bytes, char* dest) {
  const char digits[] = "0123456789ABCDEF";
  for (size_t i = 0; i < 8; ++i) {
    dest[i * 2] = digits[bytes[i] >> 4];
    dest[i * 2 + 1] = digits[bytes[i] & 15];
  }
  dest[16] = '\0';
}

bool begin_radio() {
  SPI.beginTransaction(RADIO_SPI);
  digitalWrite(CS_PIN, LOW);
  const uint32_t start = micros();
  while (digitalRead(MISO_PIN)) {
    if (static_cast<uint32_t>(micros() - start) >= 2000) {
      digitalWrite(CS_PIN, HIGH);
      SPI.endTransaction();
      ++spi_errors;
      return false;
    }
  }
  return true;
}

void end_radio() { digitalWrite(CS_PIN, HIGH); SPI.endTransaction(); }

bool strobe(Strobe command) {
  if (!begin_radio()) return false;
  SPI.transfer(static_cast<uint8_t>(command));
  end_radio();
  return true;
}

bool read_register(uint8_t address, uint8_t& value, bool status = false) {
  if (!begin_radio()) return false;
  SPI.transfer(address | (status ? 0xC0 : 0x80));
  value = SPI.transfer(0);
  end_radio();
  return true;
}

bool write_register(uint8_t address, uint8_t value) {
  if (!begin_radio()) return false;
  SPI.transfer(address);
  SPI.transfer(value);
  end_radio();
  uint8_t observed;
  return read_register(address, observed) && observed == value;
}

bool wait_state(uint8_t expected) {
  const uint32_t start = micros();
  do {
    uint8_t state;
    if (!read_register(0x35, state, true)) return false;
    if ((state & 0x1F) == expected) return true;
    delayMicroseconds(100);
  } while (static_cast<uint32_t>(micros() - start) < 20000);
  return false;
}

uint32_t dma_mask() { return (1u << dma_channels[0]) | (1u << dma_channels[1]); }

bool pio_stalled() {
  return sample_pio->fdebug & (1u << (PIO_FDEBUG_RXSTALL_LSB + sample_sm));
}

void stop_sampler();

void mark_capture_gap(bool stalled) {
  if (capture_fault) return;
  capture_fault = true;
  sampling = false;
  ++capture.gap_events;
  if (stalled) ++pio_stalls;
  else ++dma_chain_stalls;
  stop_sampler();  // stop both channels now; gap losses have unknown extent
}

void dma_complete() {
  const uint32_t flags = dma_hw->ints0 & dma_mask();
  if (!flags) return;
  dma_hw->ints0 = flags;
  if (!sampling) return;
  const unsigned slot = expected_dma;
  const bool stalled = pio_stalled();
  if (stalled || flags != (1u << dma_channels[slot]) ||
      !dma_channel_is_busy(dma_channels[slot ^ 1])) {
    mark_capture_gap(stalled);
    return;
  }
  capture.complete(slot);
  dma_channel_set_write_addr(dma_channels[slot], dma_buffers[slot], false);
  dma_channel_set_trans_count(dma_channels[slot], rx_debug::BLOCK_WORDS, false);
  expected_dma ^= 1;
}

bool init_sampler() {
  sample_sm = pio_claim_unused_sm(sample_pio, false);
  if (sample_sm < 0) return false;
  const uint16_t instruction = pio_encode_in(pio_pins, 2);
  const pio_program program = {&instruction, 1, -1};
  if (!pio_can_add_program(sample_pio, &program)) return false;
  program_offset = pio_add_program(sample_pio, &program);
  for (unsigned i = 0; i < 2; ++i) {
    dma_channels[i] = dma_claim_unused_channel(false);
    if (dma_channels[i] < 0) return false;
  }
  pio_gpio_init(sample_pio, DATA_PIN);
  pio_gpio_init(sample_pio, CARRIER_PIN);
  pio_sm_set_consecutive_pindirs(sample_pio, sample_sm, DATA_PIN, 2, false);
  system_clock_hz = clock_get_hz(clk_sys);
  divider_256 = (static_cast<uint64_t>(system_clock_hz) * 256 + rx_debug::SAMPLE_HZ / 2) /
                rx_debug::SAMPLE_HZ;
  pio_sm_config cfg = pio_get_default_sm_config();
  sm_config_set_wrap(&cfg, program_offset, program_offset);
  sm_config_set_in_pins(&cfg, DATA_PIN);
  sm_config_set_in_shift(&cfg, true, true, 32);
  sm_config_set_fifo_join(&cfg, PIO_FIFO_JOIN_RX);
  sm_config_set_clkdiv_int_frac(&cfg, divider_256 >> 8, divider_256 & 255);
  pio_sm_init(sample_pio, sample_sm, program_offset, &cfg);
  for (unsigned i = 0; i < 2; ++i) {
    dma_channel_config dma_cfg = dma_channel_get_default_config(dma_channels[i]);
    channel_config_set_transfer_data_size(&dma_cfg, DMA_SIZE_32);
    channel_config_set_read_increment(&dma_cfg, false);
    channel_config_set_write_increment(&dma_cfg, true);
    // Hardware bound remains valid even if completion IRQs are delayed.
    channel_config_set_ring(&dma_cfg, true, rx_debug::BLOCK_RING_BITS);
    channel_config_set_dreq(&dma_cfg, pio_get_dreq(sample_pio, sample_sm, false));
    channel_config_set_chain_to(&dma_cfg, dma_channels[i ^ 1]);
    dma_channel_configure(dma_channels[i], &dma_cfg, dma_buffers[i],
                          &sample_pio->rxf[sample_sm], rx_debug::BLOCK_WORDS, false);
  }
  irq_add_shared_handler(DMA_IRQ_0, dma_complete, PICO_SHARED_IRQ_HANDLER_DEFAULT_ORDER_PRIORITY);
  irq_set_enabled(DMA_IRQ_0, true);
  sampler_ready = true;
  return true;
}

void stop_sampler() {
  if (!sampler_ready) return;
  const uint32_t saved = save_and_disable_interrupts();
  sampling = false;
  pio_sm_set_enabled(sample_pio, sample_sm, false);
  // Pause both chains before taking a conservative snapshot of completed words.
  for (int channel : dma_channels) {
    dma_channel_set_irq0_enabled(channel, false);
    hw_clear_bits(&dma_channel_hw_addr(channel)->al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
  }
  const bool was_busy[] = {dma_channel_is_busy(dma_channels[0]),
                           dma_channel_is_busy(dma_channels[1])};
  const uint32_t remaining[] = {dma_channel_hw_addr(dma_channels[0])->transfer_count,
                                dma_channel_hw_addr(dma_channels[1])->transfer_count};
  const bool pending = (dma_hw->intr & dma_mask()) != 0;
  const bool gap = capture_fault || pending;
  if (dma_armed && pending && !capture_fault) ++capture.gap_events;
  // CHAN_ABORT clears TRANS_COUNT; ignore abort completion IRQs (RP2040-E13).
  for (int channel : dma_channels) dma_channel_abort(channel);
  if (dma_armed) {
    // Completed words in an unfinished/unpublished DMA block are known losses.
    for (unsigned i = 0; i < 2; ++i)
      capture.dropped_samples += rx_debug::partial_samples(was_busy[i], gap, remaining[i]);
    dma_armed = false;
  }
  dma_hw->ints0 = dma_mask();
  capture.discard(0); capture.discard(1);
  restore_interrupts(saved);
}

void start_sampler() {
  stop_sampler();
  const uint32_t saved = save_and_disable_interrupts();
  capture.new_epoch();
  capture_fault = false;
  expected_dma = 0;
  pio_sm_clear_fifos(sample_pio, sample_sm);
  pio_sm_restart(sample_pio, sample_sm);
  pio_sm_clkdiv_restart(sample_pio, sample_sm);
  pio_sm_exec(sample_pio, sample_sm, pio_encode_jmp(program_offset));
  sample_pio->fdebug = 1u << (PIO_FDEBUG_RXSTALL_LSB + sample_sm);
  for (unsigned i = 0; i < 2; ++i) {
    dma_channel_set_write_addr(dma_channels[i], dma_buffers[i], false);
    dma_channel_set_trans_count(dma_channels[i], rx_debug::BLOCK_WORDS, false);
    // AL1_CTRL changes EN without triggering; only channel A is started below.
    hw_set_bits(&dma_channel_hw_addr(dma_channels[i])->al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    dma_channel_set_irq0_enabled(dma_channels[i], true);
  }
  sampling = true;
  dma_armed = true;
  dma_start_channel_mask(1u << dma_channels[0]);
  // Host timestamps are approximate; sample intervals use the PIO clock/divider.
  epoch_start_us = time_us_64();
  pio_sm_set_enabled(sample_pio, sample_sm, true);
  restore_interrupts(saved);
}

bool stop_radio() {
  stop_sampler();
  receiving = false;
  return strobe(Strobe::idle) && wait_state(1);
}

bool start_radio(const rx_debug::Config& candidate) {
  if (!stop_radio() || !detected || !sampler_ready) return false;
  const uint32_t freq = rx_debug::frequency_word(candidate.frequency_hz);
  // TI SWRS061I asynchronous RX; DN022 narrow-band receive tuning.
  // Pin radio control, sync, Manchester, whitening, CRC and FEC are disabled.
  const uint8_t common[][2] = {
      {0x00, 0x0E}, {0x01, 0x2E}, {0x02, 0x0D}, {0x03, 0x47},
      {0x07, 0x00}, {0x08, 0x30}, {0x0A, 0x00}, {0x0B, 0x06}, {0x0C, 0x00},
      {0x0D, static_cast<uint8_t>(freq >> 16)},
      {0x0E, static_cast<uint8_t>(freq >> 8)}, {0x0F, static_cast<uint8_t>(freq)},
      {0x13, 0x00}, {0x16, 0x07}, {0x17, 0x3C}, {0x18, 0x08},
      {0x21, 0xB6}, {0x2C, 0x81}, {0x2D, 0x35}};
  for (const auto& pair : common)
    if (!write_register(pair[0], pair[1])) return false;
  const uint8_t modem[][2] = {
      {0x10, static_cast<uint8_t>(candidate.ook ? 0x87 : 0x7A)},
      {0x11, static_cast<uint8_t>(candidate.ook ? 0x85 : 0x93)},
      {0x12, static_cast<uint8_t>(candidate.ook ? 0x30 : 0x00)},
      {0x15, candidate.ook ? static_cast<uint8_t>(0) :
                          rx_debug::deviation_register(candidate.deviation_hz)},
      {0x19, static_cast<uint8_t>(candidate.ook ? 0x14 : 0x16)},
      {0x1B, static_cast<uint8_t>(candidate.ook ? 0x04 : 0x03)},
      {0x1C, static_cast<uint8_t>(candidate.ook ? 0x00 : 0x40)},
      {0x1D, static_cast<uint8_t>(candidate.ook ? 0x92 : 0x91)}};
  for (const auto& pair : modem)
    if (!write_register(pair[0], pair[1])) return false;
  if (!strobe(Strobe::calibrate) || !wait_state(1) ||
      !strobe(Strobe::rx) || !wait_state(0x0D)) {
    strobe(Strobe::idle);
    return false;
  }
  config = candidate;
  ++config_id;
  receiving = true;
  start_sampler();
  return true;
}

void base_document(JsonDocument& doc, const char* type) {
  doc["debug"] = 1;
  doc["type"] = type;
  doc["seq"] = ++sequence;
  doc["config_id"] = config_id;
}

void queue_document(JsonDocument& doc) {
  const size_t bytes = measureJson(doc);
  if (bytes + 1 > sizeof(output)) {
    ++output_errors;
    return;
  }
  serializeJson(doc, output, sizeof(output));
  output[bytes] = '\n';
  output_size = bytes + 1;
  output_sent = 0;
  pending_samples = 0;
}

void status_document(JsonDocument& doc, const char* type) {
  base_document(doc, type);
  doc["product"] = "ha-x2d-rx-debug";
  doc["firmware"] = "rx-debug-0.2.2";
  doc["device_id"] = device_id;
  doc["session"] = session;
  doc["tx_enabled"] = false;
  doc["rx_enabled"] = receiving;
  doc["uptime_ms"] = millis();
  doc["max_input_bytes"] = rx_debug::MAX_INPUT_BYTES;
  doc["max_output_bytes"] = sizeof(output);
  doc["buffer_bytes"] = rx_debug::BLOCK_BYTES * 2;
  doc["block_samples"] = rx_debug::BLOCK_SAMPLES;
  doc["timing"] = "pio-in-pins-2-dma";
  doc["system_clock_hz"] = system_clock_hz;
  doc["divider_256"] = divider_256;
  doc["sample_hz"] = divider_256 ? system_clock_hz * 256.0 / divider_256 : 0;
  doc["sampler_ready"] = sampler_ready;
  JsonObject cfg = doc["config"].to<JsonObject>();
  cfg["mode"] = config.ook ? "ook" : "fsk";
  cfg["frequency_hz"] = config.frequency_hz;
  cfg["actual_frequency_hz"] = rx_debug::frequency_word(config.frequency_hz) * (26000000.0 / 65536);
  cfg["deviation_hz"] = config.deviation_hz;
  const uint8_t dev = rx_debug::deviation_register(config.deviation_hz);
  cfg["actual_deviation_hz"] = config.ook ? 0 : ((8 + (dev & 7)) << (dev >> 4)) * (26000000.0 / 131072);
  cfg["crystal_hz_assumed"] = 26000000;
  cfg["bitrate_bps"] = (256 + (config.ook ? 0x85 : 0x93)) *
      (1 << (config.ook ? 7 : 10)) * (26000000.0 / 268435456);
  cfg["bandwidth_hz"] = config.ook ? 203125 : 232142.857;
  JsonObject counters = doc["counters"].to<JsonObject>();
  const uint32_t saved = save_and_disable_interrupts();
  const uint64_t completed = capture.completed_samples, dropped = capture.dropped_samples;
  const uint32_t values[] = {capture.overrun_blocks, capture.gap_events,
      capture.copy_retries, pio_stalls, dma_chain_stalls, capture.epoch};
  restore_interrupts(saved);
  counters["completed_samples"] = completed;
  counters["dropped_samples"] = dropped;
  const char* keys[] = {"overrun_blocks", "gap_events", "copy_retries", "pio_stalls", "dma_chain_stalls", "capture_epoch"};
  for (size_t i = 0; i < 6; ++i) counters[keys[i]] = values[i];
  counters["spi_errors"] = spi_errors;
  counters["output_errors"] = output_errors;
  counters["serial_read_errors"] = serial_read_errors;
  doc["gdo0"] = digitalRead(DATA_PIN);
  doc["gdo2"] = digitalRead(CARRIER_PIN);
  doc["gdo0_wiring_ok"] = data_wire_ok;
  doc["gdo2_wiring_ok"] = carrier_wire_ok;
  JsonObject radio = doc["radio"].to<JsonObject>();
  bool ok = true;
  const uint8_t addresses[] = {0x30, 0x31, 0x35, 0x34, 0x32, 0x38};
  const char* names[] = {"partnum", "version", "marcstate", "rssi_raw", "freqest_raw", "pktstatus"};
  for (size_t i = 0; i < sizeof(addresses); ++i) {
    uint8_t value;
    if (read_register(addresses[i], value, true)) {
      radio[names[i]] = i == 2 ? value & 0x1F : value;
      if (i == 3) radio["rssi_dbm_approx"] = static_cast<int8_t>(value) / 2.0 - 74;
    } else { radio[names[i]] = nullptr; ok = false; }
  }
  JsonArray registers = radio["registers_00_2e"].to<JsonArray>();
  for (uint8_t i = 0; i <= 0x2E; ++i) {
    uint8_t value;
    if (read_register(i, value)) registers.add(value);
    else { registers.add(nullptr); ok = false; }
  }
  radio["spi_ok"] = ok;
  radio["detected"] = detected;
}

void send_status(const char* type = "status") {
  host_ready = true;
  JsonDocument doc;
  status_document(doc, type);
  queue_document(doc);
  last_status_ms = millis();
}

void send_error(const char* error, bool command_diagnostic = false) {
  JsonDocument doc;
  base_document(doc, "error");
  doc["error"] = error;
  if (command_diagnostic) {
    char bytes[rx_debug::MAX_INPUT_BYTES * 2 - 1];
    rx_debug::hex_bytes(reinterpret_cast<const uint8_t*>(input.data()), input.length, bytes);
    doc["command_length"] = input.length;
    doc["command_bytes"] = bytes;
  }
  queue_document(doc);
}

void send_samples() {
  uint8_t bytes[rx_debug::CHUNK_BYTES];
  const uint32_t saved = save_and_disable_interrupts();
  if (pio_stalled()) mark_capture_gap(true);
  const unsigned slot = capture.blocks[0].ready ? 0 : 1;
  if (!sampling || !capture.blocks[slot].ready) { restore_interrupts(saved); return; }
  const uint32_t block = capture.blocks[slot].index, offset = capture.blocks[slot].offset;
  const size_t remaining = rx_debug::BLOCK_BYTES - offset;
  const size_t count = remaining < sizeof(bytes) ? remaining : sizeof(bytes);
  const bool busy_before = dma_channel_is_busy(dma_channels[slot]);
  const uint32_t count_before = dma_channel_hw_addr(dma_channels[slot])->transfer_count;
  const uint32_t flags_before = dma_hw->intr & dma_mask();
  if (!busy_before && count_before == 0 && !flags_before)
    memcpy(bytes, reinterpret_cast<const uint8_t*>(dma_buffers[slot]) + offset, count);
  __dmb();
  const bool stable = rx_debug::copy_stable(busy_before, count_before,
      dma_channel_is_busy(dma_channels[slot]), dma_channel_hw_addr(dma_channels[slot])->transfer_count,
      flags_before || (dma_hw->intr & dma_mask()));
  if (!stable) { ++capture.copy_retries; restore_interrupts(saved); return; }
  capture.consume(slot, count);
  const uint32_t epoch = capture.epoch, gaps = capture.gap_events, overruns = capture.overrun_blocks;
  const uint64_t dropped = capture.dropped_samples, start_us = epoch_start_us;
  restore_interrupts(saved);
  const uint64_t index = static_cast<uint64_t>(block) * rx_debug::BLOCK_SAMPLES + offset * 4;
  const uint64_t sample_us = start_us + rx_debug::sample_offset_us(index, divider_256, system_clock_hz);
  const int header = snprintf(output, sizeof(output),
      "{\"debug\":1,\"type\":\"samples\",\"seq\":%lu,\"config_id\":%lu,\"chunk\":%lu,"
      "\"capture_epoch\":%lu,\"block\":%lu,\"byte_offset\":%lu,\"sample_index\":%llu,"
      "\"sample_count\":%u,\"epoch_start_us\":%llu,\"sample_start_us\":%llu,"
      "\"system_clock_hz\":%lu,\"divider_256\":%lu,\"loss\":%s,\"dropped_samples\":%llu,"
      "\"overrun_blocks\":%lu,\"gap_events\":%lu,\"data_hex\":\"",
      static_cast<unsigned long>(++sequence), static_cast<unsigned long>(config_id),
      static_cast<unsigned long>(++chunk_sequence), static_cast<unsigned long>(epoch),
      static_cast<unsigned long>(block), static_cast<unsigned long>(offset),
      static_cast<unsigned long long>(index), static_cast<unsigned>(count * 4),
      static_cast<unsigned long long>(start_us), static_cast<unsigned long long>(sample_us),
      static_cast<unsigned long>(system_clock_hz), static_cast<unsigned long>(divider_256),
      dropped != reported_dropped || gaps != reported_gaps ? "true" : "false",
      static_cast<unsigned long long>(dropped), static_cast<unsigned long>(overruns),
      static_cast<unsigned long>(gaps));
  if (header < 0 || static_cast<size_t>(header) + count * 2 + 4 > sizeof(output)) {
    ++output_errors;
    const uint32_t guard = save_and_disable_interrupts();
    capture.dropped_samples += count * 4;
    restore_interrupts(guard);
    return;
  }
  rx_debug::hex_bytes(bytes, count, output + header);
  memcpy(output + header + count * 2, "\"}\n", 3);
  output_size = header + count * 2 + 3;
  output_sent = 0;
  pending_samples = count * 4;
  reported_dropped = dropped;
  reported_gaps = gaps;
}

}  // namespace

void setup() {
  pico_unique_board_id_t board_id;
  pico_get_unique_board_id(&board_id);
  hex_id(board_id.id, device_id);
  const uint64_t random = (static_cast<uint64_t>(rp2040.hwrand32()) << 32) | rp2040.hwrand32();
  uint8_t bytes[8];
  for (size_t i = 0; i < 8; ++i) bytes[i] = random >> (56 - i * 8);
  hex_id(bytes, session);
  USB.disconnect();
  USB.setManufacturer("ha-x2d");
  USB.setProduct("HA-X2D RX Debug");
  USB.setSerialNumber(device_id);
  USB.connect();
  Serial.begin(115200);
  pinMode(CS_PIN, OUTPUT);
  digitalWrite(CS_PIN, HIGH);
  pinMode(MISO_PIN, INPUT);
  pinMode(DATA_PIN, INPUT);
  pinMode(CARRIER_PIN, INPUT);
  SPI.setRX(MISO_PIN);
  SPI.setTX(MOSI_PIN);
  SPI.setSCK(SCK_PIN);
  SPI.begin();
  digitalWrite(CS_PIN, HIGH); delayMicroseconds(40);
  digitalWrite(CS_PIN, LOW); delayMicroseconds(10);
  digitalWrite(CS_PIN, HIGH); delayMicroseconds(40);
  uint8_t part, version;
  detected = strobe(Strobe::reset) && wait_state(1) &&
             read_register(0x30, part, true) && read_register(0x31, version, true) &&
             part == 0 && (version == 4 || version == 0x14);
  if (detected) {
    // TI Table 41: constant low then inverted constant high, one wire at a time.
    const bool data_low_written = write_register(0x02, 0x2F);
    delayMicroseconds(50);
    const bool data_low = digitalRead(DATA_PIN) == LOW;
    const bool data_high_written = write_register(0x02, 0x6F);
    delayMicroseconds(50);
    data_wire_ok = data_low_written && data_high_written && data_low && digitalRead(DATA_PIN) == HIGH;
    const bool data_restored = write_register(0x02, 0x0D);
    const bool carrier_low_written = write_register(0x00, 0x2F);
    delayMicroseconds(50);
    const bool carrier_low = digitalRead(CARRIER_PIN) == LOW;
    const bool carrier_high_written = write_register(0x00, 0x6F);
    delayMicroseconds(50);
    carrier_wire_ok = carrier_low_written && carrier_high_written && carrier_low && digitalRead(CARRIER_PIN) == HIGH;
    const bool carrier_restored = write_register(0x00, 0x0E);
    data_wire_ok = data_wire_ok && data_restored;
    carrier_wire_ok = carrier_wire_ok && carrier_restored;
  }
  init_sampler();
}

void loop() {
  if (!Serial) {
    if (connected) {
      stop_radio();
      capture.dropped_samples += pending_samples;
    }
    connected = false;
    host_ready = false;
    input.reset();
    output_size = output_sent = pending_samples = 0;
    while (Serial.available()) Serial.read();
    return;
  }
  if (!connected) connected = true;
  if (receiving && capture_fault) start_sampler();
  if (output_sent < output_size) {
    const int space = Serial.availableForWrite();
    if (space > 0) {
      const size_t remaining = output_size - output_sent;
      output_sent += Serial.write(reinterpret_cast<const uint8_t*>(output + output_sent),
                                  remaining < static_cast<size_t>(space) ? remaining : space);
      if (output_sent == output_size) pending_samples = 0;
    }
    return;
  }
  pending_samples = 0;
  while (Serial.available()) {
    const int byte = Serial.read();
    if (byte < 0) { ++serial_read_errors; break; }
    const auto event = input.feed(static_cast<char>(byte));
    if (event == ha_x2d::LineFramer::Event::none) continue;
    if (event == ha_x2d::LineFramer::Event::too_long) send_error("line_too_long");
    else {
      rx_debug::Config candidate;
      switch (rx_debug::parse(input.data(), input.length, candidate)) {
        case rx_debug::Command::status: send_status(); break;
        case rx_debug::Command::stop:
          if (stop_radio()) send_status("stopped");
          else send_error("idle_failed");
          break;
        case rx_debug::Command::rx:
          if (start_radio(candidate)) send_status("config");
          else send_error("radio_config_failed");
          break;
        default: send_error("invalid_command", true); break;
      }
    }
    return;
  }
  if (host_ready && static_cast<uint32_t>(millis() - last_status_ms) >= 1000) send_status();
  else if (receiving) send_samples();
}
