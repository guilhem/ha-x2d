"""Linux PTY checks: real serialx transport, simulated diagnostic firmware."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tty
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python/src"))
import x2d_gateway
from x2d_gateway import Gateway, ProtocolError

IDENTITY = "0123456789ABCDEF"
INFO = {
    "product": "ha-x2d", "firmware": "0.1.0", "device_id": IDENTITY,
    "session": "ABCDEF0123456789", "max_line_bytes": 512,
    "capabilities": ["info", "status", "cc1101_probe"],
}
STATUS = {
    "uptime_ms": 100, "tx_enabled": False,
    "radio": {"detected": True, "partnum": 0, "version": 20, "marcstate": 1},
}


class SimulatedGateway:
    """Own the other end of a real PTY; never opens hardware or sends RF."""

    def __init__(self):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.device = os.ttyname(self.slave)
        self.info = deepcopy(INFO)
        self.status = deepcopy(STATUS)
        self.requests = []
        self.buffer = b""
        self.response_filter = lambda request, response: response
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.master, self._read)

    def _read(self):
        try:
            self.buffer += os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            return
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            request = json.loads(line)
            self.requests.append(request)
            result = self.info if request["op"] == "hello" else self.status
            response = {"v": 1, "id": request["id"], "ok": True, "result": result}
            response = self.response_filter(request, response)
            if response is None:
                continue
            data = response if isinstance(response, bytes) else json.dumps(response).encode() + b"\n"
            # Exercise split serial reads as well as complete lines.
            os.write(self.master, data[:9])
            self.loop.call_soon(self._write, data[9:])

    def _write(self, data):
        if self.master is not None:
            os.write(self.master, data)

    def unplug(self):
        if self.master is not None:
            self.loop.remove_reader(self.master)
            os.close(self.master)
            self.master = None

    def close(self):
        self.unplug()
        os.close(self.slave)


class GatewayChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = SimulatedGateway()

    async def asyncTearDown(self):
        self.peer.close()

    async def test_serial_lifecycle_and_single_owner(self):
        gateway = await Gateway.open(self.peer.device)
        try:
            self.assertEqual(gateway.info["device_id"], IDENTITY)
            first, second = await asyncio.gather(gateway.status(), gateway.status())
            self.assertEqual(first, STATUS)
            self.assertEqual(second, STATUS)
            self.assertEqual([r["id"] for r in self.peer.requests], [1, 2, 3])
            with self.assertRaises(OSError):
                await Gateway.open(self.peer.device)
            self.peer.status["radio"] = {
                "detected": False, "partnum": None, "version": None, "marcstate": None,
            }
            self.assertFalse((await gateway.status())["radio"]["detected"])
        finally:
            await gateway.close()
        again = await Gateway.open(self.peer.device, expected_device_id=IDENTITY)
        await again.close()

    async def test_wrong_identity_releases_port(self):
        with self.assertRaisesRegex(ValueError, "Unsupported serial port"):
            await Gateway.open("unsupported-scheme://device")
        with self.assertRaisesRegex(ProtocolError, "Different gateway"):
            await Gateway.open(self.peer.device, expected_device_id="0000000000000000")
        self.peer.info["product"] = "unrelated-board"
        with self.assertRaisesRegex(ProtocolError, "Not an ha-x2d"):
            await Gateway.open(self.peer.device)
        self.peer.info["product"] = "ha-x2d"
        gateway = await Gateway.open(self.peer.device)
        await gateway.close()

    async def test_bad_responses_close_session(self):
        invalid = [
            b"not json\n", b"[]\n", b"\xff\n", b"x" * 513 + b"\n",
            {"v": 2, "id": 2, "ok": True, "result": STATUS},
            {"v": True, "id": 2, "ok": True, "result": STATUS},
            {"v": 1, "id": 3, "ok": True, "result": STATUS},
            {"v": 1, "id": 2, "ok": False, "error": "invalid_request"},
            {"v": 1, "id": 2, "ok": True, "result": {**STATUS, "tx_enabled": True}},
            {"v": 1, "id": 2, "ok": True, "result": {**STATUS, "uptime_ms": True}},
        ]
        for bad in invalid:
            with self.subTest(response=bad):
                self.peer.response_filter = lambda _, response: response
                gateway = await Gateway.open(self.peer.device)
                self.peer.response_filter = lambda _, response: bad
                with self.assertRaises(ProtocolError):
                    await gateway.status()
                with self.assertRaises(ConnectionError):
                    await gateway.status()

    async def test_timeout_and_cancellation_never_retry(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                self.peer.response_filter = lambda _, response: response
                gateway = await Gateway.open(self.peer.device)
                self.peer.requests.clear()
                self.peer.response_filter = lambda _, response: None
                with patch.object(x2d_gateway, "TIMEOUT", 0.1):
                    request = asyncio.create_task(gateway.status())
                    if cancel:
                        while not self.peer.requests:
                            await asyncio.sleep(0.001)
                        request.cancel()
                    with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                        await request
                self.assertEqual(len(self.peer.requests), 1)
                with self.assertRaises(ConnectionError):
                    await gateway.status()

    async def test_unplug(self):
        gateway = await Gateway.open(self.peer.device)
        self.peer.unplug()
        with self.assertRaises((OSError, ConnectionError)):
            await gateway.status()
        await gateway.close()


if __name__ == "__main__":
    unittest.main()
