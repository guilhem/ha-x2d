"""Collect the standalone passive RX debug stream (serialx + standard library)."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

MAX_LINE_BYTES = 4096
BLOCK_BYTES = 16384
SAMPLE_HZ = 400000


def integer(value, low=0, high=0xFFFFFFFF):
    return type(value) is int and low <= value <= high


def parse_line(line):
    if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
        raise ValueError("Oversized or truncated debug line")
    try:
        message = json.loads(line)
    except RecursionError as exc:
        raise ValueError("Excessive JSON nesting") from exc
    if (not isinstance(message, dict) or type(message.get("debug")) is not int
            or message["debug"] != 1 or not integer(message.get("seq"))
            or not integer(message.get("config_id"))):
        raise ValueError("Not an RX debug event")
    kind = message.get("type")
    if kind not in ("status", "config", "samples", "stopped", "error"):
        raise ValueError("Unknown debug event")
    if kind == "samples":
        payload = message.get("data_hex")
        fields = ("chunk", "capture_epoch", "block", "byte_offset", "sample_count",
                  "system_clock_hz", "divider_256", "overrun_blocks", "gap_events")
        wide = ("sample_index", "epoch_start_us", "sample_start_us", "dropped_samples")
        if (any(not integer(message.get(name)) for name in fields)
                or any(not integer(message.get(name), high=0xFFFFFFFFFFFFFFFF) for name in wide)
                or not isinstance(payload, str) or not 8 <= len(payload) <= 3072
                or len(payload) % 8 or any(c not in "0123456789abcdef" for c in payload)
                or type(message.get("loss")) is not bool
                or message["sample_count"] != len(payload) * 2
                or message["byte_offset"] % 4
                or message["byte_offset"] + len(payload) // 2 > BLOCK_BYTES
                or message["sample_index"] != message["block"] * BLOCK_BYTES * 4 + message["byte_offset"] * 4
                or message["system_clock_hz"] == 0 or message["divider_256"] == 0
                or message["divider_256"] != (message["system_clock_hz"] * 256 + SAMPLE_HZ // 2) // SAMPLE_HZ
                or message["sample_start_us"] != message["epoch_start_us"] +
                    message["sample_index"] * message["divider_256"] * 1000000 //
                    (message["system_clock_hz"] * 256)):
            raise ValueError("Invalid packed sample chunk")
    elif kind != "error":
        if (message.get("product") != "ha-x2d-rx-debug"
                or message.get("firmware") != "rx-debug-0.2.2"
                or message.get("timing") != "pio-in-pins-2-dma"
                or message.get("tx_enabled") is not False
                or type(message.get("rx_enabled")) is not bool
                or not isinstance(message.get("config"), dict)
                or not isinstance(message.get("counters"), dict)):
            raise ValueError("Incompatible debug firmware")
    return message


# Count transitions on the host; the firmware transfers only packed samples.
BYTE_SAMPLES = tuple(tuple((byte >> (2 * i)) & 3 for i in range(4)) for byte in range(256))
BYTE_STATS = tuple((s[0], s[-1], sum((a ^ b) & 1 for a, b in zip(s, s[1:])),
                    sum(not a & 2 and bool(b & 2) for a, b in zip(s, s[1:])),
                    sum(bool(v & 2) for v in s)) for s in BYTE_SAMPLES)


def summarize_samples(data, previous=None):
    edges = rises = high = 0
    for byte in data:
        first, last, transitions, carrier_rises, carrier_high = BYTE_STATS[byte]
        if previous is not None:
            edges += (previous ^ first) & 1
            rises += not previous & 2 and bool(first & 2)
        edges += transitions
        rises += carrier_rises
        high += carrier_high
        previous = last
    return edges, rises, high, previous


async def collect(args, output):
    from serialx import SerialException, create_serial_connection

    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES)
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    writer = None
    totals = {"chunks": 0, "samples": 0, "data_edges": 0, "carrier_rises": 0,
              "carrier_high_samples": 0, "loss_chunks": 0, "sequence_gaps": 0, "sample_gaps": 0}
    last_sequence = None
    last_status = None
    next_print = 0
    last_stream = None
    last_chunk = None
    previous_sample = None

    def record(direction, **fields):
        output.write(json.dumps({"host_time": datetime.now(timezone.utc).isoformat(),
                                 "host_monotonic_ns": time.monotonic_ns(),
                                 "direction": direction, **fields}, separators=(",", ":")) + "\n")
        output.flush()

    async def send(command):
        line = command.encode("ascii") + b"\n"
        record("tx", line=line.decode("ascii"))
        writer.write(line)
        await asyncio.wait_for(writer.drain(), 3)

    async def receive(timeout):
        nonlocal last_sequence, last_status, next_print, last_stream, last_chunk, previous_sample
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            raise ConnectionError("RX debug device disconnected")
        # Keep the exact wire line even when validation fails. No radio decoding.
        record("rx", line=line.decode("utf-8", errors="backslashreplace"))
        message = parse_line(line)
        if last_sequence is not None and message["seq"] != (last_sequence + 1) & 0xFFFFFFFF:
            totals["sequence_gaps"] += 1
        last_sequence = message["seq"]
        kind = message["type"]
        if kind == "error":
            raise ValueError(f"Firmware rejected command: {message.get('error')}")
        if kind == "samples":
            stream = (message["config_id"], message["capture_epoch"], message["sample_index"])
            if (last_stream is not None and stream != last_stream or
                    last_stream is None and message["sample_index"] != 0 or
                    last_chunk is not None and message["chunk"] != (last_chunk + 1) & 0xFFFFFFFF):
                totals["sample_gaps"] += 1
                previous_sample = None
            edges, rises, high, previous_sample = summarize_samples(bytes.fromhex(message["data_hex"]), previous_sample)
            totals["chunks"] += 1
            totals["samples"] += message["sample_count"]
            totals["data_edges"] += edges
            totals["carrier_rises"] += rises
            totals["carrier_high_samples"] += high
            totals["loss_chunks"] += message["loss"]
            last_chunk = message["chunk"]
            last_stream = (stream[0], stream[1], message["sample_index"] + message["sample_count"])
        else:
            last_status = message
            if time.monotonic() >= next_print:
                cfg, counters, radio = message["config"], message["counters"], message.get("radio", {})
                print(f"{cfg.get('mode')} {cfg.get('frequency_hz')}Hz "
                      f"RX={message['rx_enabled']} MARC={radio.get('marcstate')} "
                      f"RSSI≈{radio.get('rssi_dbm_approx')}dBm "
                      f"samples={totals['samples']} edges={totals['data_edges']} CS↑={totals['carrier_rises']} "
                      f"dropped={counters.get('dropped_samples')} overruns={counters.get('overrun_blocks')} "
                      f"gaps={counters.get('gap_events')}", flush=True)
                next_print = time.monotonic() + 5
        return message

    async def until(kind, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            message = await receive(max(0.001, deadline - time.monotonic()))
            if message["type"] == kind:
                return message
            if time.monotonic() >= deadline:
                raise TimeoutError(f"No {kind} response")

    record("meta", device=args.device, mode=args.mode, frequency_hz=args.frequency_hz,
           deviation_hz=args.deviation_hz, duration_s=args.duration)
    try:
        try:
            transport, _ = await asyncio.wait_for(create_serial_connection(
                loop, lambda: protocol, url=args.device, baudrate=115200, exclusive=True), 5)
        except SerialException as exc:
            raise ValueError("Cannot open the requested serial connection") from exc
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        await send("status")  # transport has configured raw mode before this command
        status = await until("status")
        if args.device_id and status.get("device_id") != args.device_id:
            raise ValueError("Unexpected physical device ID")
        if status.get("sampler_ready") is not True:
            raise ValueError("PIO/DMA sampler initialization failed")
        await send(f"rx {args.mode} {args.frequency_hz} {args.deviation_hz}")
        configured = await until("config")
        cfg = configured["config"]
        if (not configured["rx_enabled"] or cfg.get("mode") != args.mode
                or cfg.get("frequency_hz") != args.frequency_hz
                or cfg.get("deviation_hz") != args.deviation_hz):
            raise ValueError("Unexpected applied RX configuration")
        print(f"Ready: {status.get('device_id')} "
              f"GDO0={configured.get('gdo0_wiring_ok')} GDO2={configured.get('gdo2_wiring_ok')} "
              f"{args.duration:g}s → {args.output}", flush=True)
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            try:
                await receive(min(3, max(0.001, deadline - time.monotonic())))
            except TimeoutError:
                if time.monotonic() < deadline:
                    raise TimeoutError("No debug events for 3 seconds")
        await send("stop")
        await until("stopped")
    finally:
        if writer is not None:
            writer.close()  # firmware also stops RX when DTR disconnects
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                pass
        record("summary", totals=totals, last_status=last_status)
        print(f"Capture: {totals['samples']} samples / {totals['chunks']} chunks, "
              f"{totals['data_edges']} data edges, {totals['carrier_rises']} CS rises, "
              f"{totals['loss_chunks']} loss chunks, {totals['sample_gaps']} sample gaps, "
              f"{totals['sequence_gaps']} sequence gaps", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", help="Explicit serial path; one owner, no auto-discovery")
    parser.add_argument("--mode", choices=("fsk", "ook"), default="fsk")
    parser.add_argument("--frequency-hz", type=int, default=868350000)
    parser.add_argument("--deviation-hz", type=int, help="FSK deviation; default 38086 (OOK: 0)")
    parser.add_argument("--duration", type=float, default=600, help="1–600 seconds")
    parser.add_argument("--device-id", help="Optional expected physical flash ID")
    parser.add_argument("--output", type=Path, help="New timestamped JSONL file")
    args = parser.parse_args()
    if args.deviation_hz is None:
        args.deviation_hz = 0 if args.mode == "ook" else 38086
    if not 863000000 <= args.frequency_hz <= 870000000:
        parser.error("frequency must be 863000000–870000000 Hz")
    if (args.mode == "ook" and args.deviation_hz != 0 or
            args.mode == "fsk" and not 1000 <= args.deviation_hz <= 200000):
        parser.error("deviation must be 0 for OOK, or 1000–200000 Hz for FSK")
    if not 1 <= args.duration <= 600:
        parser.error("duration must be 1–600 seconds")
    if args.output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        args.output = Path(__file__).resolve().parents[1] / "build/captures" / f"{stamp}-{args.mode}-{args.frequency_hz}.jsonl"
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            asyncio.run(collect(args, output))
    except KeyboardInterrupt:
        parser.exit(130, "Capture interrupted; partial JSONL preserved.\n")
    except (OSError, ValueError, TimeoutError, ImportError) as exc:
        parser.exit(1, f"Capture failed: {exc}\n")


if __name__ == "__main__":
    main()
