"""Linux serialx integration with the shared C++ gateway, not a protocol mock.

Build gateway_server with CMake before running; X2D_GATEWAY_SERVER can override
build/x2d-core/gateway_server. Simulated radio/flash do not qualify real RF.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import sys
import tty
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python/src"))
from x2d_gateway import CommandUncertain, Gateway, GatewayError, ProtocolError


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def fragments(data):
    """Include single-byte writes and split JSON tokens and line endings."""
    offset = 0
    while offset < len(data):
        size = 1 + offset % 3
        yield data[offset:offset + size]
        offset += size


class NativeBridge:
    """Only forward/observe bytes; the executable owns all protocol behavior."""

    def __init__(self, mode):
        self.mode = mode
        self.incoming = asyncio.Queue()
        self.sent = []
        self.received = []
        self.sent_buffer = bytearray()
        self.received_buffer = bytearray()
        self.master = self.slave = None
        self.writer = None
        self.tcp_task = None
        self.server = None
        self.tasks = []

    async def start(self, binary):
        self.control, child_control = socket.socketpair()
        self.control.setblocking(False)
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(binary), f"--control-fd={child_control.fileno()}",
                pass_fds=(child_control.fileno(),), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        finally:
            child_control.close()
        self.tasks = [asyncio.create_task(self._input()),
                      asyncio.create_task(self._output())]
        if self.mode == "tcp":
            self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
            self.device = f"socket://localhost:{self.server.sockets[0].getsockname()[1]}"
        else:
            self.attach_pty()
        return self

    def attach_pty(self):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.device = os.ttyname(self.slave)
        asyncio.get_running_loop().add_reader(self.master, self._read_pty)

    def _read_pty(self):
        try:
            data = os.read(self.master, 4096)
        except BlockingIOError:
            return
        if data:
            self.incoming.put_nowait(data)

    def _accept(self, reader, writer):
        self.writer = writer
        self.tcp_task = asyncio.create_task(self._read_tcp(reader))

    async def _read_tcp(self, reader):
        while data := await reader.read(4096):
            self.incoming.put_nowait(data)

    @staticmethod
    def _record(buffer, lines, data):
        buffer.extend(data)
        while b"\n" in buffer:
            end = buffer.index(b"\n") + 1
            lines.append(bytes(buffer[:end]))
            del buffer[:end]

    async def _input(self):
        while True:
            data = await self.incoming.get()
            for chunk in fragments(data):
                self.process.stdin.write(chunk)
                await self.process.stdin.drain()
                self._record(self.sent_buffer, self.sent, chunk)
                await asyncio.sleep(0)

    async def _output(self):
        while data := await self.process.stdout.read(4096):
            master, writer = self.master, self.writer
            for chunk in fragments(data):
                if master is not None and self.master == master:
                    os.write(master, chunk)
                elif writer is not None and self.writer is writer and not writer.is_closing():
                    writer.write(chunk)
                    await writer.drain()
                await asyncio.sleep(0)
            self._record(self.received_buffer, self.received, data)

    def messages(self):
        return [json.loads(line) for line in self.received]

    async def disconnect(self):
        """Fence old input before the native reset, retaining process and flash."""
        if self.master is not None:
            asyncio.get_running_loop().remove_reader(self.master)
            os.close(self.master)
            os.close(self.slave)
            self.master = self.slave = None
        if self.writer is not None:
            self.writer.transport.abort()
            await self.writer.wait_closed()
            self.writer = None
        if self.tcp_task is not None:
            self.tcp_task.cancel()
            await asyncio.gather(self.tcp_task, return_exceptions=True)
            self.tcp_task = None
        self.tasks[0].cancel()
        await asyncio.gather(self.tasks[0], return_exceptions=True)
        self.incoming = asyncio.Queue()
        self.sent_buffer.clear()
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(3):
            await loop.sock_sendall(self.control, b"D")
            ack = await loop.sock_recv(self.control, 1)
        if ack != b"D":
            raise AssertionError(f"gateway_server reset acknowledgement: {ack!r}, expected b'D'")
        self.tasks[0] = asyncio.create_task(self._input())

    async def close(self):
        if self.server is not None:
            self.server.close()
        if self.master is not None:
            asyncio.get_running_loop().remove_reader(self.master)
            os.close(self.master)
            os.close(self.slave)
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
        if self.server is not None:
            await self.server.wait_closed()
        if self.tcp_task is not None:
            self.tcp_task.cancel()
            self.tasks.append(self.tcp_task)
        for task in self.tasks:
            task.cancel()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)
        self.process.stdin.close()
        try:
            async with asyncio.timeout(2):
                await self.process.wait()
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        self.control.close()
        errors = [result for result in results
                  if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)]
        stderr = (await self.process.stderr.read()).decode(errors="replace")
        if errors or self.process.returncode:
            raise AssertionError(f"C++ gateway/bridge failed: {errors}; exit={self.process.returncode}; {stderr}")


class SharedGatewayChecks(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ.get("X2D_GATEWAY_SERVER", ROOT / "build/x2d-core/gateway_server"))
        if not cls.binary.is_file() or not os.access(cls.binary, os.X_OK):
            raise AssertionError(
                f"Shared C++ gateway executable missing: {cls.binary}. "
                "Run devenv test (builds CMake first), or build the gateway_server "
                "CMake target and set X2D_GATEWAY_SERVER to its executable."
            )

    async def asyncSetUp(self):
        self.loop_errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: self.loop_errors.append(context))

    async def asyncTearDown(self):
        self.assertEqual(self.loop_errors, [], self.loop_errors)

    def command(self, gateway, slot, action):
        task = asyncio.create_task(gateway.command(slot, action))
        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(cleanup)
        return task

    @asynccontextmanager
    async def peer(self, mode):
        bridge = NativeBridge(mode)
        try:
            await bridge.start(self.binary)
            yield bridge
        finally:
            await bridge.close()

    @asynccontextmanager
    async def client(self, peer, **kwargs):
        gateway = await Gateway.open(peer.device, **kwargs)
        try:
            yield gateway
        finally:
            await gateway.close()

    async def command_ack(self, peer, slot, action):
        def acknowledged():
            requests = [json.loads(line) for line in peer.sent]
            ids = {r["id"] for r in requests if r["op"] == "command"
                   and r["args"] == {"shutter_id": slot, "action": action}}
            return any(m.get("id") in ids and m.get("ok") is True
                       for m in peer.messages())
        await until(acknowledged)

    async def test_hello_status_and_two_paired_slots(self):
        for mode in ("pty", "tcp"):
            with self.subTest(transport=mode):
                async with self.peer(mode) as peer, self.client(peer) as gateway:
                    self.assertEqual(gateway.info["product"], "ha-x2d")
                    self.assertEqual(gateway.info["max_line_bytes"], 4096)
                    self.assertEqual(gateway.info["max_shutters"], 16)
                    self.assertTrue({"status", "shutters", "command"}.issubset(gateway.info["capabilities"]))
                    status, inventory = await asyncio.gather(gateway.status(), gateway.shutters())
                    self.assertTrue(status["tx_enabled"])
                    self.assertTrue(status["radio"]["detected"])
                    self.assertEqual(status["storage"]["state"], "ready")
                    self.assertEqual(inventory["generation"], status["storage"]["generation"])
                    self.assertEqual(inventory["shutters"], [
                        {"shutter_id": slot, "state": "paired", "last_command": None}
                        for slot in (1, 2)
                    ])
                    later = await gateway.status()
                    self.assertGreaterEqual(later["uptime_ms"], status["uptime_ms"])

    async def test_concurrent_movements_and_stop_cancel_without_deadlock(self):
        for mode in ("pty", "tcp"):
            with self.subTest(transport=mode):
                async with self.peer(mode) as peer, self.client(peer) as gateway:
                    events = []
                    gateway.add_event_callback(events.append)
                    opening = self.command(gateway, 1, "open")
                    await self.command_ack(peer, 1, "open")
                    self.assertFalse(opening.done(), "Native burst must leave time for a concurrent STOP")
                    closing = self.command(gateway, 2, "close")
                    stopping = self.command(gateway, 1, "stop")
                    results = await asyncio.wait_for(asyncio.gather(
                        opening, closing, stopping, return_exceptions=True), 3)
                    self.assertIsInstance(results[0], GatewayError)
                    self.assertNotIsInstance(results[0], CommandUncertain)
                    self.assertIn(results[0].code, ("stop_preempted", "queue_cancelled", "tx_cancelled"))
                    self.assertEqual(results[1]["result"], "emitted")
                    self.assertEqual(results[2]["result"], "emitted")
                    self.assertEqual(len(events), 3)
                    requests = [json.loads(line) for line in peer.sent]
                    commands = [r for r in requests if r["op"] == "command"]
                    self.assertEqual({e["request_id"] for e in events}, {r["id"] for r in commands})
                    self.assertEqual([e["result"] for e in events].count("cancelled"), 1)
                    self.assertTrue(all(e["session"] == gateway.info["session"] for e in events))
                    emitted_slots = [e["shutter_id"] for e in events if e["result"] == "emitted"]
                    self.assertEqual(emitted_slots, [1, 2], "STOP must run before queued slot-2 movement")
                    self.assertFalse(gateway.closed)
                    inventory = await gateway.shutters()
                    self.assertEqual([r["last_command"] for r in inventory["shutters"]], ["stop", "close"])

    async def test_disconnect_uncertainty_reconnect_without_replay_and_identity_guard(self):
        for mode in ("pty", "tcp"):
            with self.subTest(transport=mode):
                async with self.peer(mode) as peer, self.client(peer) as gateway:
                    identity = gateway.info["device_id"]
                    session = gateway.info["session"]
                    generation = (await gateway.shutters())["generation"]
                    opening = self.command(gateway, 1, "open")
                    await self.command_ack(peer, 1, "open")
                    self.assertFalse(opening.done())
                    await peer.disconnect()
                    with self.assertRaises(CommandUncertain):
                        await asyncio.wait_for(opening, 1)
                    self.assertTrue(gateway.closed)
                    await gateway.close()
                    start = len(peer.sent)
                    if mode == "pty":
                        peer.attach_pty()
                    async with self.client(peer, expected_device_id=identity) as reconnected:
                        self.assertEqual(reconnected.info["device_id"], identity)
                        self.assertEqual(reconnected.info["session"], session)
                        self.assertEqual((await reconnected.status())["storage"]["generation"], generation)
                        inventory = await reconnected.shutters()
                        self.assertEqual(inventory["generation"], generation)
                        self.assertEqual([r["state"] for r in inventory["shutters"]], ["paired", "paired"])
                        # Wait past the original burst. Old completion events must
                        # not poison a session that reused request IDs 1/2/3.
                        await asyncio.sleep(0.35)
                        self.assertEqual((await reconnected.shutters())["generation"], generation)
                        self.assertFalse(reconnected.closed)
                    self.assertEqual([json.loads(line)["op"] for line in peer.sent[start:]],
                                     ["hello", "status", "shutters", "shutters"])
                    self.assertEqual(sum(json.loads(line)["op"] == "command" for line in peer.sent), 1)
                    await peer.disconnect()
                    if mode == "pty":
                        peer.attach_pty()
                    other_identity = ("0" if identity[0] != "0" else "1") + identity[1:]
                    with self.assertRaisesRegex(ProtocolError, "Different gateway"):
                        await Gateway.open(peer.device, expected_device_id=other_identity)
                    self.assertEqual(json.loads(peer.sent[-1])["op"], "hello")



if __name__ == "__main__":
    unittest.main()
