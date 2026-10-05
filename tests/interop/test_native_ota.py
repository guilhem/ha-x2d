"""Real pymysensors serial sessions over PTY; bootloader, flash and RF simulated.

Run with the unpublished pymysensors checkout on PYTHONPATH. This suite is
separate from the pinned Home Assistant lifecycle checks in test_mysensors.py:

    PYTHONPATH=/path/to/pymysensors python -m unittest discover \
        -s tests/interop -p test_native_ota.py -v
"""

import asyncio
import contextlib
import os
from pathlib import Path
import struct
import tempfile
import tty
import unittest

from mysensors.gateway_serial import AsyncSerialGateway
from mysensors.ota import FirmwareImage, FirmwareUpdateError, FirmwareUpdateTimeout


ROOT = Path(__file__).resolve().parents[2]
SERVER = Path(os.environ.get("X2D_MYSENSORS_SERVER", ROOT / "build/native/mysensors_server"))
NODE, KIND, VERSION = 31, 42, 9


def words(*values):
    """Independent wire encoding, rather than the controller's serializer."""
    return struct.pack(f"<{len(values)}H", *values).hex().upper()


def wire(node, kind, subtype, payload="", child=255):
    return f"{node};{child};{kind};0;{subtype};{payload}\n"


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


class SerialPeer:
    """A byte-only PTY bridge; the actual library owns controller parsing/I/O."""

    def __init__(self):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.path = os.ttyname(self.slave)
        self.lines = asyncio.Queue()
        self.received = []
        self.buffer = bytearray()
        self.closed = False
        asyncio.get_running_loop().add_reader(self.master, self._read)

    def _read(self):
        try:
            data = os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            return
        self.buffer.extend(data)
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            decoded = line.decode().rstrip("\r")
            self.received.append(decoded)
            self.lines.put_nowait(decoded)

    async def send(self, line):
        raw = line.encode()
        for offset in range(0, len(raw), 3):
            chunk = raw[offset:offset + 3]
            while True:
                try:
                    written = os.write(self.master, chunk)
                    chunk = chunk[written:]
                    if not chunk:
                        break
                except BlockingIOError:
                    await asyncio.sleep(0.001)
            await asyncio.sleep(0)

    async def expect(self, kind, subtype, node=NODE, timeout=3):
        async with asyncio.timeout(timeout):
            while True:
                line = await self.lines.get()
                fields = line.split(";", 5)
                if fields[:5] == [str(node), "255", str(kind), "0", str(subtype)]:
                    return fields[5]

    def messages(self, kind, subtype, node=NODE):
        prefix = f"{node};255;{kind};0;{subtype};"
        return [line[len(prefix):] for line in self.received if line.startswith(prefix)]

    def close(self):
        if self.closed:
            return
        self.closed = True
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)
        os.close(self.slave)


@contextlib.asynccontextmanager
async def controller():
    peer = SerialPeer()
    gateway = AsyncSerialGateway(peer.path, protocol_version="2.3")
    try:
        await gateway.start()
        await until(lambda: gateway.tasks.transport.protocol.transport is not None)
        yield gateway, peer
    finally:
        await gateway.stop()
        await asyncio.sleep(0)
        peer.close()


@unittest.skipUnless(os.name == "posix", "PTY interoperability needs POSIX")
class BootloaderInteropTests(unittest.IsolatedAsyncioTestCase):
    """AVR-like peer requests descending blocks before application presentation."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "firmware.bin"
        path.write_bytes(bytes(range(129)))
        self.image = FirmwareImage.from_file(path, KIND, VERSION)
        self.config = words(KIND, VERSION, self.image.blocks, self.image.crc)
        self.progress = []

    async def arm(self, gateway, peer, **kwargs):
        task = asyncio.create_task(gateway.install_firmware(
            NODE, self.image, progress_callback=self.progress.append,
            timeout=kwargs.pop("timeout", 1),
            confirmation_timeout=kwargs.pop("confirmation_timeout", 1), **kwargs))
        self.addAsyncCleanup(self.cancel, task)
        self.assertEqual(await peer.expect(3, 13), "")
        await peer.send(wire(NODE, 4, 0, words(KIND, VERSION - 1, 8, 123, 0x0301)))
        self.assertEqual((await peer.expect(4, 1)).upper(), self.config)
        return task

    async def cancel(self, task):
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, FirmwareUpdateError):
            await task

    async def transfer(self, peer):
        order = list(reversed(range(self.image.blocks)))
        order.insert(1, order[0])
        for index in order:
            before = list(self.progress)
            await peer.send(wire(NODE, 4, 2, words(KIND, VERSION, index)))
            response = await peer.expect(4, 3)
            expected = words(KIND, VERSION, index) + self.image.data[index * 16:(index + 1) * 16].hex()
            self.assertEqual(response.lower(), expected.lower())
            if order.index(index) == 0 and before:
                self.assertGreaterEqual(self.progress[-1], before[-1])
        self.assertLess(self.progress[-1], 100)

    async def confirm_config(self, peer):
        await peer.send(wire(NODE, 4, 0, self.config + words(0x0301)))
        self.assertEqual((await peer.expect(4, 1)).upper(), self.config)

    async def test_avr_descending_transfer_needs_config_then_application(self):
        async with controller() as (gateway, peer):
            task = await self.arm(gateway, peer)
            await self.transfer(peer)
            self.assertFalse(task.done())
            await self.confirm_config(peer)
            self.assertFalse(task.done())
            await peer.send(wire(NODE + 1, 0, 17, "2.3.2"))
            await peer.send(wire(NODE + 1, 3, 22, "100"))
            self.assertFalse(task.done())
            await peer.send(wire(NODE, 0, 17, "2.3.2"))
            await peer.send(wire(NODE, 3, 22, "1"))
            result = await asyncio.wait_for(task, 1)
            self.assertEqual((result.firmware_type, result.firmware_version,
                              result.blocks, result.crc),
                             (KIND, VERSION, self.image.blocks, self.image.crc))
            self.assertEqual(self.progress[-1], 100)
            self.assertEqual(self.progress, sorted(self.progress))
            self.assertEqual(peer.messages(3, 13), [""])

    async def test_cancel_mid_transfer_never_replays_on_boot(self):
        async with controller() as (gateway, peer):
            task = await self.arm(gateway, peer)
            await peer.send(wire(NODE, 4, 2, words(KIND, VERSION, 0)))
            await peer.expect(4, 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            before = len(peer.messages(4, 3))
            offers = len(peer.messages(4, 1))
            await peer.send(wire(NODE, 4, 0, words(KIND, VERSION - 1, 8, 123, 0x0301)))
            await peer.send(wire(NODE, 4, 2, words(KIND, VERSION, 0)))
            await peer.send(wire(NODE, 0, 17, "2.3.2"))
            await asyncio.sleep(0.05)
            self.assertEqual(len(peer.messages(4, 3)), before)
            self.assertNotIn(self.config, [value.upper() for value in peer.messages(4, 1)[offers:]])
            self.assertEqual(peer.messages(3, 13), [""])

    async def test_validation_without_application_times_out(self):
        async with controller() as (gateway, peer):
            task = await self.arm(gateway, peer, confirmation_timeout=0.2)
            await self.transfer(peer)
            await self.confirm_config(peer)
            with self.assertRaises(FirmwareUpdateTimeout):
                await task
            self.assertNotIn(100, self.progress)
            offers = len(peer.messages(4, 1))
            await peer.send(wire(NODE, 4, 0, self.config + words(0x0301)))
            await peer.send(wire(NODE, 0, 17, "2.3.2"))
            await asyncio.sleep(0.05)
            self.assertEqual(peer.messages(3, 13), [""])
            self.assertNotIn(100, self.progress)
            self.assertLessEqual(len(peer.messages(4, 1)) - offers, 1)

    async def test_reboot_into_wrong_image_is_not_success(self):
        async with controller() as (gateway, peer):
            task = await self.arm(gateway, peer)
            await self.transfer(peer)
            await peer.send(wire(NODE, 4, 0, words(KIND, VERSION, self.image.blocks,
                                                  self.image.crc ^ 1, 0x0301)))
            await peer.send(wire(NODE, 0, 17, "2.3.2"))
            with self.assertRaises(FirmwareUpdateError):
                await asyncio.wait_for(task, 2)
            self.assertNotIn(100, self.progress)
            self.assertEqual(peer.messages(3, 13), [""])

    async def test_new_controller_does_not_resume_interrupted_session(self):
        async with controller() as (gateway, peer):
            task = await self.arm(gateway, peer)
            await self.transfer(peer)
            await self.confirm_config(peer)
            await gateway.stop()
            with self.assertRaises((FirmwareUpdateError, asyncio.CancelledError)):
                await asyncio.wait_for(task, 2)
            replacement = AsyncSerialGateway(peer.path, protocol_version="2.3")
            try:
                await replacement.start()
                await until(lambda: replacement.tasks.transport.protocol.transport is not None)
                before = len(peer.messages(4, 3))
                await peer.send(wire(NODE, 4, 0, self.config + words(0x0301)))
                await peer.send(wire(NODE, 4, 2, words(KIND, VERSION, 0)))
                await peer.send(wire(NODE, 0, 17, "2.3.2"))
                await asyncio.sleep(0.05)
                self.assertEqual(len(peer.messages(4, 3)), before)
                self.assertEqual(peer.messages(3, 13), [""])
                self.assertNotIn(100, self.progress)
            finally:
                await replacement.stop()

    async def test_usb_disconnect_during_both_expected_reboots(self):
        peers = [SerialPeer()]
        path = Path(self.directory.name) / "dongle"
        path.symlink_to(peers[-1].path)
        connected = asyncio.Event()
        gateway = AsyncSerialGateway(str(path), protocol_version="2.3")
        gateway.tasks.transport.reconnect_timeout = 0.01
        gateway.on_conn_made = lambda _: connected.set()

        async def reenumerate():
            connected.clear()
            peers[-1].close()
            peers.append(SerialPeer())
            path.unlink()
            path.symlink_to(peers[-1].path)
            await asyncio.wait_for(connected.wait(), 2)
            return peers[-1]

        try:
            await gateway.start()
            await asyncio.wait_for(connected.wait(), 1)
            task = asyncio.create_task(gateway.install_firmware(
                NODE, self.image, timeout=2, confirmation_timeout=2,
                progress_callback=self.progress.append))
            self.addAsyncCleanup(self.cancel, task)
            self.assertEqual(await peers[-1].expect(3, 13), "")
            peer = await reenumerate()
            await peer.send(wire(NODE, 4, 0, words(KIND, VERSION - 1, 8, 123, 0x0301)))
            self.assertEqual((await peer.expect(4, 1)).upper(), self.config)
            await self.transfer(peer)
            peer = await reenumerate()
            await self.confirm_config(peer)
            self.assertFalse(task.done())
            await peer.send(wire(NODE, 0, 17, "2.3.2"))
            await asyncio.wait_for(task, 1)
            self.assertEqual(self.progress[-1], 100)
            self.assertEqual(sum(len(peer.messages(3, 13)) for peer in peers), 1)
        finally:
            await gateway.stop()
            await asyncio.sleep(0.01)
            for peer in peers:
                peer.close()


def rp2040_image(version):
    """Synthetic app accepted by the real RP2040 verifier, with stable boot prefix."""
    data = bytearray((37 * index + 11) % 256 for index in range(0x3181))
    struct.pack_into("<4I", data, 0x2FF0, 0x101FF000, 0x103FF000, 0x103FF000, 0x1EF000)
    struct.pack_into("<2I", data, 0x3000, 0x20042000, 0x10003009)
    identity = f"HA-X2D YD-RP2040 OTA/1:{version}:interop".encode() + b"\0"
    data[0x3040:0x3040 + len(identity)] = identity
    return bytes(data) + b"\xff" * (-len(data) % 128)


@contextlib.asynccontextmanager
async def cpp_controller(directory, *, filter_host=lambda line: True,
                         filter_device=lambda line: True, start_gateway=True):
    """Fragmented serial bytes go through the actual C++ gateway and verifier."""
    peer = SerialPeer()
    directory = Path(directory)
    active = directory / "active.bin"
    active.write_bytes(rp2040_image(7))
    journal = directory / "journal.bin"
    staged = directory / "staged.bin"
    process = await asyncio.create_subprocess_exec(
        str(SERVER), f"--ota-file={staged}", f"--ota-active={active}",
        "--ota-reboot", f"--journal={journal}", "--paired=2",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    report = []
    peer.from_device = []

    async def host_bytes():
        while True:
            line = await peer.lines.get()
            if filter_host(line):
                process.stdin.write((line + "\n").encode())
                await process.stdin.drain()

    async def device_bytes():
        async for line in process.stdout:
            peer.from_device.append(line.decode().rstrip())
            if filter_device(line.decode()):
                await peer.send(line.decode())

    async def reports():
        async for line in process.stderr:
            report.append(line.decode().rstrip())

    gateway = AsyncSerialGateway(peer.path, protocol_version="2.3")
    pumps = [asyncio.create_task(host_bytes()), asyncio.create_task(device_bytes()),
             asyncio.create_task(reports())]
    try:
        if start_gateway:
            await gateway.start()
            await until(lambda: 1 in gateway.firmware_configs)
        yield gateway, peer, active, journal, staged, report
    finally:
        await gateway.stop()
        pumps[0].cancel()
        await asyncio.gather(pumps[0], return_exceptions=True)
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()
        await asyncio.gather(*pumps[1:])
        peer.close()
    if process.returncode != 0:
        raise AssertionError(f"C++ gateway failed ({process.returncode}): {report}")


@unittest.skipUnless(os.name == "posix", "PTY interoperability needs POSIX")
class FirmwareInteropTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertTrue(SERVER.is_file(), "Build the CMake mysensors_server target first")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "candidate.bin"
        path.write_bytes(rp2040_image(8))
        self.image = FirmwareImage.from_file(path, 0x5832, 8)

    async def test_lost_startup_configs_are_solicited_without_reboot_replay(self):
        boots = dropped = 0
        drop_config = False

        def lose_startup_config(line):
            nonlocal boots, dropped, drop_config
            if line.startswith("0;255;3;0;14;"):
                boots += 1
                drop_config = boots > 1
            if drop_config and line.startswith("1;255;4;0;0;"):
                drop_config = False
                dropped += 1
                return False
            return True

        async with cpp_controller(self.directory.name, filter_device=lose_startup_config) as rig:
            gateway, peer, active, journal, _, report = rig
            before = journal.read_bytes()
            result = await gateway.install_firmware(
                1, self.image, timeout=5, confirmation_timeout=5)
            self.assertEqual(result.image_words, self.image.image_words)
            self.assertEqual(dropped, 2)
            self.assertEqual(peer.messages(3, 19, node=1), ["", ""])
            self.assertEqual(peer.messages(3, 13, node=1), [""])
            self.assertEqual(active.read_bytes(), self.image.data)
            self.assertEqual(journal.read_bytes(), before)
            self.assertFalse(any("burst start" in line for line in report))

    async def test_real_cpp_install_confirms_active_image_and_preserves_journal(self):
        async with cpp_controller(self.directory.name) as rig:
            gateway, peer, active, journal, staged, report = rig
            before = journal.read_bytes()
            self.assertFalse(staged.exists())
            progress = []
            result = await gateway.install_firmware(
                1, self.image, progress_callback=progress.append,
                timeout=5, confirmation_timeout=5)
            self.assertEqual((result.firmware_version, result.blocks, result.crc),
                             (8, self.image.blocks, self.image.crc))
            self.assertEqual(active.read_bytes(), self.image.data)
            self.assertEqual(journal.read_bytes(), before)
            self.assertEqual(progress[-1], 100)
            self.assertEqual(peer.messages(3, 13, node=1), [""])
            self.assertEqual(set(gateway.firmware_configs), {1})
            self.assertFalse(any("burst start" in line for line in report), report)
            staged_count = sum("ota staged" in line for line in report)
            gateway.send(wire(1, 3, 19))
            await asyncio.sleep(0.05)
            self.assertEqual(sum("ota staged" in line for line in report), staged_count)

    async def test_cancel_before_verification_keeps_running_image(self):
        responses = 0

        def stop_after_ten(line):
            nonlocal responses
            if line.startswith("1;255;4;0;3;"):
                responses += 1
                return responses <= 10
            return True

        async with cpp_controller(self.directory.name, filter_host=stop_after_ten) as rig:
            gateway, peer, active, journal, staged, report = rig
            before, image_before = journal.read_bytes(), active.read_bytes()
            task = asyncio.create_task(gateway.install_firmware(1, self.image, timeout=5))
            try:
                await until(lambda: responses >= 11)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await until(lambda: not staged.exists(), timeout=4)
                self.assertEqual(active.read_bytes(), image_before)
                self.assertEqual(journal.read_bytes(), before)
                self.assertFalse(any("ota staged" in line or "burst start" in line for line in report))
                self.assertEqual(peer.messages(3, 13, node=1), [""])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_after_commit_does_not_replay_on_application_return(self):
        async with cpp_controller(self.directory.name) as rig:
            gateway, peer, active, journal, _, report = rig
            before = journal.read_bytes()
            progress = []
            task = asyncio.create_task(gateway.install_firmware(
                1, self.image, progress_callback=progress.append,
                timeout=5, confirmation_timeout=5))
            try:
                await until(lambda: any("ota staged" in line for line in report), timeout=5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await until(lambda: gateway.firmware_configs[1].firmware_version == 8, timeout=5)
                self.assertEqual(active.read_bytes(), self.image.data)
                self.assertEqual(journal.read_bytes(), before)
                self.assertNotIn(100, progress)
                self.assertEqual(peer.messages(3, 13, node=1), [""])
                self.assertEqual(sum("ota staged" in line for line in report), 1)
                self.assertFalse(any("burst start" in line for line in report))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
