#!/usr/bin/env python3
"""Stage a whole Arduino-Pico .bin over the X2D native MySensors USB port.

Usage: uv run tools/update_firmware.py /dev/ttyACM0 firmware.bin \
    --version 7 --device-id 0123456789ABCDEF

Stop Home Assistant's MySensors integration and other serial clients first.
The device ID prevents accidental targeting; it is not authentication.
Success means ota_staged was received, not that reboot or installation succeeded.
"""

import argparse
from dataclasses import dataclass
import math
from pathlib import Path
import re
import stat
import struct
import sys
import time

NODE, CHILD = 1, 255
C_INTERNAL, C_STREAM = 3, 4
I_LOG_MESSAGE = 9
REBOOT_RECEIPT = b"1;255;3;0;13;\n"
ST_CONFIG_REQUEST, ST_CONFIG_RESPONSE, ST_REQUEST, ST_RESPONSE = 0, 1, 2, 3
FIRMWARE_TYPE = 0x5832
BLOCK_SIZE = 16
APP_OFFSET = 0x3000
MAX_IMAGE_SIZE = 0xFFFF * BLOCK_SIZE
MAX_LINE_SIZE = 512


class UpdateError(Exception):
    """Transfer failed; staging and installation must not be assumed."""


def crc16(data):
    """MySensors CRC16 MODBUS: reflected 0xA001, initial 0xFFFF, no final XOR.

    Upstream mysensors/ota.py uses the MODBUS model (crcmod in locked 0.26.0)
    and struct.pack('<...H') for the wire fields, including the checksum.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def device_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f]{16}", value):
        raise ValueError("device ID must contain exactly 16 hexadecimal digits")
    return value.upper()


def unpack_hex(payload, words):
    if not re.fullmatch(r"[0-9A-Fa-f]{%d}" % (words * 4), payload):
        raise UpdateError("Malformed firmware request: invalid hex payload length or digits")
    return struct.unpack(f"<{words}H", bytes.fromhex(payload))


def stream_message(subtype, payload=""):
    return f"{NODE};{CHILD};{C_STREAM};0;{subtype};{payload}\n".encode("ascii")


@dataclass(frozen=True)
class Firmware:
    data: bytes
    version: int
    checksum: int

    @classmethod
    def from_bytes(cls, data, version):
        """Validate the complete image locally and CRC its padded full contents."""
        if type(version) is not int or not 0 <= version <= 0xFFFF:
            raise ValueError("version must be an integer in 0..65535")
        if not APP_OFFSET < len(data) <= MAX_IMAGE_SIZE:
            raise ValueError(f"binary size must be > {APP_OFFSET} and <= {MAX_IMAGE_SIZE} bytes")
        # The shared image validator owns partition map, vector and board checks.
        if __package__:
            from .firmware_layout import image_version, validate_binary
        else:
            from firmware_layout import image_version, validate_binary
        data = validate_binary(data)
        if image_version(data) != version:
            raise ValueError("version does not match the compiled image identity")
        return cls(data, version, crc16(data))

    @property
    def blocks(self):
        return len(self.data) // BLOCK_SIZE

    def config_response(self):
        payload = struct.pack("<HHHH", FIRMWARE_TYPE, self.version,
                              self.blocks, self.checksum).hex().upper()
        return stream_message(ST_CONFIG_RESPONSE, payload)

    def block_response(self, payload):
        kind, version, index = unpack_hex(payload, 3)
        if (kind, version) != (FIRMWARE_TYPE, self.version):
            raise UpdateError("Firmware request type/version does not match this image")
        if index >= self.blocks:
            raise UpdateError(f"Firmware block index {index} is out of range")
        offset = index * BLOCK_SIZE
        response = struct.pack("<HHH", kind, version, index) + self.data[offset:offset + BLOCK_SIZE]
        return index, stream_message(ST_RESPONSE, response.hex().upper())


class LineReader:
    """Bounded framing which retains partial USB reads across polling timeouts."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        lines = []
        while b"\n" in self.buffer:
            end = self.buffer.index(b"\n")
            if end > MAX_LINE_SIZE:
                raise UpdateError("Oversized serial line")
            lines.append(bytes(self.buffer[:end]).rstrip(b"\r"))
            del self.buffer[:end + 1]
        if len(self.buffer) > MAX_LINE_SIZE:
            raise UpdateError("Oversized serial line")
        return lines


def parse_message(line):
    """Ignore unrelated traffic, but reject corrupt packets for our OTA endpoint."""
    fields = line.split(b";", 5)
    if fields[:2] != [b"1", b"255"]:
        return None
    if len(fields) != 6:
        raise UpdateError("Malformed MySensors packet for the OTA endpoint")
    try:
        if any(not re.fullmatch(rb"[0-9]+", field) for field in fields[:5]):
            raise ValueError("non-numeric header")
        node, child, command, ack, subtype = map(int, fields[:5])
        if command > 4 or ack not in (0, 1) or subtype > 255:
            raise ValueError("header out of range")
        payload = fields[5].decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise UpdateError(f"Malformed MySensors OTA packet: {exc}") from exc
    if ack:
        return None  # an echo acknowledgement is not a device request or staging result
    return command, subtype, payload


def stage_firmware(transport, firmware, expected_device_id, *, discovery_timeout=10,
                   idle_timeout=10, total_timeout=300, retry_interval=1,
                   clock=time.monotonic):
    """Serve an injected read/write transport until staging is explicitly confirmed.

    read() must poll with a bounded timeout; empty reads mean no data yet.
    Disconnects must raise OSError. No reconnect/replay is attempted.
    """
    expected_device_id = device_id(expected_device_id)
    for value in (discovery_timeout, idle_timeout, total_timeout, retry_interval):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeouts and retry interval must be finite and positive")
    started = clock()
    last_activity = started
    next_probe = started
    identified = configured = config_received = False
    served = set()
    reader = LineReader()

    def send(packet, *, staged=False):
        status = ("staging is confirmed; reboot receipt is unconfirmed" if staged
                  else "staging is unconfirmed")
        try:
            if transport.write(packet) != len(packet):
                raise UpdateError(f"Incomplete serial write; {status}")
        except OSError as exc:
            raise UpdateError(f"Serial write failed; {status}: {exc}") from exc

    while True:
        now = clock()
        if now - started >= total_timeout:
            raise UpdateError("Overall update timeout; staging is unconfirmed")
        if not configured and now - started >= discovery_timeout:
            raise UpdateError("Discovery timeout waiting for device identity/current config")
        if configured and now - last_activity >= idle_timeout:
            raise UpdateError("Transfer timeout waiting for requests/ota_staged; staging is unconfirmed")
        if not configured and now >= next_probe:
            send(stream_message(ST_CONFIG_REQUEST))
            next_probe = now + retry_interval
        try:
            # Waiting for a whole 256-byte read would cost the serial timeout
            # on every 27-byte block request. Block only for the first byte.
            size = min(256, max(1, getattr(transport, "in_waiting", 256)))
            chunk = transport.read(size)
        except OSError as exc:
            raise UpdateError(f"Serial disconnected; staging is unconfirmed: {exc}") from exc
        for line in reader.feed(chunk):
            message = parse_message(line)
            if message is None:
                continue
            command, subtype, payload = message
            if command == C_INTERNAL and subtype == I_LOG_MESSAGE:
                if payload.startswith("ota_id:"):
                    try:
                        actual = device_id(payload.removeprefix("ota_id:"))
                    except ValueError as exc:
                        raise UpdateError(f"Invalid OTA device identity: {exc}") from exc
                    if actual != expected_device_id:
                        raise UpdateError(f"Wrong device identity: expected {expected_device_id}, got {actual}")
                    identified = True
                elif payload.startswith("ota_error:"):
                    raise UpdateError(f"Device rejected update: {payload.removeprefix('ota_error:')}")
                elif payload == "ota_staged":
                    if not configured or len(served) != firmware.blocks:
                        raise UpdateError("Unexpected ota_staged before all image blocks were sent")
                    send(REBOOT_RECEIPT, staged=True)
                    return len(served)
            elif command == C_STREAM and subtype == ST_CONFIG_REQUEST:
                # Four words exist only on the old running gateway: retain
                # this migration entrypoint for its one-time standard upgrade.
                words = 5 if len(payload) == 20 else 4
                kind, *_ = unpack_hex(payload, words)
                if kind != FIRMWARE_TYPE:
                    raise UpdateError("Current firmware type is not X2D (0x5832)")
                config_received = True
                if configured:
                    send(firmware.config_response())
                    last_activity = clock()
            elif command == C_STREAM and subtype == ST_REQUEST:
                if not configured:
                    raise UpdateError("Firmware block requested before identity/config handshake")
                index, packet = firmware.block_response(payload)
                send(packet)
                served.add(index)
                last_activity = clock()
            if identified and config_received and not configured:
                send(firmware.config_response())
                configured = True
                last_activity = clock()


def reject_existing_owners(port):
    """Reject already-open Linux owners visible to this user's /proc access.

    TIOCEXCL prevents subsequent opens, but cannot evict a pre-existing owner.
    Owners hidden by OS permissions still require the operator to stop HA.
    """
    if not sys.platform.startswith("linux"):
        return
    import os
    descriptor = port.fileno()
    target = os.fstat(descriptor)
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            for handle in (process / "fd").iterdir():
                if process.name == str(os.getpid()) and handle.name == str(descriptor):
                    continue
                try:
                    other = handle.stat()
                except OSError:
                    continue  # descriptors can disappear while being inspected
                if stat.S_ISCHR(other.st_mode) and other.st_rdev == target.st_rdev:
                    raise ValueError(f"serial port already open by PID {process.name}; stop that owner first")
        except OSError:
            continue  # other users' descriptor tables may be inaccessible


def open_serial(path, baudrate):
    """Open a local port exclusively; never fall back to shared or URL transports."""
    try:
        import serial
    except ImportError as exc:
        raise UpdateError("pyserial is missing; run this tool with uv run") from exc
    port = None
    try:
        port = serial.Serial(path, baudrate=baudrate, timeout=0.1,
                             write_timeout=3, exclusive=True)
        # pyserial's POSIX exclusive flag uses flock. TIOCEXCL also rejects new
        # opens by clients which do not participate in that advisory lock.
        if sys.platform != "win32":
            import fcntl
            import termios
            fcntl.ioctl(port.fileno(), termios.TIOCEXCL)
        reject_existing_owners(port)
        port.reset_input_buffer()  # discard stale identities and staging acknowledgements
        return port
    except (OSError, ValueError) as exc:
        if port is not None:
            port.close()
        raise UpdateError(f"Cannot open serial port exclusively: {exc}. Stop Home Assistant/other serial owners.") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("serial_path", help="local USB serial path (for example /dev/ttyACM0)")
    parser.add_argument("binary", type=Path, help="whole Arduino-Pico .bin, including bootloader/partition prefix")
    parser.add_argument("--version", type=int, required=True, help="compiled firmware wire version (0.6.0-rc2 uses 7)")
    parser.add_argument("--device-id", required=True, help="expected board ID: exactly 16 hex digits")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--discovery-timeout", type=float, default=10)
    parser.add_argument("--idle-timeout", type=float, default=10)
    parser.add_argument("--total-timeout", type=float, default=300)
    args = parser.parse_args(argv)
    try:
        expected = device_id(args.device_id)
        if args.binary.suffix.lower() != ".bin":
            raise ValueError("firmware must be a whole Arduino-Pico .bin file")
        for value in (args.discovery_timeout, args.idle_timeout, args.total_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("timeouts must be finite and positive")
        if args.baudrate <= 0:
            raise ValueError("baudrate must be positive")
        with args.binary.open("rb") as source:
            data = source.read(MAX_IMAGE_SIZE + 1)
        firmware = Firmware.from_bytes(data, args.version)
        print(f"Validated {firmware.blocks} blocks, CRC {firmware.checksum:04X}. "
              "Stop Home Assistant/other serial owners before updating.", file=sys.stderr)
        with open_serial(args.serial_path, args.baudrate) as transport:
            stage_firmware(transport, firmware, expected,
                           discovery_timeout=args.discovery_timeout,
                           idle_timeout=args.idle_timeout, total_timeout=args.total_timeout)
        print("Firmware staged (ota_staged). Reboot, installation and running firmware are unverified.")
        return 0
    except (UpdateError, ValueError, OSError, ImportError) as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Update interrupted; staging is unconfirmed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
