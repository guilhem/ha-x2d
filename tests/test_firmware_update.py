"""Host OTA wire/state-machine tests with synthetic serial traffic, never hardware."""

from collections import deque
import contextlib
import io
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import update_firmware as ota
from tools import firmware_layout as layout

DEVICE = "0123456789ABCDEF"
IDENTITY = f"1;255;3;0;9;ota_id:{DEVICE}\n".encode()
CURRENT = b"1;255;4;0;0;3258050000000000\n"
STAGED = b"1;255;3;0;9;ota_staged\n"

def binary(size=ota.APP_OFFSET + 33):
    """Synthetic whole image with the authoritative map, vectors and OTA marker."""
    data = bytearray(index % 256 for index in range(size))
    struct.pack_into("<4I", data, ota.APP_OFFSET - 16, *layout.PARTITION)
    struct.pack_into("<2I", data, ota.APP_OFFSET, 0x20042000, 0x10003009)
    marker = layout.IMAGE_MARKER + b"5:"
    data[ota.APP_OFFSET + 8:ota.APP_OFFSET + 8 + len(marker)] = marker
    return bytes(data)


IMAGE = binary()
FIRMWARE = ota.Firmware.from_bytes(IMAGE, 5)
ROOT = Path(__file__).resolve().parents[1]
SERVER = Path(os.environ.get("X2D_MYSENSORS_SERVER", ROOT / "build/native/mysensors_server"))


def request(index, kind=ota.FIRMWARE_TYPE, version=5):
    return ota.stream_message(2, struct.pack("<HHH", kind, version, index).hex().upper())


def remaining_requests(*already_sent):
    return b"".join(request(index) for index in range(FIRMWARE.blocks) if index not in already_sent)


def fragment(data):
    offset = 0
    while offset < len(data):
        size = offset % 5 + 1
        yield data[offset:offset + size]
        offset += size


class FakeSerial:
    """Each bounded read advances fake monotonic time, including empty reads."""

    def __init__(self, chunks=(), *, step=0.01):
        self.chunks = deque(chunks)
        self.writes = []
        self.now = 0
        self.step = step
        self.short_write = False

    def clock(self):
        return self.now

    def read(self, size):
        self.now += self.step
        value = self.chunks.popleft() if self.chunks else b""
        if isinstance(value, Exception):
            raise value
        if len(value) > size:
            self.chunks.appendleft(value[size:])
            value = value[:size]
        return value

    def write(self, packet):
        self.writes.append(packet)
        return len(packet) - int(self.short_write)


def run(serial, **kwargs):
    return ota.stage_firmware(serial, FIRMWARE, DEVICE, clock=serial.clock, **kwargs)


class EncodingTests(unittest.TestCase):
    def test_compiled_version_must_match_transfer_identity(self):
        with self.assertRaisesRegex(ValueError, "compiled image"):
            ota.Firmware.from_bytes(IMAGE, 6)

    def test_crc_known_modbus_vectors(self):
        self.assertEqual(ota.crc16(b""), 0xFFFF)
        self.assertEqual(ota.crc16(b"123456789"), 0x4B37)
        self.assertEqual(ota.crc16(b"\x01\x03\x00\x00\x00\x0A"), 0xCDC5)

    def test_installed_pymysensors_crc_and_field_order_agree(self):
        try:
            from mysensors.ota import compute_crc, fw_int_to_hex
        except ImportError:
            self.skipTest("upstream comparison requires locked pymysensors; use uv run")
        for data in (b"", b"123456789", bytes(range(256)), FIRMWARE.data):
            with self.subTest(size=len(data)):
                self.assertEqual(ota.crc16(data), compute_crc(data))
        payload = fw_int_to_hex(ota.FIRMWARE_TYPE, 5, FIRMWARE.blocks, FIRMWARE.checksum).upper()
        self.assertEqual(FIRMWARE.config_response(), ota.stream_message(1, payload))

    def test_little_endian_uppercase_config_and_exact_block_response(self):
        firmware = ota.Firmware(FIRMWARE.data, 0x1234, 0xABCD)
        self.assertEqual(firmware.config_response(), b"1;255;4;0;1;325834120803CDAB\n")
        index, response = firmware.block_response("325834120100")
        self.assertEqual(index, 1)
        self.assertEqual(response, b"1;255;4;0;3;325834120100101112131415161718191A1B1C1D1E1F\n")
        self.assertEqual(len(response.decode().strip().split(";")[5]), 44)

    def test_image_validation_padding_and_crc_cover_full_prefix(self):
        data = binary()
        with patch.object(layout, "validate_binary", wraps=layout.validate_binary) as validator:
            firmware = ota.Firmware.from_bytes(data, 5)
        validator.assert_called_once_with(data)
        self.assertEqual(firmware.data, data + b"\xff" * 95)
        self.assertEqual(firmware.blocks, 776)
        self.assertEqual(firmware.checksum, ota.crc16(data + b"\xff" * 95))
        self.assertNotEqual(firmware.checksum, ota.crc16(firmware.data[ota.APP_OFFSET:]))

    def test_no_extra_block_when_image_is_already_aligned(self):
        data = binary(ota.APP_OFFSET + 128)
        self.assertEqual(ota.Firmware.from_bytes(data, 5).data, data)

    def test_size_version_and_shared_layout_rejection(self):
        for size in (0, ota.APP_OFFSET, ota.MAX_IMAGE_SIZE + 1):
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, "binary size"):
                ota.Firmware.from_bytes(b"\x00" * size, 5)
        for version in (-1, 65536, 1.0, True):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "version"):
                ota.Firmware.from_bytes(IMAGE, version)
        for offset, reason in ((ota.APP_OFFSET - 16, "flash layout"),
                               (ota.APP_OFFSET, "vectors"),
                               (ota.APP_OFFSET + 8, "OTA-capable")):
            data = bytearray(IMAGE)
            data[offset:offset + 4] = b"\x00" * 4
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                ota.Firmware.from_bytes(data, 5)

    def test_maximum_block_count_and_last_block(self):
        firmware = ota.Firmware.from_bytes(binary(layout.MAX_IMAGE_SIZE), 5)
        self.assertEqual(firmware.blocks, 65528)
        self.assertEqual(firmware.block_response("32580500F7FF")[0], 65527)
        with self.assertRaisesRegex(ota.UpdateError, "out of range"):
            firmware.block_response("32580500F8FF")


class TransferTests(unittest.TestCase):
    def test_standard_five_word_announcement_and_legacy_four_word_migration(self):
        standard = ota.stream_message(0, struct.pack("<5H", ota.FIRMWARE_TYPE, 4, 776, 0xABCD, 1).hex())
        for current in (standard, CURRENT):
            with self.subTest(config=current):
                serial = FakeSerial([IDENTITY + current + remaining_requests() + STAGED])
                self.assertEqual(run(serial), FIRMWARE.blocks)
                self.assertEqual(serial.writes[-1], ota.REBOOT_RECEIPT)

    def test_fragmented_input_coalescing_noise_crlf_and_staged_completion(self):
        noise = b"boot log\n0;255;3;0;14;Gateway startup complete\n1;1;4;0;2;broken\n"
        traffic = noise + IDENTITY + CURRENT + request(1) + remaining_requests(1) + STAGED
        serial = FakeSerial(fragment(traffic.replace(b"\n", b"\r\n")))
        self.assertEqual(run(serial), FIRMWARE.blocks)
        self.assertEqual(serial.writes, [ota.stream_message(0), FIRMWARE.config_response(),
                                       FIRMWARE.block_response("325805000100")[1]] + [
                            FIRMWARE.block_response(struct.pack("<HHH", ota.FIRMWARE_TYPE, 5, i).hex())[1]
                            for i in range(FIRMWARE.blocks) if i != 1] + [ota.REBOOT_RECEIPT])

    def test_config_before_identity_is_held_until_exact_identity(self):
        serial = FakeSerial([CURRENT, b"", IDENTITY, remaining_requests(), STAGED])
        self.assertEqual(run(serial), FIRMWARE.blocks)
        self.assertEqual(serial.writes[1], FIRMWARE.config_response())
        serial = FakeSerial([CURRENT], step=0.1)
        with self.assertRaisesRegex(ota.UpdateError, "Discovery timeout"):
            run(serial, discovery_timeout=0.5)
        self.assertTrue(all(packet == ota.stream_message(0) for packet in serial.writes))

    def test_repeated_config_and_blocks_are_idempotent(self):
        serial = FakeSerial([IDENTITY + CURRENT + CURRENT + request(1) + request(1)
                             + CURRENT + remaining_requests(1) + STAGED])
        self.assertEqual(run(serial), FIRMWARE.blocks)
        configs = [packet for packet in serial.writes if packet.startswith(b"1;255;4;0;1;")]
        self.assertEqual(configs, [FIRMWARE.config_response()] * 3)
        blocks = [packet for packet in serial.writes if packet.startswith(b"1;255;4;0;3;")]
        self.assertEqual(blocks[0], blocks[1])

    def test_wrong_identity_never_sends_image_metadata_or_blocks(self):
        for chunks in ([IDENTITY.replace(DEVICE.encode(), b"FEDCBA9876543210")],
                       [CURRENT, IDENTITY.replace(DEVICE.encode(), b"FEDCBA9876543210")]):
            serial = FakeSerial(chunks)
            with self.subTest(chunks=chunks), self.assertRaisesRegex(ota.UpdateError, "Wrong device"):
                run(serial)
            self.assertEqual(serial.writes, [ota.stream_message(0)])

    def test_invalid_identity_and_identity_changes_abort(self):
        for identity in (b"1;255;3;0;9;ota_id:XYZ\n",
                         b"1;255;3;0;9;ota_id:0123456789ABCDEF0\n"):
            with self.subTest(identity=identity), self.assertRaisesRegex(ota.UpdateError, "Invalid OTA"):
                run(FakeSerial([identity]))
        changed = IDENTITY.replace(DEVICE.encode(), b"FFFFFFFFFFFFFFFF")
        with self.assertRaisesRegex(ota.UpdateError, "Wrong device"):
            run(FakeSerial([IDENTITY + CURRENT + request(0) + changed + request(1)]))

    def test_discovery_probe_retries_and_timeout(self):
        serial = FakeSerial(step=0.1)
        with self.assertRaisesRegex(ota.UpdateError, "Discovery timeout"):
            run(serial, discovery_timeout=0.8, retry_interval=0.2)
        self.assertGreaterEqual(len(serial.writes), 3)
        self.assertTrue(all(packet == ota.stream_message(0) for packet in serial.writes))

    def test_invalid_config_and_requests_abort_without_a_block_response(self):
        cases = [(b"1;255;4;0;0;32580500\n", "Malformed"),
                 (b"1;255;4;0;0;FFFF050000000000\n", "firmware type"),
                 (b"1;255;4;0;2;32580500000Z\n", "Malformed"),
                 (b"1;255;4;0;2;32580500 0000\n", "Malformed"),
                 (b"1;255;4;0;2;325805000000FF\n", "Malformed"),
                 (request(0, kind=1), "type/version"),
                 (request(0, version=6), "type/version"),
                 (request(FIRMWARE.blocks), "out of range"),
                 (request(65535), "out of range"),
                 (b"1;255;4;9;2;325805000000\n", "Malformed"),
                 (b"1;255;4;0;2;\xff\n", "Malformed"),
                 (b"1;255;4;0\n", "Malformed")]
        for traffic, reason in cases:
            serial = FakeSerial([IDENTITY + CURRENT, traffic])
            with self.subTest(traffic=traffic), self.assertRaisesRegex(ota.UpdateError, reason):
                run(serial)
            self.assertFalse(any(packet.startswith(b"1;255;4;0;3;") for packet in serial.writes))

    def test_block_request_before_handshake_is_rejected(self):
        serial = FakeSerial([request(0)])
        with self.assertRaisesRegex(ota.UpdateError, "before identity"):
            run(serial)
        self.assertEqual(serial.writes, [ota.stream_message(0)])

    def test_device_error_discovery_or_transfer(self):
        error = b"1;255;3;0;9;ota_error:crc\n"
        for traffic in (error, IDENTITY + CURRENT + request(0) + error):
            with self.subTest(traffic=traffic), self.assertRaisesRegex(ota.UpdateError, "rejected update: crc"):
                run(FakeSerial([traffic]))

    def test_staged_requires_all_distinct_blocks(self):
        for traffic in (STAGED, IDENTITY + CURRENT + STAGED,
                        IDENTITY + CURRENT + request(0) + request(0) + STAGED):
            serial = FakeSerial([traffic])
            with self.subTest(traffic=traffic), self.assertRaisesRegex(ota.UpdateError, "before all image"):
                run(serial)
            self.assertNotIn(ota.REBOOT_RECEIPT, serial.writes)

    def test_reboot_receipt_after_valid_stage_only_and_write_failure_is_precise(self):
        serial = FakeSerial([IDENTITY + CURRENT + remaining_requests() + STAGED])
        self.assertEqual(run(serial), FIRMWARE.blocks)
        self.assertEqual(serial.writes[-1], b"1;255;3;0;13;\n")
        self.assertEqual(serial.writes.count(ota.REBOOT_RECEIPT), 1)
        for failure in ("disconnect", "short"):
            serial = FakeSerial([IDENTITY + CURRENT + remaining_requests() + STAGED])
            write = serial.write

            def fail_receipt(packet):
                if packet == ota.REBOOT_RECEIPT:
                    if failure == "disconnect":
                        raise OSError("USB removed after staging")
                    return len(packet) - 1
                return write(packet)

            serial.write = fail_receipt
            with self.subTest(failure=failure), self.assertRaisesRegex(
                    ota.UpdateError, "staging is confirmed; reboot receipt is unconfirmed"):
                run(serial)

    def test_other_endpoint_and_echoed_ack_cannot_complete_transfer(self):
        traffic = IDENTITY + CURRENT + remaining_requests()
        traffic += STAGED.replace(b"1;255", b"0;255") + STAGED.replace(b"3;0;9", b"3;1;9")
        with self.assertRaisesRegex(ota.UpdateError, "Transfer timeout"):
            run(FakeSerial([traffic], step=0.1), idle_timeout=0.5)

    def test_idle_timeout_when_requests_or_final_ack_are_missing(self):
        for traffic in (IDENTITY + CURRENT, IDENTITY + CURRENT + remaining_requests()):
            serial = FakeSerial([traffic], step=0.1)
            with self.subTest(traffic=traffic), self.assertRaisesRegex(ota.UpdateError, "staging is unconfirmed"):
                run(serial, idle_timeout=0.5)

    def test_unending_retries_still_hit_overall_deadline(self):
        serial = FakeSerial([IDENTITY + CURRENT] + [request(0)] * 30, step=0.1)
        with self.assertRaisesRegex(ota.UpdateError, "Overall update timeout"):
            run(serial, total_timeout=0.8, idle_timeout=0.5)

    def test_disconnect_and_short_writes_fail_without_reconnect(self):
        serial = FakeSerial([IDENTITY + CURRENT + remaining_requests(), OSError("USB removed")])
        with self.assertRaisesRegex(ota.UpdateError, "disconnected; staging is unconfirmed"):
            run(serial)
        serial = FakeSerial([IDENTITY])
        serial.short_write = True
        with self.assertRaisesRegex(ota.UpdateError, "Incomplete serial write"):
            run(serial)
        serial.write = Mock(side_effect=OSError("write timeout"))
        with self.assertRaisesRegex(ota.UpdateError, "Serial write failed"):
            run(serial)

    def test_oversized_serial_line_is_bounded(self):
        for traffic in (b"x" * 513, b"x" * 513 + b"\n"):
            with self.subTest(size=len(traffic)), self.assertRaisesRegex(ota.UpdateError, "Oversized"):
                run(FakeSerial([traffic]))
        reader = ota.LineReader()
        self.assertEqual(reader.feed(b"a" * 512 + b"\n"), [b"a" * 512])

    def test_invalid_options_do_not_touch_transport(self):
        serial = FakeSerial()
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite and positive"):
                run(serial, idle_timeout=value)
        with self.assertRaisesRegex(ValueError, "16 hexadecimal"):
            ota.stage_firmware(serial, FIRMWARE, "BAD", clock=serial.clock)
        self.assertEqual(serial.writes, [])


class CliTests(unittest.TestCase):
    def test_serial_open_is_exclusive_bounded_and_discards_stale_input(self):
        port = Mock()
        module = types.ModuleType("serial")
        module.Serial = Mock(return_value=port)
        with patch.dict(sys.modules, {"serial": module}), patch("fcntl.ioctl") as ioctl, \
                patch.object(ota, "reject_existing_owners"):
            self.assertIs(ota.open_serial("/dev/ttyACM0", 115200), port)
        module.Serial.assert_called_once_with("/dev/ttyACM0", baudrate=115200, timeout=0.1,
                                             write_timeout=3, exclusive=True)
        ioctl.assert_called_once()
        port.reset_input_buffer.assert_called_once()

    def test_exclusivity_failure_closes_the_port(self):
        port = Mock()
        module = types.ModuleType("serial")
        module.Serial = Mock(return_value=port)
        with patch.dict(sys.modules, {"serial": module}), \
                patch("fcntl.ioctl", side_effect=OSError("busy")), \
                self.assertRaisesRegex(ota.UpdateError, "Stop Home Assistant"):
            ota.open_serial("/dev/ttyACM0", 115200)
        port.close.assert_called_once()
        port.reset_input_buffer.assert_not_called()

    def test_cli_staging_only_success_message_and_port_cleanup(self):
        serial = FakeSerial([IDENTITY + CURRENT + remaining_requests() + STAGED])
        manager = Mock()
        manager.__enter__ = Mock(return_value=serial)
        manager.__exit__ = Mock(return_value=False)
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "test.bin"
            image.write_bytes(IMAGE)
            with patch.object(ota, "open_serial", return_value=manager), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ota.main(["/dev/ttyACM0", str(image), "--version", "5",
                                           "--device-id", DEVICE]), 0)
        self.assertIn("staged (ota_staged)", stdout.getvalue())
        self.assertIn("Reboot, installation and running firmware are unverified", stdout.getvalue())
        manager.__exit__.assert_called_once()

    def test_cli_layout_rejection_happens_before_serial_open(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "wrong.bin"
            cases = [(b"wrong image", "binary size")]
            for offset, reason in ((ota.APP_OFFSET - 16, "flash layout"),
                                   (ota.APP_OFFSET, "vectors"),
                                   (ota.APP_OFFSET + 8, "OTA-capable")):
                data = bytearray(IMAGE)
                data[offset:offset + 4] = b"\x00" * 4
                cases.append((data, reason))
            for data, reason in cases:
                image.write_bytes(data)
                stderr = io.StringIO()
                with self.subTest(reason=reason), patch.object(ota, "open_serial") as open_port, \
                        contextlib.redirect_stderr(stderr):
                    self.assertEqual(ota.main(["/dev/ttyACM0", str(image), "--version", "5",
                                               "--device-id", DEVICE]), 1)
                open_port.assert_not_called()
                self.assertIn(reason, stderr.getvalue())

    def test_cli_requires_version_and_device_id(self):
        for options in ([], ["--version", "5"], ["--device-id", DEVICE]):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                ota.main(["/dev/ttyACM0", "firmware.bin", *options])
            self.assertEqual(error.exception.code, 2)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux PTY serial checks")
class PtyTests(unittest.TestCase):
    def setUp(self):
        try:
            import serial
        except ImportError:
            self.skipTest("PTY checks require pyserial; use uv run")
        self.serial = serial
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        os.close(self.slave)
        self.addCleanup(os.close, self.master)

    def test_real_port_rejects_a_new_nonexclusive_owner(self):
        with ota.open_serial(self.path, 115200):
            with self.assertRaises(self.serial.SerialException):
                self.serial.Serial(self.path)

    def test_real_port_rejects_an_already_open_nonexclusive_owner(self):
        with self.serial.Serial(self.path):
            with self.assertRaisesRegex(ota.UpdateError, "already open"):
                ota.open_serial(self.path, 115200)

    def test_real_fragmented_transfer_and_many_retries_do_not_wait_per_block(self):
        import select
        errors = []

        def receive_line():
            result = bytearray()
            while not result.endswith(b"\n"):
                if not select.select([self.master], [], [], 1)[0]:
                    raise AssertionError("host response timed out")
                result.extend(os.read(self.master, 1))
            return bytes(result)

        def device():
            try:
                self.assertEqual(receive_line(), ota.stream_message(0))
                for chunk in fragment(IDENTITY + CURRENT):
                    os.write(self.master, chunk)
                self.assertEqual(receive_line(), FIRMWARE.config_response())
                for index in [0] * 20 + list(range(FIRMWARE.blocks)):
                    for chunk in fragment(request(index)):
                        os.write(self.master, chunk)
                    self.assertEqual(receive_line(), FIRMWARE.block_response(
                        struct.pack("<HHH", ota.FIRMWARE_TYPE, 5, index).hex())[1])
                for chunk in fragment(STAGED):
                    os.write(self.master, chunk)
                self.assertEqual(receive_line(), ota.REBOOT_RECEIPT)
            except Exception as exc:
                errors.append(exc)

        with ota.open_serial(self.path, 115200) as port:
            worker = threading.Thread(target=device, daemon=True)
            worker.start()
            try:
                self.assertEqual(ota.stage_firmware(port, FIRMWARE, DEVICE, total_timeout=5), FIRMWARE.blocks)
            finally:
                worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])


@contextlib.contextmanager
def gateway_link(output, *, mutate_host=lambda line: line, mutate_device=lambda line: line):
    """Byte bridge only: the executable owns Gateway, OTA parsing and storage.

    Each complete line is observed/mutated for fault injection, then forwarded
    in small fragments through real pyserial + PTY + child stdin/stdout pipes.
    """
    import select
    master, slave = os.openpty()
    path = os.ttyname(slave)
    os.close(slave)
    stop = threading.Event()
    link = types.SimpleNamespace(host=[], device=[], errors=[], report="")
    try:
        with ota.open_serial(path, 115200) as port, tempfile.TemporaryFile() as report:
            reference = output.with_name("reference.bin")
            reference.write_bytes(IMAGE)
            link.reference = reference
            class BridgeTransport:
                @property
                def in_waiting(self):
                    return port.in_waiting

                def read(self, size):
                    if link.errors:
                        raise OSError(link.errors[0])
                    return port.read(size)

                def write(self, packet):
                    if link.errors:
                        raise OSError(link.errors[0])
                    return port.write(packet)

            link.port = BridgeTransport()
            process = subprocess.Popen([str(SERVER), "--paired=0", f"--ota-file={output}",
                                        f"--ota-prefix={reference}"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=report,
                                       bufsize=0)
            link.process = process

            def bridge():
                buffers = {master: bytearray(), process.stdout.fileno(): bytearray()}
                try:
                    while not stop.is_set():
                        ready, _, _ = select.select(list(buffers), [], [], 0.05)
                        for source in ready:
                            chunk = os.read(source, 4096)
                            if not chunk:
                                if not stop.is_set():
                                    raise ConnectionError("C++ Gateway pipe closed unexpectedly")
                                return
                            buffer = buffers[source]
                            buffer.extend(chunk)
                            while b"\n" in buffer:
                                end = buffer.index(b"\n") + 1
                                line = bytes(buffer[:end])
                                del buffer[:end]
                                if source == master:
                                    link.host.append(line)
                                    destination = process.stdin.fileno()
                                    line = mutate_host(line)
                                else:
                                    link.device.append(line)
                                    destination = master
                                    line = mutate_device(line)
                                for part in fragment(line):
                                    view = memoryview(part)
                                    while view:
                                        count = os.write(destination, view)
                                        if not count:
                                            raise ConnectionError("PTY bridge write made no progress")
                                        view = view[count:]
                except Exception as exc:
                    if not stop.is_set():
                        link.errors.append(str(exc))

            worker = threading.Thread(target=bridge, daemon=True)
            worker.start()
            try:
                yield link
            finally:
                if not link.errors and process.poll() is None:
                    # Let the bridge deliver the final receipt before closing
                    # the serial port or child input (write() can precede read()).
                    for _ in range(100):
                        if ota.REBOOT_RECEIPT in link.host:
                            break
                        if stop.wait(0.001):
                            break
                stop.set()
                worker.join(timeout=2)
                process.stdin.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                process.stdout.close()
                report.seek(0)
                link.report = report.read().decode(errors="replace")
                if worker.is_alive():
                    link.errors.append("PTY bridge did not stop")
    finally:
        os.close(master)


@unittest.skipUnless(sys.platform.startswith("linux"), "C++ Gateway PTY integration requires Linux")
class GatewayInteropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SERVER.is_file():
            raise RuntimeError(f"Build the real mysensors_server target before running OTA tests: {SERVER}")

    def test_simulated_reset_discards_commands_beyond_the_rx_window(self):
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active.bin"
            staged = Path(directory) / "staged.bin"
            journal = Path(directory) / "journal.bin"
            active.write_bytes(FIRMWARE.data)
            command = [str(SERVER), "--ota-reboot", f"--ota-active={active}",
                       f"--ota-file={staged}", f"--journal={journal}", "--paired=1"]
            subprocess.run(command, input=b"", capture_output=True, check=True, timeout=2)
            before = journal.read_bytes()
            # One atomic pipe write: reboot is consumed from the 256-byte RX
            # window, while OPEN remains unread in stdin across the reset.
            packet = ota.REBOOT_RECEIPT.ljust(256, b"\n") + b"2;1;1;0;29;1\n"
            result = subprocess.run(command, input=packet, capture_output=True, check=True, timeout=2)
            self.assertIn(b"ota reboot", result.stderr)
            self.assertNotIn(b"burst start", result.stderr)
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(active.read_bytes(), FIRMWARE.data)
            self.assertFalse(staged.exists())

    def test_simulated_reset_waits_for_control_reconnect_and_preserves_active_journal(self):
        import select
        import socket
        import time
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active.bin"
            staged = Path(directory) / "staged.bin"
            journal = Path(directory) / "journal.bin"
            active.write_bytes(FIRMWARE.data)
            controller, device = socket.socketpair()
            controller.settimeout(2)
            process = subprocess.Popen(
                [str(SERVER), f"--control-fd={device.fileno()}", "--ota-reboot",
                 f"--ota-active={active}", f"--ota-file={staged}", f"--journal={journal}", "--paired=1"],
                pass_fds=(device.fileno(),), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
            device.close()
            buffer = bytearray()

            def config():
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    while b"\n" in buffer:
                        line, _, rest = buffer.partition(b"\n")
                        buffer[:] = rest
                        if line.startswith(b"1;255;4;0;0;"):
                            return ota.unpack_hex(line.split(b";", 5)[5].decode(), 5)
                    if select.select([process.stdout], [], [], 0.05)[0]:
                        chunk = os.read(process.stdout.fileno(), 4096)
                        self.assertTrue(chunk)
                        buffer.extend(chunk)
                self.fail("No fresh active config after reconnect")

            try:
                controller.sendall(b"C")
                self.assertEqual(controller.recv(1), b"C")
                expected = (ota.FIRMWARE_TYPE, 5, FIRMWARE.blocks, FIRMWARE.checksum, 1)
                self.assertEqual(config(), expected)
                before = journal.read_bytes()
                process.stdin.write(ota.REBOOT_RECEIPT)
                self.assertEqual(controller.recv(1), b"B")
                buffer.clear()  # old application evidence cannot cross a USB reset
                self.assertEqual(active.read_bytes(), FIRMWARE.data)
                self.assertEqual(journal.read_bytes(), before)
                self.assertFalse(staged.exists())
                controller.sendall(b"C")
                self.assertEqual(controller.recv(1), b"C")
                self.assertEqual(config(), expected)
                process.stdin.write(FIRMWARE.config_response())  # current config must not install
                process.stdin.close()
                self.assertEqual(process.wait(timeout=2), 0)
                self.assertFalse(staged.exists())
            finally:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                process.stdout.close()
                controller.close()

    def transfer(self, output, *, expected=DEVICE, mutate_host=lambda line: line,
                 mutate_device=lambda line: line, error=None):
        with gateway_link(output, mutate_host=mutate_host, mutate_device=mutate_device) as link:
            if error:
                with self.assertRaisesRegex(ota.UpdateError, error):
                    ota.stage_firmware(link.port, FIRMWARE, expected, total_timeout=20, idle_timeout=3)
            else:
                self.assertEqual(ota.stage_firmware(link.port, FIRMWARE, expected,
                                                   total_timeout=20, idle_timeout=3), FIRMWARE.blocks)
        self.assertEqual(link.errors, [], link.report)
        self.assertEqual(link.process.returncode, 0, link.report)
        return link

    def test_real_gateway_commits_all_blocks_and_matching_full_image_crc(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "staged.bin"
            stdout = io.StringIO()
            with gateway_link(output) as link:
                # Inject only the port factory: this is the already-open real
                # pyserial PTY, with the actual CLI image validation and sender.
                with patch.object(ota, "open_serial", return_value=contextlib.nullcontext(link.port)), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                    result = ota.main(["/dev/test-pty", str(link.reference), "--version", "5",
                                       "--device-id", DEVICE])
            self.assertEqual(result, 0, link.report)
            self.assertEqual(link.errors, [], link.report)
            self.assertEqual(link.process.returncode, 0, link.report)
            self.assertIn("running firmware are unverified", stdout.getvalue())
            self.assertEqual(output.read_bytes(), FIRMWARE.data)
            self.assertEqual(ota.crc16(output.read_bytes()), FIRMWARE.checksum)
        self.assertIn(FIRMWARE.config_response(), link.host)
        self.assertIn(STAGED, link.device)
        self.assertEqual(link.host[-1], ota.REBOOT_RECEIPT)
        indices = set()
        for line in link.host:
            if line.startswith(b"1;255;4;0;3;"):
                payload = line.decode().strip().split(";")[5]
                _, _, index = struct.unpack("<HHH", bytes.fromhex(payload[:12]))
                self.assertEqual(line, FIRMWARE.block_response(payload[:12])[1])
                indices.add(index)
        self.assertEqual(indices, set(range(FIRMWARE.blocks)))

    def test_real_gateway_wrong_identity_does_not_start_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "staged.bin"
            link = self.transfer(output, expected="FFFFFFFFFFFFFFFF", error="Wrong device identity")
            self.assertFalse(output.exists())
        self.assertEqual(link.host, [ota.stream_message(0)])

    def test_real_gateway_corrupt_and_out_of_range_requests_are_rejected(self):
        for replacement, error in ((b"1;255;4;0;2;32580500000Z\n", "Malformed"),
                                   (request(FIRMWARE.blocks), "out of range")):
            def corrupt_request(line):
                return replacement if line.startswith(b"1;255;4;0;2;") else line

            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "staged.bin"
                link = self.transfer(output, mutate_device=corrupt_request, error=error)
                self.assertFalse(output.exists())
                self.assertNotIn(STAGED, link.device)

    def test_real_gateway_rejects_malformed_host_response(self):
        for corruption, error in (("hex", "message"), ("length", "block")):
            def corrupt_block(line):
                if not line.startswith(b"1;255;4;0;3;"):
                    return line
                return line[:-2] + b"Z\n" if corruption == "hex" else line[:-3] + b"\n"

            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "staged.bin"
                link = self.transfer(output, mutate_host=corrupt_block,
                                     error=f"Device rejected update: {error}")
                self.assertFalse(output.exists())
                self.assertNotIn(STAGED, link.device)
                self.assertNotIn(ota.REBOOT_RECEIPT, link.host)

    def test_real_gateway_rejects_data_corruption_at_final_crc(self):
        last_header = struct.pack("<HHH", ota.FIRMWARE_TYPE, 5, FIRMWARE.blocks - 1).hex().upper().encode()

        def corrupt_last_block(line):
            if line.startswith(b"1;255;4;0;3;" + last_header):
                changed = int(line[-3:-1], 16) ^ 1
                return line[:-3] + f"{changed:02X}\n".encode()
            return line

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "staged.bin"
            link = self.transfer(output, mutate_host=corrupt_last_block, error="Device rejected update: crc")
            self.assertFalse(output.exists())
        self.assertNotIn(STAGED, link.device)


if __name__ == "__main__":
    unittest.main()
