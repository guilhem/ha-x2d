"""Independent USB v2 client; one reader, no RF encoding or request replay."""

import asyncio
from collections.abc import Callable
import json
import re

from serialx import SerialException, create_serial_connection

MAX_LINE_BYTES = 4096
MAX_SHUTTERS = 16
PROTOCOL_VERSION = 2
TIMEOUT = 3.0
TX_TIMEOUT = 8.0  # two enrollment trains plus the 2 s hold interval
ACTIONS = {"open", "close", "stop"}
ERRORS = {
    "session_disconnected", "deadline_expired", "incomplete_burst",
    "enrollment_overlap", "queue_expired", "queue_cancelled",
    "invalid_request", "unsupported_operation", "line_too_long", "stale_session",
    "duplicate_conflict", "stale_request", "storage_corrupt", "storage_full",
    "unknown_shutter", "not_paired", "counter_exhausted", "queue_full", "profile_unverified",
    "tx_disabled", "unqualified_profile", "storage_io_error", "maintenance_pending",
    "stop_in_progress", "body_refused", "radio_fault", "radio_start_failed", "stop_preempted",
}


class ProtocolError(ValueError):
    """Peer framing or data cannot be trusted; the session is closed."""


class GatewayError(Exception):
    """A valid application rejection, distinct from corrupt protocol data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CommandUncertain(GatewayError):
    """RF completion is unknown. Never replay this operation automatically."""

    def __init__(self, code: str = "command_uncertain"):
        super().__init__(code)


def _require(condition: bool, description: str) -> None:
    if not condition:
        raise ProtocolError(description)


def _integer(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _hex(value: object, size: int = 16) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9A-F]{{{size}}}", value) is not None


def _fields(value: object, names: set[str]) -> None:
    _require(isinstance(value, dict) and value.keys() == names, "Invalid object fields")


def _record(record: dict) -> None:
    _require(_integer(record.get("shutter_id"), 1, MAX_SHUTTERS), "Invalid shutter slot")
    _require(record.get("state") in ("pending", "paired"), "Invalid shutter state")
    _require(record.get("last_command") in (None, "open", "close", "stop"), "Invalid last command")


def _validate_result(operation: str, result: object, args: dict) -> dict:
    _require(isinstance(result, dict), "Result must be an object")
    if operation == "hello":
        _fields(result, {"product", "firmware", "device_id", "session", "max_line_bytes", "max_shutters", "capabilities"})
        _require(result["product"] == "ha-x2d", "Not an ha-x2d gateway")
        _require(_hex(result["device_id"]) and _hex(result["session"]), "Invalid device/session")
        _require(isinstance(result["firmware"], str) and 0 < len(result["firmware"]) <= 64, "Invalid firmware")
        _require(_integer(result["max_line_bytes"], MAX_LINE_BYTES, MAX_LINE_BYTES), "Incompatible message limit")
        _require(_integer(result["max_shutters"], MAX_SHUTTERS, MAX_SHUTTERS), "Incompatible slot limit")
        caps = result["capabilities"]
        _require(isinstance(caps, list) and all(isinstance(v, str) for v in caps), "Invalid capabilities")
        _require(len(caps) == len(set(caps)) and {"status", "shutters"}.issubset(caps), "Missing capabilities")
    elif operation == "status":
        _fields(result, {"uptime_ms", "tx_enabled", "radio", "storage"})
        _require(_integer(result["uptime_ms"], 0, 0xFFFFFFFF), "Invalid uptime")
        _require(type(result["tx_enabled"]) is bool, "Invalid TX readiness")
        radio = result["radio"]
        _fields(radio, {"detected", "partnum", "version", "marcstate"})
        _require(type(radio["detected"]) is bool, "Invalid radio detection")
        for field in ("partnum", "version", "marcstate"):
            _require(_integer(radio[field], 0, 255) if radio["detected"] else radio[field] is None, f"Invalid radio {field}")
        storage = result["storage"]
        _fields(storage, {"state", "generation"})
        _require(storage["state"] in ("empty", "ready", "corrupt", "full"), "Invalid storage state")
        _require(_hex(storage["generation"]) or (storage["generation"] is None and storage["state"] in ("empty", "corrupt")), "Invalid storage generation")
    elif operation == "shutters":
        _fields(result, {"generation", "shutters"})
        records = result["shutters"]
        _require(isinstance(records, list) and len(records) <= MAX_SHUTTERS, "Invalid shutters")
        _require(_hex(result["generation"]) or (result["generation"] is None and not records), "Invalid generation")
        slots = set()
        for record in records:
            _fields(record, {"shutter_id", "state", "last_command"})
            _record(record)
            _require(record["shutter_id"] not in slots, "Duplicate shutter slot")
            slots.add(record["shutter_id"])
    elif operation in ("provision", "confirm"):
        _fields(result, {"generation", "shutter_id", "state", "last_command"})
        _record(result)
        _require(_hex(result["generation"]), "Invalid generation")
        _require(result["shutter_id"] == args["shutter_id"], "Unexpected shutter slot")
        if operation == "confirm":
            _require(result["state"] == "paired", "Confirmation did not persist")
    else:
        _fields(result, {"accepted", "shutter_id"})
        _require(result["accepted"] is True, "Invalid TX acknowledgement")
        _require(_integer(result["shutter_id"], args["shutter_id"], args["shutter_id"]), "Unexpected shutter slot")
    return result


def _json_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON field")
        result[key] = value
    return result


class Gateway:
    """Own a port until close; concurrent requests never block STOP on a TX wait."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._sequence = 0
        self._event_sequence: int | None = None
        self._pending: dict[int, tuple[str, dict, asyncio.Future]] = {}
        self._tx: dict[int, tuple[int, asyncio.Future, bool]] = {}
        self._failure: Exception | None = None
        self.info: dict = {}
        self.on_disconnect: Callable[[Exception], None] | None = None
        self._callbacks: list[Callable[[dict], None]] = []
        self._reader_task = asyncio.create_task(self._read_loop())

    @property
    def closed(self) -> bool:
        return self._writer.is_closing()

    @classmethod
    async def open(cls, device: str, *, expected_device_id: str | None = None) -> "Gateway":
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=MAX_LINE_BYTES)
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            async with asyncio.timeout(TIMEOUT):
                transport, _ = await create_serial_connection(
                    loop, lambda: protocol, url=device, baudrate=115200, exclusive=True
                )
        except SerialException as exc:
            raise ValueError("Unsupported serial port or connection settings") from exc
        writer = asyncio.StreamWriter(transport, protocol, reader, loop)
        gateway = cls(reader, writer)
        try:
            gateway.info = await gateway._request("hello")
            if expected_device_id is not None:
                _require(gateway.info["device_id"] == expected_device_id, "Different gateway")
            return gateway
        except BaseException:
            await gateway.close()
            raise

    def add_event_callback(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        """Synchronous notifications after validation; callbacks must not block."""
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback)

    async def status(self) -> dict:
        return await self._request("status")

    async def shutters(self) -> dict:
        return await self._request("shutters")

    async def provision(self, shutter_id: int) -> dict:
        return await self._slot_request("provision", shutter_id)

    async def pair(self, shutter_id: int) -> dict:
        return await self._slot_request("pair", shutter_id)

    async def confirm(self, shutter_id: int) -> dict:
        return await self._slot_request("confirm", shutter_id)

    async def command(self, shutter_id: int, action: str) -> dict:
        if action not in ("open", "close", "stop"):
            raise ValueError("Unsupported shutter action")
        return await self._slot_request("command", shutter_id, action=action)

    async def _slot_request(self, operation: str, shutter_id: int, **args) -> dict:
        if not _integer(shutter_id, 1, MAX_SHUTTERS):
            raise ValueError("Shutter slot must be an integer from 1 to 16")
        return await self._request(operation, {"shutter_id": shutter_id, **args})

    async def _request(self, operation: str, args: dict | None = None) -> dict:
        if self.closed:
            raise ConnectionError("Gateway is closed") from self._failure
        if self._sequence == 0x7FFFFFFF:
            await self.close()
            raise ConnectionError("Request IDs exhausted; open a new session")
        self._sequence += 1
        request_id = self._sequence
        args = args or {}
        request = {"v": PROTOCOL_VERSION, "id": request_id, "op": operation}
        if operation != "hello":
            request.update(session=self.info["session"], args=args)
        reply = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (operation, args, reply)
        tx = None
        if operation in ("pair", "command"):
            tx = asyncio.get_running_loop().create_future()
            self._tx[request_id] = (args["shutter_id"], tx, False)
        try:
            # Whole lines are written before yielding; no lock spans an ACK/TX wait.
            self._writer.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            async with asyncio.timeout(TIMEOUT):
                await self._writer.drain()
                result = await reply
            if tx is not None:
                async with asyncio.timeout(TX_TIMEOUT):
                    result = await tx
                if result["result"] == "unknown":
                    raise CommandUncertain(result.get("error", "command_uncertain"))
                if "error" in result or result["result"] != "emitted":
                    raise GatewayError(result.get("error", f"tx_{result['result']}"))
            return result
        except GatewayError:
            raise
        except BaseException as exc:
            self._terminate(exc if isinstance(exc, Exception) else ConnectionError("Request cancelled"))
            await self.close()
            if tx is not None and isinstance(exc, (OSError, TimeoutError, ProtocolError)):
                raise CommandUncertain() from exc
            raise
        finally:
            self._pending.pop(request_id, None)
            self._tx.pop(request_id, None)
            for future in (reply, tx):
                if future is not None:
                    if not future.done():
                        future.cancel()
                    elif not future.cancelled():
                        future.exception()

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    line = await self._reader.readline()
                except ValueError as exc:
                    raise ProtocolError("Response exceeds message limit") from exc
                if not line:
                    raise ConnectionError("Gateway disconnected")
                _require(len(line) <= MAX_LINE_BYTES and line.endswith(b"\n"), "Oversized or truncated response")
                try:
                    message = json.loads(line.decode("utf-8"), object_pairs_hook=_json_object,
                                         parse_constant=lambda _: _require(False, "Invalid JSON constant"))
                except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                    raise ProtocolError("Invalid JSON response") from exc
                _require(isinstance(message, dict), "Message must be an object")
                _require(_integer(message.get("v"), 2, 2), "Incompatible protocol version")
                if "event" in message:
                    self._event(message)
                else:
                    self._response(message)
        except asyncio.CancelledError:
            return
        except (OSError, ValueError, TypeError) as exc:
            self._terminate(exc if isinstance(exc, (OSError, ProtocolError)) else ProtocolError("Invalid peer data"))

    def _response(self, message: dict) -> None:
        request_id = message.get("id")
        _require(_integer(request_id, 1, 0x7FFFFFFF) and request_id in self._pending, "Unexpected response id")
        _require(type(message.get("ok")) is bool, "Invalid response status")
        operation, args, future = self._pending[request_id]
        _require(not future.done(), "Late response to a completed request")
        _fields(message, {"v", "id", "ok", "result" if message["ok"] else "error"})
        if message["ok"]:
            result = _validate_result(operation, message["result"], args)
            if operation == "hello":
                self.info = result  # A subsequent event in the same read sees this session.
            if request_id in self._tx:
                slot, tx, _ = self._tx[request_id]
                self._tx[request_id] = (slot, tx, True)
            future.set_result(result)
        else:
            _require(isinstance(message["error"], str) and message["error"] in ERRORS, "Invalid application error")
            future.set_exception(GatewayError(message["error"]))
        self._pending.pop(request_id)

    def _event(self, event: dict) -> None:
        _require(self.info and event.get("session") == self.info["session"], "Stale event session")
        seq = event.get("seq")
        _require(_integer(seq, 0, 0xFFFFFFFF), "Invalid event sequence")
        if self._event_sequence is not None:
            _require(0 < (seq - self._event_sequence) % (1 << 32) < (1 << 31), "Nonmonotonic event sequence")
        self._event_sequence = seq
        if event["event"] == "tx_result":
            _fields(event, {"v", "session", "seq", "event", "request_id", "shutter_id", "result"}
                    | (event.keys() & {"error", "completed_copies"}))
            request_id = event["request_id"]
            _require(_integer(request_id, 1, 0x7FFFFFFF) and request_id in self._tx, "Unexpected TX request id")
            slot, future, acknowledged = self._tx[request_id]
            _require(acknowledged and not future.done(), "TX event without acknowledgement or duplicate")
            _require(_integer(event["shutter_id"], slot, slot), "Unexpected TX slot")
            _require(event["result"] in ("emitted", "cancelled", "expired", "unknown", "rejected"), "Invalid TX result")
            if "error" in event:
                _require(isinstance(event["error"], str) and event["error"] in ERRORS, "Invalid TX error")
            if "completed_copies" in event:
                _require(_integer(event["completed_copies"], 0, 64), "Invalid completed copies")
            future.set_result(event)
        elif event["event"] == "rx":
            _fields(event, {"v", "session", "seq", "event", "identity", "action", "counter"} | ({"shutter_id"} if "shutter_id" in event else set()))
            _require(_hex(event["identity"], 6), "Invalid RX identity")
            _require(event["action"] in ("open", "close", "stop"), "Invalid RX action")
            _require(_integer(event["counter"], 0, 0xFFFF), "Invalid RX counter")
            if "shutter_id" in event:
                _require(_integer(event["shutter_id"], 1, MAX_SHUTTERS), "Invalid RX slot")
        else:
            raise ProtocolError("Unknown event")
        for callback in tuple(self._callbacks):
            try:
                callback(event.copy())
            except Exception as exc:
                asyncio.get_running_loop().call_exception_handler({"message": "Gateway event callback failed", "exception": exc})

    def _terminate(self, cause: Exception) -> None:
        if self._failure is not None:
            return
        self._failure = cause
        self._writer.close()
        for future in [v[2] for v in self._pending.values()] + [v[1] for v in self._tx.values()]:
            if not future.done():
                future.set_exception(cause)
        if self.on_disconnect is not None:
            self.on_disconnect(cause)

    async def close(self) -> None:
        """Close on cancellation/timeout; late data can never satisfy a new request."""
        self._terminate(ConnectionError("Gateway closed"))
        if self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
            await self._reader_task
        try:
            async with asyncio.timeout(1):
                await self._writer.wait_closed()
        except (OSError, TimeoutError):
            pass
