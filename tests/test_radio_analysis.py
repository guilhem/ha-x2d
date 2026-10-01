"""Synthetic RX/BMC vectors only; no private identities, hardware or dependencies."""

import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.analyze_rx import (UNPACK, analyze_capture, decode_chips, frame_fields,
                              read_segments, transform, unstuff)


def body(counter=40, command=b"\x22\x04"):
    identity = 0x123456  # Synthetic identity, never an installation identity.
    data = identity.to_bytes(3, "big") + bytes.fromhex("010598") + command
    data += transform(counter, identity).to_bytes(2, "little")
    return data + ((-sum(data)) & 0xFFFF).to_bytes(2, "big")


def stuffed(frame):
    bits, ones = bytearray(), 0
    for byte in frame:
        for i in range(8):
            bit = (byte >> i) & 1
            bits.append(bit)
            ones = ones + 1 if bit else 0
            if ones == 5:
                bits.append(0)
                ones = 0
    return bytes(bits)


def encode(frames, polarity=0):
    bits = b"\x00" * 8 + b"\x01" * 6 + b"\x00"
    bits += (b"\x01" * 6 + b"\x00").join(stuffed(f) + b"\x01" * 8 + b"\x00"
                                             for f in frames)
    chips, level = bytearray(), polarity
    for bit in bits:
        level ^= 1
        chips.append(level)
        level ^= bit
        chips.append(level)
    return bytes(chips)


def signal_for(frames, bias=0, polarity=0):
    chips = encode(frames, polarity)
    # Every actual edge retained, including configurable OOK pulse widening.
    signal = bytearray(b"\x00" * 2000)
    for match in re.finditer(b"\x00+|\x01+", chips):
        a, b = match.span()
        duration = round(b * 83.4) - round(a * 83.4) + bias * (1 if chips[a] else -1)
        signal.extend(bytes([chips[a]]) * duration)
    signal.extend(b"\x00" * 2000)
    signal.extend(b"\x00" * (-len(signal) % 16))
    return signal


def sample_records(signal, epoch=1, clock=200000000, divider=128000):
    records = []
    for index in range(0, len(signal), 4096):
        part = signal[index:index + 4096]
        # Set GDO2 high independently: it must not contaminate the GDO0 decoder.
        packed = bytes(sum((part[i + j] | 2) << (j * 2) for j in range(4))
                       for i in range(0, len(part), 4))
        message = dict(debug=1, type="samples", seq=len(records), config_id=1,
                       capture_epoch=epoch, epoch_start_us=1000000,
                       sample_index=index, sample_count=len(part),
                       sample_start_us=1000000 + index * divider * 1000000 // (clock * 256),
                       system_clock_hz=clock, divider_256=divider, chunk=len(records),
                       block=index // 65536, byte_offset=index % 65536 // 4,
                       loss=False, dropped_samples=0, overrun_blocks=0, gap_events=0,
                       data_hex=packed.hex())
        records.append(message)
    return records


class RadioAnalysisTests(unittest.TestCase):
    def write_capture(self, directory, messages):
        path = Path(directory) / "synthetic.jsonl"
        path.write_text("".join(json.dumps({"direction": "rx", "line": json.dumps(m) + "\n"})
                                + "\n" for m in messages))
        return path

    def test_bmc_stuffing_checksum_and_rejection(self):
        self.assertEqual(UNPACK[0xE4], bytes((0, 1, 0, 1)))
        self.assertIsNone(unstuff(b"\x01" * 6))
        self.assertIsNone(unstuff(b"\x01" * 5))
        self.assertEqual(unstuff(b"\x01" * 5 + b"\x00\x00"), b"\x01" * 5 + b"\x00")
        for polarity in (0, 1):
            chips = encode([body(), body()], polarity)
            hits = list(decode_chips(chips, list(range(len(chips)))))
            self.assertEqual([h["frame_hex"] for h in hits], [body().hex()] * 2)
            fields = frame_fields(bytes.fromhex(hits[0]["frame_hex"]))
            self.assertEqual(fields["rolling_clear"], 40)
            bad_boundary = bytearray(chips)
            first = hits[0]["sample_start"]
            bad_boundary[first + 2] = bad_boundary[first + 1]
            self.assertEqual(len(list(decode_chips(bad_boundary, list(range(len(chips)))))), 1)
        corrupt = bytearray(body())
        corrupt[6] ^= 1
        chips = encode([corrupt])
        self.assertEqual(list(decode_chips(chips, list(range(len(chips))))), [])
        chips = encode([body()])
        self.assertEqual(list(decode_chips(chips[:-12], list(range(len(chips) - 12)))), [])

        # Public HACF export in research/verify_public_samples.py; synthetic BMC
        # encoding of its candidate header, not a public raw X2D capture.
        public = bytes.fromhex("9231f7010598220400dce2fbc4")
        public_chips = encode([public])
        public_hits = list(decode_chips(public_chips, list(range(len(public_chips)))))
        self.assertEqual([h["frame_hex"] for h in public_hits], [public.hex()])
        self.assertEqual(transform(0xE2DC, 0xF73192, inverse=True), 6641)

    def test_jsonl_to_repetitions_and_counter_fields(self):
        signal = signal_for([body()] * 3) + b"\x00" * 160000 + signal_for([body(41)] * 3)
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_capture(directory, sample_records(signal))
            report = analyze_capture(path)
        self.assertEqual(report["validated_physical_copies"], 6)
        self.assertEqual(len(report["segments"]), 1)
        self.assertEqual([g["validated_physical_copies"] for g in report["groups"]], [3, 3])
        bodies = [g["bodies"][0] for g in report["groups"]]
        self.assertEqual([b["rolling_clear"] for b in bodies], [40, 41])
        self.assertEqual([b["counter_delta_mod65536"] for b in bodies], [None, 1])
        self.assertTrue(all(f["search_variants"] > 1 for f in report["physical_frames"]))
        self.assertTrue(report["count_is_lower_bound"])

    def test_pulse_bias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_capture(directory, sample_records(signal_for([body()] * 3, bias=50)))
            report = analyze_capture(path, method="pulse", bias_us=(125,))
        self.assertEqual(report["validated_physical_copies"], 3)

    def test_epoch_reset_and_sequence_wrap_preserve_distinct_copies(self):
        messages = sample_records(signal_for([body()] * 3))
        second = sample_records(signal_for([body(41)] * 3), epoch=2)
        for i, message in enumerate(messages + second):
            message["seq"] = (0xFFFFFFFD + i) & 0xFFFFFFFF
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_capture(directory, messages + second)
            report = analyze_capture(path, method="grid")
        self.assertEqual(len(report["segments"]), 2)
        self.assertEqual(report["integrity"]["sequence_gaps"], 0)
        self.assertEqual(report["validated_physical_copies"], 6)
        self.assertEqual([s["sample_index"] for s in report["segments"]], [0, 0])

    def test_boundaries_never_join_a_frame(self):
        messages = sample_records(signal_for([body()]))
        for reason in ("sequence", "sample", "chunk", "config", "epoch", "loss", "clock", "counters"):
            altered = [m.copy() for m in messages]
            for m in altered[2:]:
                if reason == "sequence":
                    m["seq"] += 1
                elif reason == "sample":
                    m["sample_index"] += 16
                    m["byte_offset"] += 4
                    m["sample_start_us"] += 40
                elif reason == "epoch":
                    m["capture_epoch"] += 1
                elif reason == "chunk":
                    m["chunk"] += 1
                elif reason == "config":
                    m["config_id"] += 1
                elif reason == "clock":
                    m["divider_256"] += 1
                    m["sample_start_us"] = (m["epoch_start_us"] + m["sample_index"] *
                                            m["divider_256"] * 1000000 // (m["system_clock_hz"] * 256))
                elif reason == "counters":
                    m["gap_events"] += 1
            if reason == "loss":
                altered[2]["loss"] = True
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                path = self.write_capture(directory, altered)
                self.assertEqual(analyze_capture(path)["validated_physical_copies"], 0)
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_capture(directory, sample_records(signal_for([body()]), divider=128001))
            segments = list(read_segments(path, {}))
            self.assertAlmostEqual(segments[0]["sample_hz"], 200000000 * 256 / 128001)

    def test_bad_metadata_and_options_fail_closed(self):
        messages = sample_records(signal_for([body()]))
        for key, value in (("sample_count", 1), ("data_hex", "ff z"),
                           ("sample_start_us", 0), ("divider_256", 0)):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                bad = messages[0] | {key: value}
                path = self.write_capture(directory, [bad])
                with self.assertRaisesRegex(ValueError, "line 1"):
                    analyze_capture(path)
        with self.assertRaises(ValueError):
            analyze_capture("unused", chip_us=(float("nan"),))


if __name__ == "__main__":
    unittest.main()
