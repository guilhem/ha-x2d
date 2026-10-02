#pragma once

#include <x2d/radio_codec.h>

#if defined(ARDUINO_ARCH_RP2040)
#include <hardware/clocks.h>
#include <hardware/dma.h>
#include <hardware/gpio.h>
#include <hardware/pio.h>
#include <pico/time.h>
#endif

namespace x2d {
namespace radio {
namespace digital_tx {

constexpr uint32_t DEFAULT_CHIP_NS = 208500;
constexpr uint32_t CHIP_CYCLES = 16;
constexpr uint8_t STOP_PIN = 22; // free/unwired internal latch; SIO -> PIO JMP PIN
constexpr uint32_t TERMINAL_MASK = 0xFFFFFFC0;
constexpr uint32_t FAULT_MARKER = UINT32_MAX;

// Bit 0 = level, bit 1 = valid, bits 2..7 = completed frame (zero except
// on its LAST chip), bit 8 = final burst chip. No padding and no control words
// between copies. A missing word substitutes invalid/low, never carrier high.
inline size_t pack_burst(const Waveform& wave, uint32_t (&words)[MAX_CHIPS]) {
  if (!wave.copies() ||
      wave.frame_end(wave.copies() - 1) != wave.chips()) return 0;
  size_t begin = 0;
  for (uint8_t frame = 0; frame < wave.copies(); ++frame) {
    const size_t end = wave.frame_end(frame);
    if (end <= begin || end > wave.chips() || end > MAX_CHIPS) return 0;
    begin = end;
  }
  for (size_t chip = 0; chip < wave.chips(); ++chip)
    words[chip] = 2u | static_cast<uint32_t>(wave.chip(chip));
  for (uint8_t frame = 0; frame < wave.copies(); ++frame)
    words[wave.frame_end(frame) - 1] |= uint32_t{frame + 1u} << 2;
  words[wave.chips() - 1] |= 1u << 8;
  return wave.chips();
}

// RP2040 divider is 16.8 fixed point; calibration accepts 1 ns..1 ms,
// subject to the hardware divider range. No floating-point truncation to us.
inline uint32_t clock_divider_256(uint32_t clock_hz, uint32_t chip_ns) {
  if (!clock_hz || !chip_ns || chip_ns > 1000000) return 0;
  constexpr uint64_t denominator = uint64_t{CHIP_CYCLES} * 1000000000;
  const uint64_t divider =
      (uint64_t{clock_hz} * chip_ns * 256 + denominator / 2) / denominator;
  return divider >= 256 && divider <= 0xFFFFFF ? static_cast<uint32_t>(divider) : 0;
}

// Real program words, also executed by the native check. SDK relocates JMPs.
// All paths between OUT PINS are exactly 16 cycles, including copy boundaries.
// Y holds a frame marker until AFTER the following chip starts (the previous
// frame is now complete). NOBLOCK PUSH loss sets FDEBUG_RXSTALL: never guess.
// STOP is sampled five ticks before EOF, then the FULL final period elapses
// before low/terminal marker. A later request stops at the next full frame.
// https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf
constexpr uint16_t PROGRAM[] = {
    0xe020,  //  0: set x,0
    0x8080,  //  1: pull noblock
    0x6001,  //  2: out pins,1
    0x0068,  //  3: jmp !y,8
    0xa0c2,  //  4: mov isr,y
    0x8000,  //  5: push noblock        ; prior copy complete, pins untouched
    0xe040,  //  6: set y,0
    0x0009,  //  7: jmp 9
    0xa342,  //  8: nop [3]            ; balance absent prior marker
    0x6021,  //  9: out x,1            ; valid bit
    0x003b,  // 10: jmp !x,27
    0x6026,  // 11: out x,6            ; frame index
    0x0039,  // 12: jmp !x,25
    0xa041,  // 13: mov y,x
    0x00d7,  // 14: jmp pin,23         ; GP22 STOP latch
    0x6021,  // 15: out x,1            ; final chip flag
    0x0020,  // 16: jmp !x,0           ; uninterrupted next chip
    0xa142,  // 17: nop [1]            ; final-chip path: complete 16 cycles
    0xe000,  // 18: set pins,0
    0xa0cb,  // 19: mov isr,~null
    0x4046,  // 20: in y,6             ; terminal mask | completed copy
    0x8000,  // 21: push noblock
    0x0016,  // 22: jmp 22             ; halted/low
    0xa242,  // 23: nop [2]            ; STOP path: complete 16 cycles
    0x0012,  // 24: jmp 18
    0xa242,  // 25: nop [2]            ; ordinary-chip path balance
    0x0000,  // 26: jmp 0
    0xe000,  // 27: set pins,0         ; underrun/invalid token
    0xa0cb,  // 28: mov isr,~null       ; distinct fault marker
    0x8000,  // 29: push noblock
    0x0016,  // 30: jmp 22
};

#if defined(ARDUINO_ARCH_RP2040)

enum class State : uint8_t { idle, running, frame_boundary, stopping, fault };
enum class Error : uint8_t {
  none, resources, underrun, pio_stall, dma_error, timeout, clock_changed,
  marker_lost, marker_error
};

// Digital only: never configures CC1101 or strobes STX. Single loop/core owner.
// Keep static; must outlive DMA, including State::stopping. Construction and
// refused starts do not drive any GPIO. Each start requires the caller to
// explicitly assert that CC1101 GDO0 has ALREADY become an async TX INPUT.
// Default gateway profile qualification remains entirely the caller's gate.
//
// start_burst -> poll until frame_boundary -> abort(true) after radio IDLE.
// completed_frames resets per burst; counts ONLY sequenced complete markers.
// running spans multiple copies; frame_boundary is emitted only after EOF/STOP
// and DMA retirement, with GP20 already low. Marker loss is always a fault.
// request_stop latches GP22 once. It is sampled during the final chip: a request
// in its last five PIO ticks may defer to ONE additional complete copy. GPIO22
// must be free/unwired. Neither pin is driven before tx_input_ready is asserted.
// Faults park GP20 low until the caller
// confirms radio IDLE through abort(true); abort(false) keeps it low. Abort
// never counts a partial copy. Poll stopping before reuse. No blocking waits.
class Tx {
 public:
  Tx() = default;
  Tx(const Tx&) = delete;
  Tx& operator=(const Tx&) = delete;

  bool start_burst(const Waveform& wave, uint32_t chip_ns, bool tx_input_ready) {
    if (!tx_input_ready || state_ != State::idle) return false;
    const uint32_t hz = clock_get_hz(clk_sys);
    const uint32_t divider = clock_divider_256(hz, chip_ns);
    if (!divider) return false;
    const size_t count = pack_burst(wave, words_);
    if (!count) return false;
    if (!claim()) {
      error_ = Error::resources;
      release();
      state_ = State::fault;
      return false;
    }
    clock_hz_ = hz;
    divider_256_ = divider;
    error_ = Error::none;
    hold_low_ = false;
    completed_frames_ = 0;
    copies_ = wave.copies();
    stop_requested_ = boundary_pending_ = false;
    pio_sm_config cfg = pio_get_default_sm_config();
    sm_config_set_wrap(&cfg, offset_, offset_ + PROGRAM_LENGTH - 1);
    sm_config_set_out_pins(&cfg, PIN, 1);
    sm_config_set_set_pins(&cfg, PIN, 1);
    sm_config_set_out_shift(&cfg, true, false, 32);
    sm_config_set_in_shift(&cfg, false, false, 32);
    sm_config_set_jmp_pin(&cfg, STOP_PIN);
    sm_config_set_clkdiv_int_frac(&cfg, divider >> 8, divider & 255);
    pio_sm_init(pio_, sm_, offset_, &cfg);
    pio_->fdebug = debug_mask();
    pio_sm_exec(pio_, sm_, pio_encode_set(pio_y, 0));
    gpio_init(STOP_PIN);
    gpio_put(STOP_PIN, false);
    gpio_set_dir(STOP_PIN, GPIO_OUT);
    latch_owned_ = true;
    // Initialize PIO level/direction BEFORE selecting it on the connected pin.
    pio_sm_set_pins_with_mask(pio_, sm_, 0, 1u << PIN);
    pio_sm_set_pindirs_with_mask(pio_, sm_, 1u << PIN, 1u << PIN);
    pio_gpio_init(pio_, PIN);
    gpio_disable_pulls(PIN);
    pin_owned_ = true;

    // Leave at least one word for DMA, including a one-chip synthetic burst.
    const size_t prefill = count > 4 ? 4 : count - 1;
    for (size_t i = 0; i < prefill; ++i) pio_sm_put(pio_, sm_, words_[i]);
    dma_channel_config dma_cfg = dma_channel_get_default_config(dma_);
    channel_config_set_transfer_data_size(&dma_cfg, DMA_SIZE_32);
    channel_config_set_read_increment(&dma_cfg, true);
    channel_config_set_write_increment(&dma_cfg, false);
    channel_config_set_dreq(&dma_cfg, pio_get_dreq(pio_, sm_, true));
    dma_channel_set_irq0_enabled(dma_, false);
    dma_channel_set_irq1_enabled(dma_, false);
    dma_hw->intr = 1u << dma_;
    dma_channel_configure(dma_, &dma_cfg, &pio_->txf[sm_], words_ + prefill,
                          count - prefill, false);
    // Clear W1C bus-error flags left by an earlier owner, without triggering.
    hw_set_bits(&dma_channel_hw_addr(dma_)->al1_ctrl,
                DMA_CH0_CTRL_TRIG_READ_ERROR_BITS | DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS);
    dma_channel_start(dma_);
    // Two startup ticks, then full chips; terminal marker follows low by 3 ticks.
    const uint64_t cycles = count * CHIP_CYCLES + 6;
    const uint64_t duration_us =
        (cycles * divider * 1000000 + uint64_t{hz} * 256 - 1) / (uint64_t{hz} * 256);
    started_us_ = time_us_64();  // observation at SM enable, not a measured edge
    deadline_us_ = started_us_ + duration_us + 1000;
    boundary_observed_us_ = 0;
    state_ = State::running;
    pio_sm_set_enabled(pio_, sm_, true);
    return true;
  }

  State poll() {
    if (state_ == State::stopping) {
      // A bus error can arrive from an in-flight write during STOP retirement.
      if (boundary_pending_ && (dma_channel_hw_addr(dma_)->ctrl_trig &
          (DMA_CH0_CTRL_TRIG_READ_ERROR_BITS | DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS)))
        fail(Error::dma_error);
      if (dma_quiescent()) {
        if (boundary_pending_) state_ = State::frame_boundary;
        else {
          release();
          state_ = error_ == Error::none ? State::idle : State::fault;
        }
      }
    } else if (state_ == State::running) {
      if (clock_get_hz(clk_sys) != clock_hz_) fail(Error::clock_changed);
      else if (dma_channel_hw_addr(dma_)->ctrl_trig &
               (DMA_CH0_CTRL_TRIG_READ_ERROR_BITS | DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS))
        fail(Error::dma_error);
      else if (pio_->fdebug & debug_mask()) marker_fault();
      else {
        for (uint8_t budget = 0; budget < 4 && !pio_sm_is_rx_fifo_empty(pio_, sm_); ++budget) {
          const uint32_t marker = pio_sm_get(pio_, sm_);
          if (marker == FAULT_MARKER) { fail(Error::underrun); break; }
          const bool terminal = (marker & TERMINAL_MASK) == TERMINAL_MASK;
          const uint32_t frame = terminal ? marker & 63u : marker;
          if (frame != completed_frames_ + 1 || frame > copies_ ||
              (terminal && frame < copies_ && !stop_requested_)) {
            fail(Error::marker_error); break;
          }
          ++completed_frames_;
          boundary_observed_us_ = time_us_64();
          if (terminal) {
            if (frame == copies_ && (dma_channel_is_busy(dma_) ||
                dma_channel_hw_addr(dma_)->transfer_count ||
                !pio_sm_is_tx_fifo_empty(pio_, sm_))) {
              fail(Error::dma_error); break;
            }
            pio_sm_set_enabled(pio_, sm_, false); // hardware already halted low
            boundary_pending_ = true;
            stop_dma(); // STOP can leave future words in DMA/FIFO; discard safely
            state_ = dma_quiescent() ? State::frame_boundary : State::stopping;
            break;
          }
        }
        // PUSH may race a drain; an overflow detected after draining is still
        // a fault even if a plausible terminal marker was received.
        if ((state_ == State::running || boundary_pending_) && (pio_->fdebug & debug_mask()))
          marker_fault();
        else if (boundary_pending_ && (dma_channel_hw_addr(dma_)->ctrl_trig &
                 (DMA_CH0_CTRL_TRIG_READ_ERROR_BITS | DMA_CH0_CTRL_TRIG_WRITE_ERROR_BITS)))
          fail(Error::dma_error);
        else if (state_ == State::running && time_us_64() >= deadline_us_) fail(Error::timeout);
      }
    }
    return state_;
  }

  bool at_frame_boundary() const { return state_ == State::frame_boundary; }
  void request_stop() {
    if (state_ != State::running || stop_requested_) return;
    stop_requested_ = true;
    gpio_put(STOP_PIN, true);
  }
  void abort(bool radio_idle) {
    error_ = Error::none;
    hold_low_ = !radio_idle;
    stop_requested_ = boundary_pending_ = false;
    stop_hardware();
  }
  State state() const { return state_; }
  Error error() const { return error_; }
  uint32_t divider_256() const { return divider_256_; }
  uint32_t system_clock_hz() const { return clock_hz_; }
  uint32_t completed_frames() const { return completed_frames_; }
  uint64_t started_us() const { return started_us_; }
  uint64_t boundary_observed_us() const { return boundary_observed_us_; }

 private:
  static constexpr uint PIN = 20;
  static constexpr uint PROGRAM_LENGTH = sizeof(PROGRAM) / sizeof(PROGRAM[0]);
  uint32_t debug_mask() const {
    return (1u << (PIO_FDEBUG_TXSTALL_LSB + sm_)) |
           (1u << (PIO_FDEBUG_TXOVER_LSB + sm_)) |
           (1u << (PIO_FDEBUG_RXSTALL_LSB + sm_));
  }
  bool dma_quiescent() const {
    // RP2040-E13: ABORT can clear before in-flight writes retire. Check BUSY
    // too, and never return a channel to the pool with an abort still pending.
    return dma_ < 0 || (!(dma_hw->abort & (1u << dma_)) && !dma_channel_is_busy(dma_));
  }
  bool claim() {
    if (dma_ >= 0) return true;
    const pio_program program = {PROGRAM, PROGRAM_LENGTH, -1, 0};
    const PIO candidates[] = {pio0, pio1};
    for (PIO candidate : candidates) {
      const int sm = pio_claim_unused_sm(candidate, false);
      if (sm < 0) continue;
      if (!pio_can_add_program(candidate, &program)) {
        pio_sm_unclaim(candidate, sm);
        continue;
      }
      const int offset = pio_add_program(candidate, &program);
      if (offset < 0) {
        pio_sm_unclaim(candidate, sm);
        continue;
      }
      pio_ = candidate;
      sm_ = sm;
      offset_ = offset;
      dma_ = dma_claim_unused_channel(false);
      return dma_ >= 0;
    }
    return false;
  }
  void input_safe() {
    if (pin_owned_ && !hold_low_) {
      // SIO output latch low but OE disabled; no generic GPIO output is enabled.
      gpio_put(PIN, false);
      gpio_set_dir(PIN, GPIO_IN);
      gpio_set_function(PIN, GPIO_FUNC_SIO);
      gpio_disable_pulls(PIN);
      pin_owned_ = false;
    }
    if (latch_owned_) {
      gpio_put(STOP_PIN, false);
      gpio_set_dir(STOP_PIN, GPIO_IN);
      gpio_pull_down(STOP_PIN);
      latch_owned_ = false;
    }
  }
  void park_low() {
    if (!pin_owned_) return;
    gpio_put(PIN, false);
    gpio_set_dir(PIN, GPIO_OUT);
    gpio_set_function(PIN, GPIO_FUNC_SIO);
  }
  void release() {
    if (hold_low_) park_low();
    input_safe();
    if (dma_ >= 0) {
      dma_hw->intr = 1u << dma_;
      dma_channel_unclaim(dma_);
      dma_ = -1;
    }
    if (sm_ >= 0) {
      pio_sm_set_enabled(pio_, sm_, false);
      pio_sm_clear_fifos(pio_, sm_);
      pio_sm_set_pindirs_with_mask(pio_, sm_, 0, 1u << PIN);
      const pio_program program = {PROGRAM, PROGRAM_LENGTH, -1, 0};
      pio_remove_program(pio_, &program, offset_);
      pio_sm_unclaim(pio_, sm_);
      sm_ = -1;
      pio_ = nullptr;
    }
  }
  void stop_dma() {
    if (dma_ >= 0) {
      hw_clear_bits(&dma_channel_hw_addr(dma_)->al1_ctrl, DMA_CH0_CTRL_TRIG_EN_BITS);
      if (dma_channel_is_busy(dma_))
        dma_hw->abort = 1u << dma_;  // asynchronous; no SDK abort spin loop
    }
  }
  void stop_hardware() {
    if (sm_ >= 0) pio_sm_set_enabled(pio_, sm_, false);
    if (hold_low_) park_low();  // CC1101 may still be in asynchronous TX
    else input_safe();
    stop_dma();
    state_ = State::stopping;
    if (dma_quiescent()) {
      release();
      state_ = error_ == Error::none ? State::idle : State::fault;
    }
  }
  void fail(Error error) {
    error_ = error;
    hold_low_ = true;  // caller releases GP20 only after confirming radio IDLE
    boundary_pending_ = false;
    stop_hardware();  // no frame notification on ambiguous/partial output
  }
  void marker_fault() {
    fail((pio_->fdebug & (1u << (PIO_FDEBUG_RXSTALL_LSB + sm_)))
             ? Error::marker_lost : Error::pio_stall);
  }

  alignas(4) uint32_t words_[MAX_CHIPS]{};
  PIO pio_ = nullptr;
  int sm_ = -1, dma_ = -1;
  uint offset_ = 0;
  State state_ = State::idle;
  Error error_ = Error::none;
  bool stop_requested_ = false, pin_owned_ = false, latch_owned_ = false, boundary_pending_ = false;
  bool hold_low_ = false;
  uint8_t copies_ = 0;
  uint32_t clock_hz_ = 0, divider_256_ = 0, completed_frames_ = 0;
  uint64_t started_us_ = 0, boundary_observed_us_ = 0, deadline_us_ = 0;
};

#endif
}  // namespace digital_tx
}  // namespace radio
}  // namespace x2d
