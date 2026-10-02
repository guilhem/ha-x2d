# Digital TX check — no RF activation

Standalone USB product **HA-X2D TX Check**. Boot only resets the CC1101 and
requests IDLE. Send `check\n` for a quiet run, `check usb\n` for USB traffic during
capture (CRLF also accepted); `status\n` reads state. In the USB run, continuously
read JSON traffic lines and optionally stream load bytes until the result arrives.
Input is discarded with a 64-byte loop budget; writes use available USB space
and a bounded output queue. Compare both result lines; byte counters report
the actual load handled. `gap_qualified` is always false: observed RF captures
have no idle chips between copies. This version uses one continuous DMA burst;
the measured gaps must be checked on board before RF qualification.
There is no transmit strobe, radio calibration, FIFO write or flash write.

Each check verifies CC1101 identity, IDLE, IOCFG0 `0x2e` (HiZ), and PKTCTRL0
`0x30` (async) before the TX backend takes GP20. The public F73192/counter 6641
vector is extended with three synthetic `ff` bytes; it is not an enrollment
formatter. Three complete copies are clocked continuously at calibrated 208.5 us/chip.
An independent PIO SM reads GP20 at 400 kHz into a finite 16 KiB DMA buffer,
without IRQs or changes to the TX pin's mux/direction. GP20 remains low after
the full final chip until capture completes, then returns to input with a weak
pulldown. No SPI reads or CPU restarts occur between copies.
Final status rechecks IDLE and both registers. Failures abort output,
return `pass:false`, and retain any resource whose DMA has not safely retired.

One JSON result contains completed frames, confirmed samples, exact chip-slice
matching, captured edges, chip duration statistics, sample positions, measured
gaps, and the low tail. `continuous_gap_match` allows only two samples of gap
error (about 5 us); the digital `pass` requires this as well as complete shape.
`end_edge_observed:false` means a low final chip merges
into the gap: its end sample is estimated from the chip clock, while the backend
completion marker independently confirms full duration. This test's last frame
ends high, so its final falling edge is measured directly. Matching tolerates
two samples (about 5 us); both PIOs share clk_sys, so absolute quartz accuracy,
RF output and receiver compatibility remain unqualified.

Backend integration: `start_burst(wave, chip_ns, tx_input_ready)` requires
verified CC1101 async input before taking GP20 or free/unwired GP22 (STOP latch).
`request_stop()` latches once. STOP is sampled five PIO ticks before frame end;
a later request can send one more complete copy. `poll()` remains `running`
between copies and returns `frame_boundary` only after EOF/STOP, full final
period, low output and DMA retirement. `completed_frames()` resets per burst.
After confirmed radio IDLE, call `abort(true)` to release pins/resources.
Faults park GP20 low; `abort(false)` keeps it low if radio IDLE is unknown.
Poll through `stopping` before reuse. Poll often enough to drain the four-word
completion FIFO; any dropped marker is a fault, never an inferred frame count.
The maximum DMA buffer is 41,024 bytes. The PIO program uses 31 instruction slots.

Build and verify the reserved journal layout, without flashing:

```sh
devenv shell
arduino-cli compile --profile yd-rp2040-4mb-journal --warnings all \
  --build-property compiler.cpp.extra_flags=-Werror \
  --build-path /tmp/ha-x2d-tx-check-build \
  --output-dir /tmp/ha-x2d-tx-check-output firmware/tx_check
python tools/check_uf2_layout.py /tmp/ha-x2d-tx-check-output/tx_check.ino.uf2
g++ -std=c++17 -O2 -Wall -Wextra -Werror \
  -Ilib/x2d-core/src firmware/tx_check/check_capture.cpp -o /tmp/check_tx_capture
/tmp/check_tx_capture
```

The linked profile reserves the existing raw journal. Do not mount or format a
filesystem or erase flash. Board execution and flashing are separate steps.
