"""Offline X2D-compatible BMC analysis of collect_rx_debug JSONL; stdlib only.

Reports contain captured identities and frames: keep private reports in build/.
No correction, voting, USB access or transmission. Counts are lower bounds.
"""

import argparse
from bisect import bisect_left
import hashlib
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
from verify_public_samples import transform

UNPACK = tuple(bytes((byte >> (2 * i)) & 1 for i in range(4)) for byte in range(256))
PREFIX = re.compile(b"\x00{7,}\x01{6,}\x00")
EOF = re.compile(b"\x01{8}\x00")


def read_segments(path, integrity):
    """Yield contiguous GDO0 segments; indices/times stay in the capture epoch.

    Clock rate is system_clock_hz * 256 / divider_256, not the CC1101 bitrate.
    GDO2 occupies the other packed bit and is deliberately not used as data.
    """
    digest = hashlib.sha256()
    segment = previous = None
    sequence = None
    config = {}
    reasons = ["capture_start"]
    integrity.update(chunks=0, samples=0, sequence_gaps=0, loss_chunks=0)
    with Path(path).open("rb") as source:
        for number, line in enumerate(source, 1):
            digest.update(line)
            try:
                if len(line) > 16384 or not line.endswith(b"\n"):
                    raise ValueError("oversized or truncated JSONL record")
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("expected collector record")
                if record.get("direction") != "rx":
                    continue
                message = json.loads(record["line"])
                if (not isinstance(message, dict) or type(message.get("debug")) is not int
                        or message["debug"] != 1
                        or type(message.get("seq")) is not int
                        or not 0 <= message["seq"] <= 0xFFFFFFFF):
                    raise ValueError("expected RX debug event")
                if sequence is not None and message["seq"] != (sequence + 1) & 0xFFFFFFFF:
                    reasons.append("sequence_gap")
                    integrity["sequence_gaps"] += 1
                sequence = message["seq"]
                kind = message["type"]
                if kind != "samples":
                    if kind not in ("config", "status", "stopped", "error"):
                        raise ValueError("unknown RX debug event")
                    if kind in ("config", "stopped", "error"):
                        if segment is not None:
                            yield segment
                            segment = previous = None
                        reasons.append(kind)
                    if kind in ("config", "status"):
                        if (message.get("timing") != "pio-in-pins-2-dma"
                                or message.get("tx_enabled") is not False
                                or not isinstance(message.get("config"), dict)):
                            raise ValueError("expected passive PIO/DMA metadata")
                        config = message["config"].copy()
                    continue
                names = ("config_id", "capture_epoch", "epoch_start_us", "sample_index",
                         "sample_count", "sample_start_us", "system_clock_hz", "divider_256",
                         "chunk", "block", "byte_offset", "dropped_samples", "overrun_blocks",
                         "gap_events")
                wide = ("epoch_start_us", "sample_index", "sample_start_us", "dropped_samples")
                if any(type(message.get(k)) is not int or
                       not 0 <= message[k] < 1 << (64 if k in wide else 32) for k in names):
                    raise ValueError("invalid sample metadata")
                clock, divider = message["system_clock_hz"], message["divider_256"]
                payload = message["data_hex"]
                if (not clock or not divider or type(message.get("loss")) is not bool
                        or not isinstance(payload, str) or not 8 <= len(payload) <= 3072
                        or len(payload) % 8 or re.fullmatch("[0-9a-fA-F]+", payload) is None):
                    raise ValueError("invalid clock or packed payload")
                raw = bytes.fromhex(payload)
                index, count = message["sample_index"], message["sample_count"]
                if (count != len(raw) * 4 or message["byte_offset"] % 4
                        or message["byte_offset"] + len(raw) > 16384
                        or index != message["block"] * 65536 + message["byte_offset"] * 4
                        or message["sample_start_us"] != message["epoch_start_us"] +
                        index * divider * 1000000 // (clock * 256)):
                    raise ValueError("inconsistent sample count, index or timestamp")
                key = tuple(message[k] for k in ("config_id", "capture_epoch", "epoch_start_us",
                            "system_clock_hz", "divider_256", "dropped_samples",
                            "overrun_blocks", "gap_events"))
                if previous is not None:
                    if key != previous[0]:
                        reasons.append("epoch_clock_config_or_loss_counters")
                    if index != previous[1] or message["chunk"] != (previous[2] + 1) & 0xFFFFFFFF:
                        reasons.append("sample_or_chunk_gap")
                if message["loss"]:
                    reasons.append("loss_flag")
                    integrity["loss_chunks"] += 1
                if reasons or segment is None:
                    if segment is not None:
                        yield segment
                    segment = {k: message[k] for k in names[:4]}
                    segment.update(system_clock_hz=clock, divider_256=divider,
                                   sample_hz=clock * 256 / divider, config=config.copy(),
                                   boundary_reasons=sorted(set(reasons)), signal=bytearray())
                    reasons = []
                segment["signal"].extend(b"".join(UNPACK[b] for b in raw))
                previous = key, index + count, message["chunk"]
                integrity["chunks"] += 1
                integrity["samples"] += count
            except (ValueError, KeyError, TypeError, RecursionError) as exc:
                raise ValueError(f"invalid capture record at line {number}") from exc
    if segment is not None:
        yield segment
    integrity["sha256"] = digest.hexdigest()


def unstuff(bits):
    out = bytearray()
    ones = 0
    for bit in bits:
        if ones == 5:
            if bit:
                return None
            ones = 0
            continue
        out.append(bit)
        ones = ones + 1 if bit else 0
    return None if ones == 5 else bytes(out)


def decode_chips(chips, centers, max_frame_bytes=32):
    """Yield strictly framed, byte-aligned, checksum-valid bodies, both phases."""
    for phase in (0, 1):
        bits = bytes(chips[i] ^ chips[i + 1] for i in range(phase, len(chips) - 1, 2))
        ends = [m.start() for m in EOF.finditer(bits)]
        starts = sorted({m.end() for m in PREFIX.finditer(bits)} |
                        {p + 16 for p in ends if bits[p + 9:p + 16] == b"\x01" * 6 + b"\x00"})
        for end in ends:
            # Stuffing adds at most one bit for each five payload bits.
            lower = bisect_left(starts, end - max_frame_bytes * 8 * 6 // 5)
            upper = bisect_left(starts, end - 63)
            for start in starts[lower:upper]:
                first, last = phase + start * 2, phase + (end + 9) * 2
                if any(chips[i] == chips[i - 1] for i in range(first + 2, last, 2)):
                    continue
                data = unstuff(bits[start:end])
                if data is None or len(data) % 8:
                    continue
                frame = bytes(sum(data[i + j] << j for j in range(8))
                              for i in range(0, len(data), 8))
                if (not 8 <= len(frame) <= max_frame_bytes or
                        (-sum(frame[:-2])) & 0xFFFF != int.from_bytes(frame[-2:], "big")):
                    continue
                yield {"sample_start": centers[first], "sample_end": centers[last - 1],
                       "chip_start": first, "chip_stop_exclusive": last,
                       "frame_hex": frame.hex(), "additive_checksum_ok": True,
                       "bmc_boundary_violations": 0}


def pulse_blocks(signal, start, end, period, bias):
    """Retain every edge; quantize runs to one/two chips or break the block."""
    chips, centers = bytearray(), []
    for match in re.finditer(b"\x00+|\x01+", signal[start:end]):
        a, b = (start + v for v in match.span())
        value = signal[a]
        count = round((b - a - bias * (1 if value else -1)) / period)
        if count not in (1, 2):
            if chips:
                yield bytes(chips), centers
            chips, centers = bytearray(), []
            continue
        chips.extend([value] * count)
        centers.extend(a + int((j + 0.5) * (b - a) / count) for j in range(count))
    if chips:
        yield bytes(chips), centers


def activity_groups(signal, hz, chip_us, gap_s, min_pulses):
    """Long high runs survive noisy FSK; ignore constant idle-high stretches."""
    minimum = max(1, round(hz * min(chip_us) * 1e-6 * 1.9))
    maximum = hz * max(chip_us) * 1e-6 * 4
    groups = []
    for match in re.finditer(b"\x01{" + str(minimum).encode() + b",}", signal):
        start, end = match.span()
        if end - start > maximum:
            continue
        if not groups or start - groups[-1][1] > hz * gap_s:
            groups.append([start, end, 1])
        else:
            groups[-1][1] = end
            groups[-1][2] += 1
    return [g for g in groups if g[2] >= min_pulses]


def frame_fields(frame):
    """The 12-byte profile observed privately; other lengths remain raw bodies."""
    fields = {"frame_hex": frame.hex(), "length_bytes": len(frame),
              "checksum_be": frame[-2:].hex()}
    if len(frame) == 12:
        identity = int.from_bytes(frame[:3], "big")
        rolling = int.from_bytes(frame[8:10], "little")
        fields.update(identity_rf_bytes=frame[:3].hex(), header_bytes=frame[3:6].hex(),
                      command_bytes=frame[6:8].hex(), rolling_rf_bytes=frame[8:10].hex(),
                      rolling_word_little_endian=rolling,
                      rolling_clear=transform(rolling, identity, inverse=True))
    return fields


def analyze_capture(path, *, chip_us=(208, 208.5, 209), thresholds=(0.76, 0.82, 0.88, 0.98),
                    bias_us=(0, 25, 50, 75, 100, 125, 150, 175), window_us=125,
                    phase_step_us=15, gap_s=0.3, min_pulses=12,
                    method="both", max_frame_bytes=32):
    """Return a JSON-serializable report; default search is a bounded lower bound."""
    positive = (*chip_us, window_us, phase_step_us, gap_s)
    if (not chip_us or not thresholds or not bias_us or
            any(not math.isfinite(v) or v <= 0 for v in positive) or
            any(not math.isfinite(v) or not 0 < v <= 1 for v in thresholds) or
            any(not math.isfinite(v) or v < 0 for v in bias_us) or
            min_pulses < 1 or not 8 <= max_frame_bytes <= 256 or
            method not in ("grid", "pulse", "both")):
        raise ValueError("invalid analysis parameters")
    integrity, segments, groups, frames = {}, [], [], []
    for segment_id, segment in enumerate(read_segments(path, integrity), 1):
        signal = segment.pop("signal")
        segment.update(segment=segment_id, sample_count=len(signal))
        segments.append(segment)
        hz = segment["sample_hz"]
        periods = [hz * us * 1e-6 for us in chip_us]
        half = max(1, round(hz * window_us * 1e-6 / 2))
        step = max(1, round(hz * phase_step_us * 1e-6))
        # The first/last qualifying long run can lie inside the data body.
        padding = math.ceil((max_frame_bytes * 8 * 6 / 5 * 2 + 32) * max(periods))
        for a, b, pulses in activity_groups(signal, hz, chip_us, gap_s, min_pulses):
            group_id = len(groups) + 1
            start, end = max(0, a - padding), min(len(signal), b + padding)
            hits = []
            for period in periods:
                if method in ("grid", "both"):
                    for phase in range(0, math.ceil(2 * period), step):
                        # Integer grid avoids accumulating floating-point clock drift.
                        period100 = round(period * 100)
                        centers = range((end - start - phase) * 100 // period100)
                        centers = [start + phase + i * period100 // 100 for i in centers]
                        levels = [signal[max(0, c - half):min(len(signal), c + half)].count(1)
                                  for c in centers]
                        for threshold in thresholds:
                            chips = bytes(int(v >= threshold * 2 * half) for v in levels)
                            hits.extend({**h, "method": "grid", "chip_period_samples": period100 / 100,
                                         "phase_samples": phase, "threshold": threshold,
                                         "grid_base_sample": start + segment["sample_index"],
                                         "window_samples": 2 * half}
                                        for h in decode_chips(chips, centers, max_frame_bytes))
                if method in ("pulse", "both"):
                    for us in bias_us:
                        for chips, centers in pulse_blocks(signal, start, end, period, hz * us * 1e-6):
                            hits.extend({**h, "method": "pulse", "chip_period_samples": period,
                                         "bias_us": us,
                                         "block_first_chip_sample": centers[0] + segment["sample_index"],
                                         "scan_start_sample": start + segment["sample_index"],
                                         "scan_end_sample": end + segment["sample_index"]}
                                        for h in decode_chips(chips, centers, max_frame_bytes))
            copies = []
            for hit in sorted(hits, key=lambda h: h["sample_start"]):
                hit["sample_start"] += segment["sample_index"]
                hit["sample_end"] += segment["sample_index"]
                # Compare to the first hit, not a chaining neighborhood that can grow.
                copy = next((c for c in reversed(copies)
                             if hit["sample_start"] - c["sample_start"] <= max(periods)
                             and c["frame_hex"] == hit["frame_hex"]), None)
                if copy is None:
                    copy = next((c for c in reversed(frames) if c["segment"] == segment_id
                                 and abs(hit["sample_start"] - c["sample_start"]) <= max(periods)
                                 and c["frame_hex"] == hit["frame_hex"]), None)
                if copy is not None:
                    copy["search_variants"] += 1
                    continue
                hit.update(group=group_id, segment=segment_id, search_variants=1)
                copies.append(hit)
            for copy in copies:
                for key in ("sample_start", "sample_end"):
                    copy[key + "_epoch_s"] = copy[key] / hz
                    copy[key + "_us"] = segment["epoch_start_us"] + copy[key] / hz * 1e6
            bodies = []
            for encoded in dict.fromkeys(c["frame_hex"] for c in copies):
                body = frame_fields(bytes.fromhex(encoded))
                body["validated_physical_copies"] = sum(c["frame_hex"] == encoded for c in copies)
                bodies.append(body)
            groups.append({"group": group_id, "segment": segment_id,
                           "start_sample": a + segment["sample_index"],
                           "end_sample": b + segment["sample_index"], "activity_pulses": pulses,
                           "validated_physical_copies": len(copies), "bodies": bodies})
            frames.extend(copies)
    counters = {}
    for group in groups:
        for body in group["bodies"]:
            if "rolling_clear" in body:
                previous = counters.get(body["identity_rf_bytes"])
                body["counter_delta_mod65536"] = (None if previous is None else
                                                  (body["rolling_clear"] - previous) & 0xFFFF)
                counters[body["identity_rf_bytes"]] = body["rolling_clear"]
    return {"format_observed": "X2D-compatible-biphase-mark", "integrity": integrity,
            "parameters": {"chip_us": chip_us, "thresholds": thresholds, "bias_us": bias_us,
                           "window_us": window_us, "phase_step_us": phase_step_us,
                           "gap_s": gap_s, "min_pulses": min_pulses, "method": method,
                           "max_frame_bytes": max_frame_bytes},
            "segments": segments, "groups": groups, "physical_frames": frames,
            "validated_physical_copies": len(frames), "error_correction": False,
            "count_is_lower_bound": True,
            "limitations": ["Bounded clock/phase/threshold/bias search can miss noisy copies or entire groups.",
                            "An additive checksum is not a CRC or proof of authenticity.",
                            "12-byte fields are a candidate profile; command bytes have no action label.",
                            "Identity key is symmetric under byte reversal; counters do not prove ID order.",
                            "Times locate data-chip centers within device epochs, not RF edges or host time.",
                            "Receiver configuration does not establish transmitter modulation/frequency."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path, help="New private JSON report; otherwise stdout")
    for name, default in (("chip-us", "208,208.5,209"), ("thresholds", "0.76,0.82,0.88,0.98"),
                          ("bias-us", "0,25,50,75,100,125,150,175")):
        parser.add_argument("--" + name, default=default, help="Comma-separated search values")
    parser.add_argument("--method", choices=("grid", "pulse", "both"), default="both")
    parser.add_argument("--window-us", type=float, default=125)
    parser.add_argument("--phase-step-us", type=float, default=15)
    parser.add_argument("--gap-s", type=float, default=0.3)
    parser.add_argument("--min-pulses", type=int, default=12)
    parser.add_argument("--max-frame-bytes", type=int, default=32)
    args = vars(parser.parse_args())
    path, output = args.pop("capture"), args.pop("output")
    try:
        for name in ("chip_us", "thresholds", "bias_us"):
            args[name] = tuple(float(v) for v in args[name].split(","))
        report = analyze_capture(path, **args)
        text = json.dumps(report, indent=2) + "\n"
        if output is None:
            print(text, end="")
        else:
            with output.open("x", encoding="utf-8") as destination:
                destination.write(text)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Analysis failed: {exc}\n")


if __name__ == "__main__":
    main()
