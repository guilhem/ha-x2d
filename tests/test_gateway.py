"""Real serialx transport on Linux PTYs, never physical USB/RF."""

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
from x2d_gateway import CommandUncertain, Gateway, GatewayError, ProtocolError

IDENTITY = "0123456789ABCDEF"
GENERATION = "1111111122222222"
INFO = {
    "product": "ha-x2d", "firmware": "0.3.0", "device_id": IDENTITY,
    "session": "ABCDEF0123456789", "max_line_bytes": 4096, "max_shutters": 16,
    "capabilities": ["status", "shutters", "provision", "pair", "confirm", "command"],
}
STATUS = {
    "uptime_ms": 100, "tx_enabled": True,
    "radio": {"detected": True, "partnum": 0, "version": 20, "marcstate": 1},
    "storage": {"state": "ready", "generation": GENERATION},
}


class SimulatedGateway:
    """Contract simulator; journal contents can be copied across PTY reconnects."""

    def __init__(self):
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.device = os.ttyname(self.slave)
        self.info = deepcopy(INFO)
        self.status = deepcopy(STATUS)
        self.shutters = {}
        self.requests = []
        self.buffer = b""
        self.seq = 0
        self.auto_tx = True
        self.response_filter = lambda request, response: response
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.master, self._read)

    def _read(self):
        try:
            self.buffer += os.read(self.master, 8192)
        except (BlockingIOError, OSError):
            return
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            request = json.loads(line)
            self.requests.append(request)
            assert request["v"] == 2
            assert set(request) == ({"v", "id", "op"} if request["op"] == "hello" else {"v", "id", "op", "session", "args"})
            if request["op"] != "hello":
                assert request["session"] == self.info["session"]
            result, error = self._operate(request)
            response = {"v": 2, "id": request["id"], "ok": error is None}
            response.update({"result": result} if error is None else {"error": error})
            response = self.response_filter(request, response)
            if response is None:
                continue
            self.send(response)
            if self.auto_tx and error is None and request["op"] in ("pair", "command"):
                self.loop.call_soon(self.tx_result, request)

    def _operate(self, request):
        op = request["op"]
        if op == "hello":
            return self.info, None
        if op == "status":
            return self.status, None
        generation = self.status["storage"]["generation"]
        if op == "shutters":
            return {"generation": generation, "shutters": list(self.shutters.values())}, None
        slot = request["args"]["shutter_id"]
        if self.status["storage"]["state"] in ("corrupt", "full"):
            return None, "storage_" + self.status["storage"]["state"]
        if op == "provision":
            if self.status["storage"]["state"] == "empty":
                generation = GENERATION
                self.status["storage"] = {"state": "ready", "generation": generation}
            record = self.shutters.setdefault(slot, {"shutter_id": slot, "state": "pending", "last_command": None})
            return {**record, "generation": generation}, None
        if slot not in self.shutters:
            return None, "unknown_shutter"
        record = self.shutters[slot]
        if op == "confirm":
            record["state"] = "paired"
            return {**record, "generation": generation}, None
        if not self.status["tx_enabled"]:
            return None, "profile_unverified"
        if op == "command":
            if record["state"] != "paired":
                return None, "not_paired"
            record["last_command"] = request["args"]["action"]
        return {"accepted": True, "shutter_id": slot}, None

    def send(self, message):
        data = message if isinstance(message, bytes) else json.dumps(message).encode() + b"\n"
        if self.master is not None:
            os.write(self.master, data)

    def event(self, **fields):
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        return {"v": 2, "session": self.info["session"], "seq": self.seq, **fields}

    def tx_result(self, request, result="emitted", **fields):
        self.send(self.event(event="tx_result", request_id=request["id"],
                             shutter_id=request["args"]["shutter_id"], result=result, **fields))

    def unplug(self):
        if self.master is not None:
            self.loop.remove_reader(self.master)
            os.close(self.master)
            self.master = None

    def close(self):
        self.unplug()
        os.close(self.slave)


async def wait_requests(peer, count):
    async with asyncio.timeout(1):
        while len(peer.requests) < count:
            await asyncio.sleep(0.001)


class GatewayChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = SimulatedGateway()
        self.gateway = None
        self.loop_errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: self.loop_errors.append(context))

    async def asyncTearDown(self):
        if self.gateway is not None:
            await self.gateway.close()
        self.peer.close()
        await asyncio.sleep(0)
        self.assertEqual(self.loop_errors, [], self.loop_errors)

    async def open(self):
        self.gateway = await Gateway.open(self.peer.device)
        return self.gateway

    async def test_v2_single_owner_and_idempotent_slots(self):
        gateway = await self.open()
        self.assertEqual(gateway.info, INFO)
        self.assertEqual(await asyncio.gather(gateway.status(), gateway.status()), [STATUS, STATUS])
        with self.assertRaises(OSError):
            await Gateway.open(self.peer.device)
        record = await gateway.provision(1)
        self.assertEqual(await gateway.provision(1), record)
        await gateway.provision(2)
        self.assertEqual(len((await gateway.shutters())["shutters"]), 2)
        await gateway.pair(1)
        self.assertEqual(self.peer.shutters[1]["state"], "pending")
        await gateway.confirm(1)
        self.assertEqual(self.peer.shutters[1]["state"], "paired")
        self.assertEqual(self.peer.shutters[2]["state"], "pending")
        self.assertEqual([r["id"] for r in self.peer.requests], list(range(1, len(self.peer.requests) + 1)))
        for invalid in (True, False, 0, 17, 1.0, "1"):
            with self.assertRaises(ValueError):
                await gateway.provision(invalid)
        with self.assertRaises(ValueError):
            await gateway.command(1, "toggle")

    async def test_reboot_pending_identity_and_no_replay(self):
        gateway = await self.open()
        record = await gateway.provision(1)
        await gateway.close()
        self.peer.info["session"] = "BBBBBBBBBBBBBBBB"
        gateway = await self.open()
        self.assertEqual(await gateway.provision(1), record)
        self.assertEqual(self.peer.shutters[1]["state"], "pending")
        self.assertFalse(any(r["op"] in ("pair", "command") for r in self.peer.requests))

    async def test_application_rejection_is_not_protocol_error(self):
        gateway = await self.open()
        await gateway.provision(1)
        self.peer.status["tx_enabled"] = False
        with self.assertRaises(GatewayError) as raised:
            await gateway.pair(1)
        self.assertEqual(raised.exception.code, "profile_unverified")
        self.assertFalse(gateway.closed)
        self.assertFalse((await gateway.status())["tx_enabled"])
        self.assertEqual(self.peer.shutters[1]["state"], "pending")

    async def test_tx_result_runtime_errors_and_completed_copies(self):
        gateway = await self.open()
        await gateway.provision(1)
        await gateway.provision(2)
        await gateway.confirm(2)
        self.peer.auto_tx = False
        events = []
        gateway.add_event_callback(events.append)
        codes = ("tx_disabled", "unqualified_profile", "storage_io_error", "maintenance_pending",
                 "stop_in_progress", "body_refused", "radio_fault", "radio_start_failed", "stop_preempted")
        for operation in ("pair", "command"):
            for result, copies in (("rejected", 0), ("unknown", 1), ("cancelled", 2), ("emitted", 32)):
                for code in codes:
                    with self.subTest(operation=operation, result=result, code=code):
                        start = len(self.peer.requests)
                        task = asyncio.create_task(gateway.pair(1) if operation == "pair" else gateway.command(2, "open"))
                        await wait_requests(self.peer, start + 1)
                        self.assertFalse(task.done())  # ACK alone cannot complete TX.
                        self.peer.tx_result(self.peer.requests[-1], result, error=code, completed_copies=copies)
                        with self.assertRaises(CommandUncertain if result == "unknown" else GatewayError) as raised:
                            await asyncio.wait_for(task, 1)
                        self.assertEqual(raised.exception.code, code)
                        self.assertFalse(gateway.closed)
                        self.assertEqual(len(self.peer.requests), start + 1)
                        self.assertEqual(events[-1]["completed_copies"], copies)
                        self.assertEqual(self.peer.shutters[1]["state"], "pending")
        for fields in ({}, {"completed_copies": 0}, {"completed_copies": 32},
                       {"completed_copies": 48}, {"completed_copies": 64}):
            start = len(self.peer.requests)
            task = asyncio.create_task(gateway.command(2, "stop"))
            await wait_requests(self.peer, start + 1)
            self.peer.tx_result(self.peer.requests[-1], **fields)
            result = await asyncio.wait_for(task, 1)
            self.assertEqual(result["result"], "emitted")
            self.assertEqual({key: result[key] for key in fields}, fields)

    async def test_stop_and_out_of_order_events_while_open_waits(self):
        gateway = await self.open()
        await gateway.provision(1)
        await gateway.confirm(1)
        await gateway.provision(2)
        await gateway.confirm(2)
        events = []
        unsubscribe = gateway.add_event_callback(events.append)
        self.peer.auto_tx = False
        opening = asyncio.create_task(gateway.command(1, "open"))
        await wait_requests(self.peer, 6)
        open_request = self.peer.requests[-1]
        other = asyncio.create_task(gateway.command(2, "close"))
        stopping = asyncio.create_task(gateway.command(1, "stop"))
        await wait_requests(self.peer, 8)
        close_request, stop_request = self.peer.requests[-2:]
        self.peer.send(self.peer.event(event="rx", identity="AABBCC", action="stop", counter=65535))
        self.peer.tx_result(stop_request)
        self.assertEqual((await stopping)["result"], "emitted")
        self.assertFalse(opening.done())
        self.peer.tx_result(close_request)
        self.assertEqual((await other)["shutter_id"], 2)
        self.peer.tx_result(open_request, "cancelled")
        with self.assertRaisesRegex(GatewayError, "tx_cancelled"):
            await opening
        self.assertEqual(len(events), 4)
        unsubscribe()
        self.assertEqual((await gateway.shutters())["shutters"][0]["last_command"], "stop")

    async def test_bad_peer_data_closes_session(self):
        invalid = [b"not json\n", b"[]\n", b"\xff\n", b"x" * 4096 + b"\n",
                   b'{"v":2,"v":2,"id":2,"ok":true,"result":{}}\n',
                   {"v": True, "id": 2, "ok": True, "result": STATUS},
                   {"v": 2, "id": True, "ok": True, "result": STATUS},
                   {"v": 2, "id": 500, "ok": True, "result": STATUS},
                   {"v": 2, "id": 2, "ok": True, "result": {**STATUS, "uptime_ms": True}},
                   {"v": 2, "id": 2, "ok": True, "result": {**STATUS, "tx_enabled": 1}},
                   {"v": 2, "session": True, "seq": 1, "event": "rx", "identity": "AABBCC", "counter": 1, "action": "open"},
                   {"v": 2, "session": INFO["session"], "seq": True, "event": "rx", "identity": "AABBCC", "counter": 1, "action": "open"},
                   {"v": 2, "session": INFO["session"], "seq": 1, "event": "rx", "identity": "AABBCC", "counter": True, "action": "open"},
                   {"v": 2, "session": INFO["session"], "seq": 1, "event": "rx", "identity": "AABBCC", "counter": 65536, "action": "open"}]
        for bad in invalid:
            with self.subTest(bad=bad):
                self.peer.response_filter = lambda _, response: response
                gateway = await self.open()
                self.peer.response_filter = lambda _, response: bad
                with self.assertRaises(ProtocolError):
                    await gateway.status()
                self.assertTrue(gateway.closed)
                with self.assertRaises(ConnectionError):
                    await gateway.status()

    async def test_tx_uncertainty_timeout_unplug_and_cancel_close(self):
        for failure in ("unknown", "timeout", "unplug", "cancel", "protocol"):
            with self.subTest(failure=failure):
                gateway = await self.open()
                await gateway.provision(1)
                await gateway.confirm(1)
                self.peer.auto_tx = False
                start = len(self.peer.requests)
                with patch.object(x2d_gateway, "TX_TIMEOUT", 0.05):
                    task = asyncio.create_task(gateway.command(1, "open"))
                    await wait_requests(self.peer, start + 1)
                    if failure == "unknown":
                        self.peer.tx_result(self.peer.requests[-1], "unknown")
                    elif failure == "cancel":
                        task.cancel()
                    elif failure == "unplug":
                        self.peer.unplug()
                    elif failure == "protocol":
                        self.peer.send(b"not json\n")
                    with self.assertRaises(asyncio.CancelledError if failure == "cancel" else CommandUncertain):
                        await task
                self.assertEqual(len(self.peer.requests), start + 1)
                if failure != "unknown":
                    self.assertTrue(gateway.closed)
                await gateway.close()
                if failure == "unplug":
                    self.peer.close()
                    self.peer = SimulatedGateway()

    async def test_ack_timeout_and_cancel_no_retry(self):
        for cancel in (False, True):
            self.peer.response_filter = lambda _, response: response
            gateway = await self.open()
            start = len(self.peer.requests)
            self.peer.response_filter = lambda _, response: None
            with patch.object(x2d_gateway, "TIMEOUT", 0.05):
                task = asyncio.create_task(gateway.status())
                if cancel:
                    await wait_requests(self.peer, start + 1)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                    await task
            self.assertEqual(len(self.peer.requests), start + 1)
            self.assertTrue(gateway.closed)

    async def test_framing_limit_and_truncated_line(self):
        gateway = await self.open()
        def padded(request, response):
            data = json.dumps(response).encode()
            return data + b" " * (4095 - len(data)) + b"\n"
        self.peer.response_filter = padded
        self.assertEqual(await gateway.status(), STATUS)
        self.peer.response_filter = lambda _, response: b'{"v":2'
        task = asyncio.create_task(gateway.status())
        await wait_requests(self.peer, 3)
        await asyncio.sleep(0.005)
        self.peer.unplug()
        with self.assertRaises((ProtocolError, OSError)):
            await task
        self.assertTrue(gateway.closed)

    async def test_invalid_tx_events_close_without_confirmation(self):
        bad_fields = [{"request_id": True}, {"request_id": 1000}, {"shutter_id": True},
                      {"shutter_id": 2}, {"seq": True}, {"session": "BBBBBBBBBBBBBBBB"},
                      {"result": "success"}, {"counter": 1},
                      *({"error": value} for value in ("not_a_runtime_error", None, True, 1, [], {})),
                      *({"completed_copies": value} for value in (-1, 65, True, False, 1.0, "1", None))]
        for fields in bad_fields:
            with self.subTest(fields=fields):
                gateway = await self.open()
                await gateway.provision(1)
                self.peer.auto_tx = False
                start = len(self.peer.requests)
                task = asyncio.create_task(gateway.pair(1))
                await wait_requests(self.peer, start + 1)
                request = self.peer.requests[-1]
                event = self.peer.event(event="tx_result", request_id=request["id"], shutter_id=1, result="emitted")
                self.peer.send({**event, **fields})
                with self.assertRaises(CommandUncertain) as raised:
                    await task
                self.assertIsInstance(raised.exception.__cause__, ProtocolError)
                self.assertEqual(len(self.peer.requests), start + 1)
                self.assertTrue(gateway.closed)
                self.assertEqual(self.peer.shutters[1]["state"], "pending")
                await gateway.close()
        gateway = await self.open()
        start = len(self.peer.requests)
        # An event before its ACK is invalid, even with correct IDs and session.
        self.peer.response_filter = lambda request, response: self.peer.event(event="tx_result", request_id=request["id"], shutter_id=1, result="emitted")
        with self.assertRaises(CommandUncertain) as raised:
            await gateway.pair(1)
        self.assertIsInstance(raised.exception.__cause__, ProtocolError)
        self.assertEqual(len(self.peer.requests), start + 1)

    async def test_command_ack_loss_and_cancellation_close_siblings(self):
        gateway = await self.open()
        await gateway.provision(1)
        await gateway.confirm(1)
        self.peer.auto_tx = False
        self.peer.response_filter = lambda _, response: None
        start = len(self.peer.requests)
        with patch.object(x2d_gateway, "TIMEOUT", 0.05):
            with self.assertRaises(CommandUncertain):
                await gateway.command(1, "open")
        self.assertEqual(len(self.peer.requests), start + 1)
        self.peer.response_filter = lambda _, response: response
        gateway = await self.open()
        start = len(self.peer.requests)
        opening = asyncio.create_task(gateway.command(1, "open"))
        stopping = asyncio.create_task(gateway.command(1, "stop"))
        await wait_requests(self.peer, start + 2)
        opening.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await opening
        with self.assertRaises(CommandUncertain):
            await stopping
        self.assertEqual(len(self.peer.requests), start + 2)
        self.assertTrue(gateway.closed)

    async def test_empty_and_corrupt_storage_are_not_formatted_implicitly(self):
        self.peer.status["storage"] = {"state": "empty", "generation": None}
        gateway = await self.open()
        self.assertIsNone((await gateway.status())["storage"]["generation"])
        self.assertEqual(await gateway.shutters(), {"generation": None, "shutters": []})
        self.assertEqual(self.peer.shutters, {})
        await gateway.provision(16)
        self.assertEqual(self.peer.status["storage"]["generation"], GENERATION)
        self.peer.status["storage"] = {"state": "corrupt", "generation": None}
        with self.assertRaisesRegex(GatewayError, "storage_corrupt"):
            await gateway.provision(2)
        self.assertIsNone((await gateway.status())["storage"]["generation"])
        self.assertEqual(set(self.peer.shutters), {16})

    async def test_diagnostic_only_capabilities_do_not_require_provision(self):
        self.peer.info["capabilities"] = ["status", "shutters"]
        self.peer.status["tx_enabled"] = False
        gateway = await self.open()
        self.assertFalse((await gateway.status())["tx_enabled"])
        self.assertEqual((await gateway.shutters())["shutters"], [])
        self.assertEqual(self.peer.shutters, {})
        self.assertEqual([r["op"] for r in self.peer.requests], ["hello", "status", "shutters"])

    async def test_request_ids_never_wrap(self):
        gateway = await self.open()
        gateway._sequence = 0x7FFFFFFE
        await gateway.status()
        with self.assertRaises(ConnectionError):
            await gateway.status()
        self.assertTrue(gateway.closed)
        self.assertEqual(self.peer.requests[-1]["id"], 0x7FFFFFFF)

    async def test_identity_and_hello_limits(self):
        with self.assertRaisesRegex(ValueError, "Unsupported serial port"):
            await Gateway.open("unsupported-scheme://device")
        with self.assertRaisesRegex(ProtocolError, "Different gateway"):
            await Gateway.open(self.peer.device, expected_device_id="0000000000000000")
        for field, value in (("max_shutters", True), ("max_line_bytes", 512), ("session", 1)):
            original = self.peer.info[field]
            self.peer.info[field] = value
            with self.assertRaises(ProtocolError):
                await Gateway.open(self.peer.device)
            self.peer.info[field] = original

    async def test_event_wrap_crlf_fragmentation_and_response_interleave(self):
        gateway = await self.open()
        events = []
        gateway.add_event_callback(events.append)
        self.peer.seq = 0xFFFFFFFE
        self.peer.send(self.peer.event(event="rx", identity="ABCDEF", action="open", counter=0))
        self.peer.send(self.peer.event(event="rx", identity="ABCDEF", action="stop", counter=1))
        self.peer.response_filter = lambda _, response: None
        first = asyncio.create_task(gateway.status())
        second = asyncio.create_task(gateway.status())
        await wait_requests(self.peer, 3)
        for request in reversed(self.peer.requests[-2:]):
            data = json.dumps({"v": 2, "id": request["id"], "ok": True, "result": STATUS}).encode() + b"\r\n"
            self.peer.send(data[:9])
            await asyncio.sleep(0.001)
            self.peer.send(data[9:])
        self.assertEqual(await asyncio.gather(first, second), [STATUS, STATUS])
        self.assertEqual([e["seq"] for e in events], [0xFFFFFFFF, 0])
        self.peer.send(self.peer.event(event="rx", identity="ABCDEF", action="stop", counter=65535, shutter_id=True))
        async with asyncio.timeout(1):
            while not gateway.closed:
                await asyncio.sleep(0.001)


if __name__ == "__main__":
    unittest.main()
