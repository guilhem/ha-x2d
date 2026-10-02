#include <Arduino.h>
#include <SPI.h>
#include <USB.h>
#include <pico/unique_id.h>
#include <hardware/sync.h>
#include "serial.h"
#include <ArduinoJson.h>
#include "radio_tx.h"
#include "capture_check.h"
#include <x2d/cc1101.h>
#include "radio_bus.h"

namespace {
namespace tx = x2d::radio::digital_tx;
constexpr uint8_t MISO_PIN = 16, CS_PIN = 17, SCK_PIN = 18, MOSI_PIN = 19, DATA_PIN = 20;
constexpr uint32_t SAMPLE_HZ = 400000;
constexpr size_t CAPTURE_WORDS = 4096, CAPTURE_SAMPLES = CAPTURE_WORDS * 32;
x2d::rp2040::RadioBus radio_bus;
x2d::cc1101::Driver<x2d::rp2040::RadioBus, x2d::cc1101::Mode::tx_check> radio(radio_bus);
x2d::cc1101::DigitalInput radio_input;
enum class Phase : uint8_t { idle, frames, tail, cleanup, blocked };
Phase phase = Phase::idle;
tx::Tx transmitter;
x2d::radio::Waveform wave;
x2d::LineFramer input(32);
x2d::OutputBuffer usb_output;
char output[4096];
char serial[17]; // USB core retains this pointer
alignas(4) uint32_t samples[CAPTURE_WORDS];
PIO sampler_pio = nullptr;
int sampler_sm = -1, sampler_dma = -1;
uint sampler_offset = 0;
constexpr uint16_t SAMPLE_PROGRAM[] = {0x4001}; // in pins,1; wrap every cycle
const pio_program sample_program = {SAMPLE_PROGRAM, 1, -1, 0};
bool sampler_armed = false, capture_complete = false, analysis_ready = false;
bool radio_known = false, cleanup_ok = false, result_reported = false;
bool usb_stress = false, continuous_gap_match = false;
uint32_t usb_read_bytes = 0, usb_written_bytes = 0;
uint8_t frames = 0, tx_error = 0;
uint32_t sampler_hz = 0, sampler_divider = 0, tx_divider = 0;
size_t confirmed_words = 0;
uint64_t sampler_started_us = 0, capture_deadline_us = 0, cleanup_deadline_us = 0;
uint64_t burst_started_us = 0, burst_boundary_us = 0;
const char* failure = nullptr;
tx_check::Analysis analysis;

bool read_radio() {
  radio_known = radio.read_digital_input(radio_input);
  return radio_known;
}
bool reset_radio() {
  return radio.reset() && radio.idle() && radio.probe().detected;
}
bool digital_input_verified() {
  // Keep the status snapshot even when the safety check fails.
  return read_radio() && decltype(radio)::digital_input_valid(radio_input);
}

bool sampler_quiescent() {
  return sampler_dma < 0 ||
         (!(dma_hw->abort & (1u << sampler_dma)) && !dma_channel_is_busy(sampler_dma));
}
void release_sampler() {
  if (sampler_dma >= 0) {
    dma_hw->intr = 1u << sampler_dma;
    dma_channel_unclaim(sampler_dma);
    sampler_dma = -1;
  }
  if (sampler_sm >= 0) {
    pio_sm_set_enabled(sampler_pio, sampler_sm, false);
    pio_sm_clear_fifos(sampler_pio, sampler_sm);
    pio_remove_program(sampler_pio, &sample_program, sampler_offset);
    pio_sm_unclaim(sampler_pio, sampler_sm);
    sampler_sm = -1;
    sampler_pio = nullptr;
  }
  sampler_armed = false;
}
void stop_sampler() {
  if (sampler_sm >= 0) pio_sm_set_enabled(sampler_pio, sampler_sm, false);
  if (sampler_dma >= 0) {
    hw_clear_bits(&dma_channel_hw_addr(sampler_dma)->al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
    if (sampler_armed) {
      // Lower bound before ABORT clears the live transfer count; no partial
      // OSR/FIFO word is presented as captured data after a fault.
      const uint32_t remaining = dma_channel_hw_addr(sampler_dma)->transfer_count;
      confirmed_words = remaining <= CAPTURE_WORDS ? CAPTURE_WORDS - remaining : 0;
    }
    if (dma_channel_is_busy(sampler_dma)) dma_hw->abort = 1u << sampler_dma;
  }
}
bool start_sampler() {
  const PIO candidates[] = {pio0, pio1};
  for (PIO candidate : candidates) {
    const int sm = pio_claim_unused_sm(candidate, false);
    if (sm < 0) continue;
    if (!pio_can_add_program(candidate, &sample_program)) {
      pio_sm_unclaim(candidate, sm);
      continue;
    }
    const int offset = pio_add_program(candidate, &sample_program);
    if (offset < 0) { pio_sm_unclaim(candidate, sm); continue; }
    sampler_pio = candidate;
    sampler_sm = sm;
    sampler_offset = offset;
    sampler_dma = dma_claim_unused_channel(false);
    if (sampler_dma < 0) { release_sampler(); return false; }
    break;
  }
  if (sampler_sm < 0) return false;
  sampler_hz = clock_get_hz(clk_sys);
  sampler_divider = (uint64_t{sampler_hz} * 256 + SAMPLE_HZ / 2) / SAMPLE_HZ;
  if (sampler_divider < 256 || sampler_divider > 0xFFFFFF) { release_sampler(); return false; }
  pio_sm_config cfg = pio_get_default_sm_config();
  sm_config_set_wrap(&cfg, sampler_offset, sampler_offset);
  sm_config_set_in_pins(&cfg, DATA_PIN);
  sm_config_set_in_shift(&cfg, true, true, 32);
  sm_config_set_clkdiv_int_frac(&cfg, sampler_divider >> 8, sampler_divider & 255);
  pio_sm_init(sampler_pio, sampler_sm, sampler_offset, &cfg);
  // Reading requires NO GPIO mux/direction changes. TX remains the sole owner.
  dma_channel_config dma_cfg = dma_channel_get_default_config(sampler_dma);
  channel_config_set_transfer_data_size(&dma_cfg, DMA_SIZE_32);
  channel_config_set_read_increment(&dma_cfg, false);
  channel_config_set_write_increment(&dma_cfg, true);
  channel_config_set_dreq(&dma_cfg, pio_get_dreq(sampler_pio, sampler_sm, false));
  dma_channel_set_irq0_enabled(sampler_dma, false);
  dma_channel_set_irq1_enabled(sampler_dma, false);
  dma_hw->intr = 1u << sampler_dma;
  dma_channel_configure(sampler_dma, &dma_cfg, samples, &sampler_pio->rxf[sampler_sm],
                        CAPTURE_WORDS, false);
  hw_set_bits(&dma_channel_hw_addr(sampler_dma)->al1_ctrl,
              DMA_CH0_CTRL_TRIG_READ_ERROR_BITS | DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS);
  sampler_armed = true;
  dma_channel_start(sampler_dma);
  sampler_started_us = time_us_64();
  capture_deadline_us = sampler_started_us +
      (uint64_t{CAPTURE_SAMPLES} * sampler_divider * 1000000) / (uint64_t{sampler_hz} * 256) + 20000;
  pio_sm_set_enabled(sampler_pio, sampler_sm, true);
  return true;
}

void queue_report(const char* type) {
  JsonDocument doc;
  doc["type"] = type;
  doc["product"] = "ha-x2d-tx-check";
  doc["digital_only"] = true;
  doc["rf_tx"] = false;
  doc["radio_profile_verified"] = false;
  doc["gap_qualified"] = false;
  doc["backend"] = "continuous_burst_dma";
  doc["continuous_gap_match"] = continuous_gap_match;
  doc["expected_rx_gap_us"] = 0;
  doc["usb_stress"] = usb_stress;
  doc["usb_read_bytes"] = usb_read_bytes;
  doc["usb_written_bytes"] = usb_written_bytes;
  doc["pass"] = !failure && analysis_ready && !analysis.error && continuous_gap_match && cleanup_ok && frames == tx_check::COPIES;
  if (failure) doc["error"] = failure; else doc["error"] = nullptr;
  if (radio_known) {
    doc["marcstate"] = radio_input.marcstate;
    doc["iocfg0"] = radio_input.iocfg0;
    doc["pktctrl0"] = radio_input.pktctrl0;
  } else doc["marcstate"] = nullptr;
  doc["frames"] = frames;
  doc["expected_frames"] = tx_check::COPIES;
  doc["body_bytes"] = x2d::radio::MAX_BODY_BYTES;
  doc["payload_source"] = "synthetic_public_vector";
  doc["capture_complete"] = capture_complete;
  doc["confirmed_samples"] = confirmed_words * 32;
  doc["capture_capacity_samples"] = CAPTURE_SAMPLES;
  doc["waveform_match"] = analysis_ready && !analysis.error;
  doc["gpio_released"] = transmitter.state() == tx::State::idle;
  doc["gpio_low"] = !gpio_get(DATA_PIN);
  doc["resources_released"] = sampler_sm < 0 && sampler_dma < 0 && transmitter.state() == tx::State::idle;
  doc["tx_error_code"] = tx_error;
  doc["system_clock_hz"] = sampler_hz;
  doc["sampler_divider_256"] = sampler_divider;
  doc["tx_divider_256"] = tx_divider;
  doc["timing_reference"] = "shared_clk_sys";
  doc["sampler_started_observed_us"] = sampler_started_us;
  doc["burst_started_observed_us"] = burst_started_us;
  doc["burst_boundary_observed_us"] = burst_boundary_us;
  if (sampler_divider && sampler_hz) {
    doc["sample_hz"] = double(sampler_hz) * 256 / sampler_divider;
    doc["sample_resolution_us"] = double(sampler_divider) * 1000000 / (double(sampler_hz) * 256);
    doc["calibrated_chip_us"] = double(tx_divider) * tx::CHIP_CYCLES * 1000000 / (double(sampler_hz) * 256);
  }
  if (analysis_ready) {
    doc["trailing_low_samples"] = analysis.trailing_low_samples;
    JsonArray measurements = doc["frame_measurements"].to<JsonArray>();
    for (uint8_t frame = 0; frame < tx_check::COPIES; ++frame) {
      const auto& measured = analysis.frames[frame];
      JsonObject item = measurements.add<JsonObject>();
      item["chips"] = wave.frame_end(frame) - (frame ? wave.frame_end(frame - 1) : 0);
      item["start_sample"] = measured.start_sample;
      item["end_sample"] = measured.end_sample;
      item["end_edge_observed"] = measured.end_edge_observed;
      item["gap_us"] = measured.gap_us;
      item["edges"] = measured.edges;
      item["max_edge_error_us"] = measured.max_edge_error_ns / 1000;
      if (measured.edges) {
        item["chip_us_min"] = measured.chip_ns_min / 1000;
        item["chip_us_max"] = measured.chip_ns_max / 1000;
        item["chip_us_mean"] = measured.chip_ns_sum / (measured.edges * 1000);
      }
    }
  }
  const size_t bytes = measureJson(doc);
  if (doc.overflowed() || bytes + 1 > sizeof(output)) {
    constexpr char error[] = "{\"type\":\"result\",\"digital_only\":true,\"pass\":false,\"error\":\"json_overflow\"}\n";
    usb_output.append(error, sizeof(error) - 1);
    return;
  }
  serializeJson(doc, output, sizeof(output));
  output[bytes] = '\n';
  usb_output.append(output, bytes + 1); // one pending traffic line + one result fit
}

void begin_cleanup(const char* error) {
  if (error && !failure) failure = error;
  tx_error = static_cast<uint8_t>(transmitter.error());
  transmitter.abort(true); // CC1101 remains IDLE; no spin on stalled DMA
  if (radio_known && radio_input.iocfg0 == 0x2E) gpio_pull_down(DATA_PIN);
  stop_sampler();
  cleanup_deadline_us = time_us_64() + 20000;
  phase = Phase::cleanup;
}
void start_check(bool stress) {
  failure = nullptr;
  frames = tx_error = 0;
  confirmed_words = 0;
  capture_complete = analysis_ready = cleanup_ok = result_reported = false;
  continuous_gap_match = false;
  analysis = tx_check::Analysis{};
  burst_started_us = burst_boundary_us = 0;
  sampler_hz = sampler_divider = tx_divider = 0;
  sampler_started_us = 0;
  usb_stress = stress;
  usb_read_bytes = usb_written_bytes = 0;
  radio_known = radio.configure_digital_check(radio_input);
  if (!radio_known) {
    begin_cleanup("radio_input_not_verified"); return;
  }
  // Weak low baseline only AFTER CC1101 is verified HiZ/async/IDLE. No SIO OE.
  gpio_pull_down(DATA_PIN);
  x2d::radio::Body body;
  x2d::radio::make_body(0xF73192, 4, 6641, &body); // public vector, no journal
  body.length = x2d::radio::MAX_BODY_BYTES;
  for (size_t i = x2d::radio::BODY_BYTES; i < body.length; ++i) body.bytes[i] = 0xFF;
  // Synthetic append is not an enrollment formatter or an authenticated body.
  if (!x2d::radio::encode_burst(body, tx_check::COPIES, &wave) ||
      !wave.chip(wave.chips() - 1)) { begin_cleanup("synthetic_body_failed"); return; }
  if (!start_sampler()) { begin_cleanup("sampler_resources"); return; }
  if (!transmitter.start_burst(wave, tx::DEFAULT_CHIP_NS, true)) {
    begin_cleanup("tx_start_failed"); return;
  }
  tx_divider = transmitter.divider_256();
  burst_started_us = transmitter.started_us();
  phase = Phase::frames;
}

void tick_check() {
  if (phase == Phase::idle) return;
  if (phase == Phase::cleanup || phase == Phase::blocked) {
    const auto state = transmitter.poll();
    if (state == tx::State::idle && sampler_quiescent()) {
      release_sampler();
      cleanup_ok = radio.idle() && digital_input_verified();
      if (cleanup_ok) gpio_pull_down(DATA_PIN); // preserve low after GPIO release
      else if (!failure) failure = "final_idle_not_verified";
      if (cleanup_ok && gpio_get(DATA_PIN)) {
        cleanup_ok = false;
        if (!failure) failure = "gpio_not_low_after_release";
      }
      if (!result_reported) queue_report("result");
      phase = Phase::idle;
    } else if (phase == Phase::cleanup && time_us_64() >= cleanup_deadline_us) {
      failure = "cleanup_timeout";
      queue_report("result"); // fail, retained resources; never claim validation
      result_reported = true;
      phase = Phase::blocked;
    }
    return;
  }
  if (clock_get_hz(clk_sys) != sampler_hz) { begin_cleanup("clock_changed"); return; }
  const auto* channel = dma_channel_hw_addr(sampler_dma);
  if (channel->ctrl_trig & (DMA_CH0_CTRL_TRIG_READ_ERROR_BITS | DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS)) {
    begin_cleanup("capture_dma_error"); return;
  }
  if (sampler_pio->fdebug & (1u << (PIO_FDEBUG_RXSTALL_LSB + sampler_sm))) {
    begin_cleanup("capture_pio_stall"); return;
  }
  if (!dma_channel_is_busy(sampler_dma)) {
    if (channel->transfer_count || phase != Phase::tail) {
      begin_cleanup("capture_exhausted_before_frames"); return;
    }
    pio_sm_set_enabled(sampler_pio, sampler_sm, false);
    __dmb();
    capture_complete = true;
    confirmed_words = CAPTURE_WORDS;
    const double sample_ns = double(sampler_divider) * 1e9 / (double(sampler_hz) * 256);
    const double chip_ns = double(tx_divider) * tx::CHIP_CYCLES * 1e9 / (double(sampler_hz) * 256);
    analysis = tx_check::analyze(wave, samples, CAPTURE_SAMPLES, sample_ns, chip_ns);
    analysis_ready = true;
    continuous_gap_match = !analysis.error;
    for (uint8_t frame = 1; frame < tx_check::COPIES; ++frame)
      if (analysis.frames[frame].gap_us < -sample_ns * 2 / 1000 ||
          analysis.frames[frame].gap_us > sample_ns * 2 / 1000) continuous_gap_match = false;
    begin_cleanup(analysis.error ? analysis.error : continuous_gap_match ? nullptr : "nonzero_copy_gap");
    return;
  }
  if (time_us_64() >= capture_deadline_us) { begin_cleanup("capture_timeout"); return; }
  if (phase == Phase::tail) return;
  const auto state = transmitter.poll();
  frames = transmitter.completed_frames();
  if (state == tx::State::running || state == tx::State::stopping) return;
  if (state != tx::State::frame_boundary) { begin_cleanup("tx_fault"); return; }
  if (frames != tx_check::COPIES) { begin_cleanup("tx_incomplete_burst"); return; }
  burst_boundary_us = transmitter.boundary_observed_us();
  // No CPU restart, pin mux change, or SPI between copies. Hardware is low.
  phase = Phase::tail;
}
} // namespace

void setup() {
  pico_unique_board_id_t id;
  pico_get_unique_board_id(&id);
  const char digits[] = "0123456789ABCDEF";
  for (size_t i = 0; i < 8; ++i) {
    serial[2 * i] = digits[id.id[i] >> 4];
    serial[2 * i + 1] = digits[id.id[i] & 15];
  }
  serial[16] = 0;
  USB.disconnect();
  USB.setManufacturer("ha-x2d");
  USB.setProduct("HA-X2D TX Check");
  USB.setSerialNumber(serial);
  USB.connect();
  Serial.begin(115200);
  digitalWrite(CS_PIN, HIGH);
  pinMode(CS_PIN, OUTPUT);
  pinMode(MISO_PIN, INPUT);
  pinMode(DATA_PIN, INPUT);
  gpio_disable_pulls(DATA_PIN);
  SPI.setRX(MISO_PIN); SPI.setTX(MOSI_PIN); SPI.setSCK(SCK_PIN);
  SPI.begin();
  reset_radio(); // reset/IDLE only; no digital output or unsolicited USB line
  read_radio();
}

void loop() {
  if (!Serial) {
    if (phase == Phase::frames || phase == Phase::tail) begin_cleanup("usb_disconnected");
    usb_output.clear();
    input.reset();
    tick_check();
    return;
  }
  tick_check();
  if (usb_output.size()) {
    const int space = Serial.availableForWrite();
    if (space > 0) {
      const size_t left = usb_output.contiguous();
      const size_t count = left < static_cast<size_t>(space) ? left : static_cast<size_t>(space);
      const size_t sent = Serial.write(reinterpret_cast<const uint8_t*>(usb_output.data()), count);
      usb_output.consume(sent);
      if (phase == Phase::frames || phase == Phase::tail) usb_written_bytes += sent;
    }
  }
  if (phase == Phase::frames || phase == Phase::tail) {
    if (usb_stress) {
      for (unsigned budget = 0; budget < 64 && Serial.available(); ++budget) {
        if (Serial.read() < 0) break;
        ++usb_read_bytes; // host may stream arbitrary load bytes, never commands
      }
      if (!usb_output.size()) {
        constexpr char traffic[] = "{\"type\":\"traffic\",\"digital_only\":true}\n";
        usb_output.append(traffic, sizeof(traffic) - 1);
      }
    }
    return;
  }
  if (phase != Phase::idle || usb_output.size()) return;
  for (unsigned budget = 0; budget < 64 && Serial.available(); ++budget) {
    const int byte = Serial.read();
    if (byte < 0) break;
    const auto event = input.feed(static_cast<char>(byte));
    if (event == x2d::LineFramer::Event::none) continue;
    if (event == x2d::LineFramer::Event::line && input.length == 5 &&
        !memcmp(input.data(), "check", 5)) start_check(false);
    else if (event == x2d::LineFramer::Event::line && input.length == 9 &&
             !memcmp(input.data(), "check usb", 9)) start_check(true);
    else {
      if (event == x2d::LineFramer::Event::line && input.length == 6 &&
          !memcmp(input.data(), "status", 6)) read_radio();
      else failure = "invalid_command";
      queue_report("status");
    }
    return;
  }
}
