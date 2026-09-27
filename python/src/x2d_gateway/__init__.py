"""USB gateway client. No Home Assistant imports or radio encoding here."""

import asyncio
import json
import re

from serialx import SerialException, create_serial_connection

MAX_LINE_BYTES = 512
PROTOCOL_VERSION = 1
TIMEOUT = 3.0


class ProtocolError(ValueError):
    """The peer is incompatible, or its response cannot be trusted."""


def _require(condition: bool, description: str) -> None:
    if not condition:
        raise ProtocolError(description)


def _integer(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _validate_result(operation: str, result: object) -> dict:
    _require(isinstance(result, dict), "Result must be an object")
    if operation == "hello":
        _require(result.get("product") == "ha-x2d", "Not an ha-x2d gateway")
        for field in ("device_id", "session"):
            value = result.get(field)
            _require(
                isinstance(value, str) and re.fullmatch(r"[0-9A-F]{16}", value) is not None,
                f"Invalid {field}",
            )
        version = result.get("firmware")
        _require(isinstance(version, str) and 0 < len(version) <= 64, "Invalid firmware")
        _require(
            _integer(result.get("max_line_bytes"), MAX_LINE_BYTES, MAX_LINE_BYTES),
            "Incompatible message limit",
        )
        capabilities = result.get("capabilities")
        _require(
            isinstance(capabilities, list)
            and all(isinstance(value, str) for value in capabilities)
            and {"info", "status", "cc1101_probe"}.issubset(capabilities),
            "Missing diagnostic capabilities",
        )
    else:
        _require(_integer(result.get("uptime_ms"), 0, 0xFFFFFFFF), "Invalid uptime")
        _require(result.get("tx_enabled") is False, "Expected diagnostic-only firmware")
        radio = result.get("radio")
        _require(isinstance(radio, dict), "Invalid radio status")
        _require(type(radio.get("detected")) is bool, "Invalid radio detection")
        for field in ("partnum", "version", "marcstate"):
            _require(field in radio, f"Missing radio {field}")
            _require(
                _integer(radio[field], 0, 255) if radio["detected"] else radio[field] is None,
                f"Invalid radio {field}",
            )
    return result


class Gateway:
    """One serial owner and one outstanding request; a failed session is closed.

    Reopening is an explicit caller decision. No command is ever replayed here.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()
        self._sequence = 0
        self.info: dict = {}

    @classmethod
    async def open(cls, device: str, *, expected_device_id: str | None = None) -> "Gateway":
        """Open exclusively, then verify the product and physical identity."""
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

    async def status(self) -> dict:
        """Read gateway/SPI diagnostics; this does not enable RF reception or TX."""
        return await self._request("status")

    async def _request(self, operation: str) -> dict:
        if operation not in ("hello", "status"):
            raise ValueError("Unsupported operation")
        async with self._lock:
            if self._writer.is_closing():
                raise ConnectionError("Gateway is closed")
            self._sequence = self._sequence % 0x7FFFFFFF + 1
            request = {"v": PROTOCOL_VERSION, "id": self._sequence, "op": operation}
            try:
                async with asyncio.timeout(TIMEOUT):
                    self._writer.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
                    await self._writer.drain()
                    try:
                        line = await self._reader.readline()
                    except ValueError as exc:
                        raise ProtocolError("Response exceeds message limit") from exc
                    if not line:
                        raise ConnectionError("Gateway disconnected")
                    _require(
                        len(line) <= MAX_LINE_BYTES and line.endswith(b"\n"),
                        "Oversized or truncated response",
                    )
                    try:
                        response = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                        raise ProtocolError("Invalid JSON response") from exc
                    _require(isinstance(response, dict), "Response must be an object")
                    _require(
                        _integer(response.get("v"), PROTOCOL_VERSION, PROTOCOL_VERSION),
                        "Incompatible protocol version",
                    )
                    _require(
                        _integer(response.get("id"), self._sequence, self._sequence),
                        "Unexpected response id",
                    )
                    _require(type(response.get("ok")) is bool, "Invalid response status")
                    _require(response["ok"], "Gateway rejected the request")
                    return _validate_result(operation, response.get("result"))
            except BaseException:
                # A late response must never satisfy a later request.
                await self.close()
                raise

    async def close(self) -> None:
        """Release the port, including after an unplug or a cancelled request."""
        self._writer.close()
        try:
            async with asyncio.timeout(1):
                await self._writer.wait_closed()
        except (OSError, TimeoutError):
            pass
