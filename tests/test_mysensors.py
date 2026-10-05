"""Native Home Assistant MySensors over a PTY, against the dongle's C++ adapter.

Nothing here simulates MySensors: the executable runs the dongle's real
x2d::mysensors::Gateway over simulated flash and radio, Home Assistant is the
installed 2026.9.4 core with its own mysensors integration (real config flow,
pymysensors 0.26.0 AsyncSerialGateway, real config entry, entity and device
registries), and the PTY bridge below only forwards bytes in small fragments and
observes them. RF is read from the server's stderr report, which decodes every
burst back to identity, action byte and counter. Simulated radio and flash do
not qualify real RF, USB timing or motors.

Prerequisites: build the mysensors_server CMake target (X2D_MYSENSORS_SERVER can
override build/native/mysensors_server) and install pymysensors==0.26.0 and
paho-mqtt==2.1.0 (Home Assistant's mysensors imports the mqtt integration).
"""

import asyncio
from collections import Counter, OrderedDict
import contextlib
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import tty
import struct
import zlib
import unittest

from homeassistant import loader  # first: the package aliases voluptuous before pymysensors imports it
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.components.mysensors.const import DOMAIN, MYSENSORS_GATEWAYS
from homeassistant.config_entries import ConfigEntries
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.core_config import CORE_CONFIG_SCHEMA, async_process_ha_core_config
from homeassistant.helpers import device_registry, entity_registry, frame, translation

try:
    importlib.import_module("homeassistant.components.mysensors.gateway")
except ModuleNotFoundError as error:  # fail loudly: a skip would hide the whole suite
    raise ImportError(
        f"{error}: install pymysensors==0.26.0 and paho-mqtt==2.1.0 in the test environment"
    ) from error

ROOT = Path(__file__).resolve().parents[1]
SERVER = Path(os.environ.get("X2D_MYSENSORS_SERVER", ROOT / "build/native/mysensors_server"))

NODE, PAIR, DIAGNOSTIC, INITIALIZE = 1, 17, 19, 20
COVER, SERVICE, ASSOCIATION, RETRY, RETIRE, STATE = 1, 2, 3, 4, 5, 6
V_STATUS, V_VAR1 = 2, 24
OPEN, CLOSE, STOP = 0x81, 0x82, 0x04
SLOTS = range(1, 17)
OUTPUTS = ROOT / "build/lifecycle-tests"
OUTPUTS.mkdir(parents=True, exist_ok=True)


def temporary_directory(prefix):
    return tempfile.TemporaryDirectory(prefix=prefix, dir=OUTPUTS)


CUSTOMIZE = {"customize_glob": {"cover.volet_x2d_*": {"assumed_state": True}}}  # the one accepted YAML rule


def write_legacy_journal(path, rf_identity):
    """Independent v1 on-disk fixture: one paired slot and a verified commit.

    No production Journal serializer is used, so reset/exclusion checks cannot
    pass merely because the fixture accidentally writes the new format.
    """
    data = bytearray(b"\xff" * 65536)
    body = bytearray(b"\xff" * 256)
    struct.pack_into("<IHHQII", body, 0, 0x4A443258, 1, 16, 0x0123456789ABCDEF, 1, 0)
    for slot in range(16):
        struct.pack_into("<IIBBBB", body, 24 + slot * 12,
                         rf_identity if slot == 0 else 0, 100 if slot == 0 else 0,
                         2 if slot == 0 else 0, 0, 0, 0)
    crc = zlib.crc32(body[:252])
    struct.pack_into("<I", body, 252, crc)
    commit = bytearray(b"\xff" * 256)
    struct.pack_into("<III", commit, 0, 0x43443258, 1, crc)
    struct.pack_into("<I", commit, 12, zlib.crc32(commit[:12]))
    data[:256], data[256:512] = body, commit
    path.write_bytes(data)


def identity(slot, base=0xA00000):
    """Radio identity of a simulated slot (see mysensors_server --identity-base)."""
    return base | slot << 8 | slot


def first_counter(slot):
    return 100 * slot


def fragments(data):
    """Single-byte to three-byte writes: the line framers must not need whole lines."""
    offset = 0
    while offset < len(data):
        size = 1 + offset % 3
        yield data[offset:offset + size]
        offset += size


async def until(predicate, what, timeout=10):
    try:
        async with asyncio.timeout(timeout):
            while not (result := predicate()):
                await asyncio.sleep(0.01)
            return result
    except TimeoutError:
        raise AssertionError(f"timed out waiting for {what}") from None


class Dongle:
    """PTY + executable bridge. It owns no protocol: bytes in, bytes out, RF report."""

    START = re.compile(r"burst start identity=(\w+) action=(\w+) counter=(\d+) copies=(\d+)$")
    READY = re.compile(r"ready paired=(\d+) generation=(\w+)$")
    END = re.compile(r"burst end completed=(\d+) stopped=(\d)$")

    def __init__(self):
        self.master, self.slave = os.openpty()  # kept open: the port survives server restarts
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.device = os.ttyname(self.slave)
        self.incoming = asyncio.Queue()
        self.from_host, self.from_dongle, self.rf, self.ready, self.unexpected = [], [], [], [], []
        self.exit_codes, self.failed = [], False
        self.process = self.control = None
        self.tasks = []
        self._buffers = {"host": bytearray(), "dongle": bytearray()}
        asyncio.get_running_loop().add_reader(self.master, self._read_master)

    def _read_master(self):
        try:
            data = os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            return
        if data:
            self.incoming.put_nowait(data)

    def _record(self, side, lines, data):
        buffer = self._buffers[side]
        buffer.extend(data)
        while b"\n" in buffer:
            end = buffer.index(b"\n") + 1
            lines.append(bytes(buffer[:end - 1]).decode())
            del buffer[:end]

    async def start(self, *args):
        self.control, child = socket.socketpair()
        self.control.setblocking(False)
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(SERVER), f"--control-fd={child.fileno()}", *args, pass_fds=(child.fileno(),),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        finally:
            child.close()
        while not self.incoming.empty():  # bytes written to a port nobody was listening on
            self.incoming.get_nowait()
        self._buffers["host"].clear()
        self._buffers["dongle"].clear()
        process, known = self.process, len(self.ready)
        self.tasks = [asyncio.create_task(self._pump_host(process)),
                      asyncio.create_task(self._pump_dongle(process)),
                      asyncio.create_task(self._pump_stderr(process))]
        await until(lambda: len(self.ready) > known, "the server's ready line")

    async def _pump_host(self, process):
        try:
            while True:
                data = await self.incoming.get()
                for chunk in fragments(data):
                    process.stdin.write(chunk)
                    await process.stdin.drain()
                    self._record("host", self.from_host, chunk)
                    await asyncio.sleep(0)
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _pump_dongle(self, process):
        while data := await process.stdout.read(4096):
            for chunk in fragments(data):
                while True:
                    try:
                        os.write(self.master, chunk)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.001)
                    except OSError:
                        return
                self._record("dongle", self.from_dongle, chunk)
                await asyncio.sleep(0)

    async def _pump_stderr(self, process):
        async for raw in process.stderr:
            line = raw.decode().rstrip("\n")
            text = line.removeprefix("x2d-sim: ")
            if text == line:
                self.unexpected.append(line)
            elif match := self.READY.match(text):
                self.ready.append((int(match[1]), match[2]))
            elif match := self.START.match(text):
                self.rf.append({"kind": "start", "identity": int(match[1], 16),
                                "action": int(match[2], 16), "counter": int(match[3]),
                                "copies": int(match[4])})
            elif text.startswith("burst start undecoded"):
                self.rf.append({"kind": "start", "identity": None, "action": None,
                                "counter": None, "copies": int(text.rsplit("=", 1)[1])})
            elif text == "burst stop requested":
                self.rf.append({"kind": "stop"})
            elif match := self.END.match(text):
                self.rf.append({"kind": "end", "completed": int(match[1]), "stopped": match[2] == "1"})
            else:
                self.unexpected.append(line)

    def bursts(self):
        return [event for event in self.rf if event["kind"] == "start"]

    def ends(self):
        return [event for event in self.rf if event["kind"] == "end"]

    async def _control(self, byte):
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(3):
            await loop.sock_sendall(self.control, byte)
            while (answer := await loop.sock_recv(self.control, 1)) == b"F":
                self.failed = True
        if answer != byte:
            raise AssertionError(f"server control reply {answer!r}, expected {byte!r}")

    async def connect(self):
        """The host opened the port (DTR): the server calls gateway.connected()."""
        await self._control(b"C")

    async def disconnect(self):
        """The host closed the port: gateway.disconnected(); unread input is lost."""
        await self._control(b"D")

    async def hold(self):
        await self._control(b"H")

    async def release(self):
        await self._control(b"R")

    async def stop(self):
        process = self.process
        if process is None:
            return
        self.process = None
        process.stdin.close()
        try:
            async with asyncio.timeout(3):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
        self.exit_codes.append(process.returncode)
        host, *readers = self.tasks
        host.cancel()
        await asyncio.wait_for(asyncio.gather(*readers), 3)  # forward every byte the process wrote
        await asyncio.gather(host, return_exceptions=True)
        self.control.close()

    async def restart(self, *args, connect=True):
        await self.stop()
        await self.start(*args)
        if connect:
            await self.connect()

    async def close(self):
        await self.stop()
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)
        os.close(self.slave)


class Rig:
    """One Home Assistant, one config entry and one dongle, with entity lookups."""

    def __init__(self, test, hass, dongle, entry):
        self.test, self.hass, self.dongle, self.entry = test, hass, dongle, entry
        self.history = []
        hass.bus.async_listen(EVENT_STATE_CHANGED, self._changed)

    def _changed(self, event):
        new = event.data["new_state"]
        self.history.append((event.data["entity_id"], new.state if new else None))

    def unique_id(self, child, value_type=V_STATUS, node=NODE):
        return f"{self.entry.entry_id}-{node}-{child}-{value_type}"

    def entity_id(self, domain, child, value_type=V_STATUS, node=NODE):
        return entity_registry.async_get(self.hass).async_get_entity_id(
            domain, "mysensors", self.unique_id(child, value_type, node))

    def cover(self, slot):
        return self.entity_id("cover", COVER, node=slot + 1)

    def state(self, slot):
        entity_id = self.cover(slot)
        return self.hass.states.get(entity_id) if entity_id else None

    def covers(self):
        return {slot: self.state(slot) for slot in range(1, 254) if self.state(slot) is not None}

    def switch(self, child, node=NODE):
        entity_id = self.entity_id("switch", child, node=node)
        return self.hass.states.get(entity_id) if entity_id else None

    def device(self, node):
        return device_registry.async_get(self.hass).async_get_device_by_identifier(
            identifier=("mysensors", f"{self.entry.entry_id}-{node}"),
            config_entry_id=self.entry.entry_id)

    def node_diagnostic(self, node):
        entity_id = self.entity_id("sensor", STATE, V_VAR1, node=node)
        state = self.hass.states.get(entity_id) if entity_id else None
        return state.state if state else None

    def diagnostic(self):
        entity_id = self.entity_id("sensor", DIAGNOSTIC, V_VAR1)
        state = self.hass.states.get(entity_id) if entity_id else None
        return state.state if state else None

    def registry(self):
        """entity_id -> unique_id for everything the entry registered."""
        return {e.entity_id: e.unique_id for e in entity_registry.async_entries_for_config_entry(
            entity_registry.async_get(self.hass), self.entry.entry_id)}

    async def command(self, service, slot):
        await self.hass.services.async_call(
            "cover", service, {"entity_id": self.cover(slot)}, blocking=True)

    async def press(self, child, on=True, node=NODE):
        await self.hass.services.async_call(
            "switch", "turn_on" if on else "turn_off", {"entity_id": self.entity_id("switch", child, node=node)},
            blocking=True)

    async def settled(self, covers=len(SLOTS)):
        await until(lambda: len(self.covers()) == covers and self.diagnostic() is not None
                    and self.switch(PAIR) is not None and self.switch(INITIALIZE) is not None
                    and self.diagnostic() != "Initialisation en cours"
                    and all(self.switch(child, slot + 1) is not None
                            for slot in self.covers() for child in (SERVICE, ASSOCIATION, RETRY, RETIRE))
                    and all(self.node_diagnostic(slot + 1) is not None for slot in self.covers()),
                    f"{covers} covers, manager switches and the diagnostic")
        await self.hass.async_block_till_done()

    async def remove_device(self, node):
        """The UI's Delete device: the integration hook, then the registry."""
        await asyncio.sleep(0.3)  # entity updates are debounced; a pending one would find the node gone
        registry = device_registry.async_get(self.hass)
        device = self.device(node)
        self.test.assertIsNotNone(device)
        component = importlib.import_module("homeassistant.components.mysensors")
        self.test.assertTrue(await component.async_remove_config_entry_device(self.hass, self.entry, device))
        registry.async_remove_device(device.id)
        await self.hass.async_block_till_done()

    def persisted(self):
        return json.loads(Path(self.hass.config.path(f"mysensors_{self.entry.entry_id}.json")).read_text())


class NativeMySensorsChecks(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if (version("homeassistant"), version("pymysensors")) != ("2026.9.4", "0.26.0"):
            raise AssertionError("Lifecycle evidence requires Home Assistant 2026.9.4 / pymysensors 0.26.0")
        if not SERVER.is_file() or not os.access(SERVER, os.X_OK):
            raise AssertionError(
                f"mysensors_server executable missing: {SERVER}. Build the mysensors_server CMake "
                "target (devenv test does) or set X2D_MYSENSORS_SERVER.")

    @contextlib.asynccontextmanager
    async def system(self, *server_args, customize=False, covers=len(SLOTS)):
        """The real config flow against the PTY, then the host opens the port."""
        dongle = Dongle()
        try:
            with temporary_directory(prefix="ha-x2d-mysensors-") as directory:
                hass = HomeAssistant(directory)
                hass.config.skip_pip = True
                hass.config.components.update({"http", "websocket_api"})  # not under test
                loader.async_setup(hass)
                frame.async_setup(hass)
                translation.async_setup(hass)
                device_registry.async_setup(hass)
                hass.config_entries = ConfigEntries(hass, {})
                await hass.config_entries.async_initialize()
                await device_registry.async_load(hass)
                await entity_registry.async_load(hass)
                if customize:
                    await async_process_ha_core_config(hass, CORE_CONFIG_SCHEMA(
                        {"customize_glob": OrderedDict(CUSTOMIZE["customize_glob"])}))
                try:
                    await dongle.start(*server_args)
                    manager = hass.config_entries.flow
                    flow = await manager.async_init("mysensors", context={"source": "user"})
                    flow = await manager.async_configure(flow["flow_id"], {"next_step_id": "gw_serial"})
                    flow = await manager.async_configure(flow["flow_id"], {
                        "device": dongle.device, "baud_rate": 115200, "version": "2.3"})
                    self.assertEqual(flow["type"], "create_entry", flow)
                    await hass.async_block_till_done()  # setup has opened the port
                    rig = Rig(self, hass, dongle, flow["result"])
                    await dongle.connect()
                    await rig.settled(covers)
                    yield rig
                finally:
                    # Keep exact observed serial lines and decoded RF in workspace
                    # artifacts, including failures; temporary HA files can then go.
                    evidence = {
                        "evidence": "native Home Assistant over PTY; flash and RF simulated; no hardware qualification",
                        "homeassistant": version("homeassistant"),
                        "pymysensors": version("pymysensors"),
                        "server_args": list(server_args),
                        "rf": dongle.rf,
                        "from_host": dongle.from_host,
                        "from_dongle": dongle.from_dongle,
                        "unexpected": dongle.unexpected,
                    }
                    (OUTPUTS / f"{self._testMethodName}.json").write_text(json.dumps(evidence, indent=2))
                    for entry in hass.config_entries.async_entries("mysensors"):
                        await hass.config_entries.async_unload(entry.entry_id)
                    await hass.async_stop(force=True)
        finally:
            await dongle.close()
        self.assertEqual(dongle.exit_codes, [0] * len(dongle.exit_codes))
        self.assertEqual(dongle.unexpected, [])
        self.assertFalse(dongle.failed)

    async def burst_done(self, rig, count):
        await until(lambda: len(rig.dongle.ends()) >= count and len(rig.dongle.bursts()) >= count,
                    f"{count} finished burst(s)")

    async def test_discovery_publishes_sixteen_binary_covers_and_transmits_nothing(self):
        async with self.system() as rig:
            await asyncio.sleep(0.3)
            entities = entity_registry.async_entries_for_config_entry(
                entity_registry.async_get(rig.hass), rig.entry.entry_id)
            self.assertEqual(Counter(e.domain for e in entities),
                             {"cover": 16, "switch": 66, "sensor": 34})  # HA creates its own battery sensor per node
            self.assertEqual(sorted(e.unique_id for e in entities if e.domain == "cover"),
                             sorted(rig.unique_id(COVER, node=slot + 1) for slot in SLOTS))
            devices = device_registry.async_entries_for_config_entry(
                device_registry.async_get(rig.hass), rig.entry.entry_id)
            self.assertEqual(len(devices), 17)
            cover_devices = set()
            for slot, state in rig.covers().items():
                with self.subTest(slot=slot):
                    registered = entity_registry.async_get(rig.hass).async_get(state.entity_id)
                    self.assertEqual(registered.device_id, rig.device(slot + 1).id)
                    cover_devices.add(registered.device_id)
                    attributes = state.attributes
                    self.assertEqual(state.state, "open")  # unknown position is shown as the default
                    self.assertEqual(attributes["supported_features"],
                                     CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP)
                    self.assertNotIn("current_position", attributes)
                    self.assertNotIn("current_tilt_position", attributes)
                    self.assertNotIn("assumed_state", attributes)  # supplied only by the YAML rule
                    self.assertEqual({k: v for k, v in attributes.items() if k.startswith("V_")},
                                     {"V_UP": "off", "V_DOWN": "off", "V_STOP": "on", "V_STATUS": "on"})
                    self.assertEqual(attributes["node_id"], slot + 1)
                    self.assertEqual(attributes["child_id"], COVER)
            self.assertEqual(len(cover_devices), 16)
            self.assertNotIn(rig.device(NODE).id, cover_devices)
            self.assertEqual(rig.diagnostic(), "Pret")
            self.assertEqual(rig.switch(PAIR).state, "off")
            self.assertEqual(rig.switch(INITIALIZE).state, "off")
            self.assertEqual(rig.dongle.bursts(), [])  # discovery never reaches the radio
            self.assertIn(f"{NODE};255;0;0;17;2.3.2", rig.dongle.from_dongle)

    async def test_one_global_assumed_state_rule_reaches_every_cover(self):
        async with self.system(customize=True) as rig:
            for slot, state in rig.covers().items():
                with self.subTest(slot=slot):
                    self.assertTrue(state.entity_id.startswith("cover.volet_x2d_"), state.entity_id)
                    self.assertIs(state.attributes["assumed_state"], True)
            for child in (PAIR, INITIALIZE):
                self.assertNotIn("assumed_state", rig.switch(child).attributes)

    async def test_open_close_and_stop_are_acknowledged_and_emit_one_burst_each(self):
        async with self.system() as rig:
            for slot in (3, 16):
                base = len(rig.dongle.bursts())
                sent = []
                for number, (service, action, sub, resulting) in enumerate(
                        (("open_cover", OPEN, 29, "open"), ("close_cover", CLOSE, 30, "closed"),
                         ("stop_cover", STOP, 31, "open"))):  # a stop leaves the position unknown
                    with self.subTest(slot=slot, service=service):
                        mark = len(rig.dongle.from_dongle)
                        await rig.command(service, slot)
                        await self.burst_done(rig, base + number + 1)
                        burst = rig.dongle.bursts()[base + number]
                        self.assertEqual((burst["identity"], burst["action"], burst["counter"], burst["copies"]),
                                         (identity(slot), action, first_counter(slot) + number, 25))
                        self.assertEqual(rig.dongle.ends()[base + number], {
                            "kind": "end", "completed": 25, "stopped": False})
                        ack = f"{slot + 1};1;1;1;{sub};1"  # the request with its ack flag, echoed
                        self.assertIn(ack, rig.dongle.from_host)
                        await until(lambda: ack in rig.dongle.from_dongle, f"the echo {ack}")
                        if action == CLOSE:
                            final = f"{slot + 1};1;1;0;2;0"
                            await until(lambda: final in rig.dongle.from_dongle[mark:],
                                        "authoritative closed snapshot after completed RF")
                            wire = rig.dongle.from_dongle[mark:]
                            self.assertLess(wire.index(ack), wire.index(final))
                        await until(lambda: rig.state(slot).state == resulting, f"{slot} {resulting}")
                        self.assertEqual(rig.state(slot).attributes["V_STOP"], "on")
                        self.assertNotIn("current_position", rig.state(slot).attributes)
                        sent.append(burst["identity"])
                self.assertEqual(rig.node_diagnostic(slot + 1), "Actif, position inconnue")
            self.assertEqual(set(sent), {identity(16)})
            self.assertEqual({b["identity"] for b in rig.dongle.bursts()}, {identity(3), identity(16)})
            states = {state for entity_id, state in rig.history if entity_id.startswith("cover.")}
            self.assertLessEqual(states, {"open", "closed"})  # no echo ever renders as motion

    async def test_group_close_completes_all_sixteen_real_duration_bursts(self):
        async with self.system("--burst-ms=1300") as rig:
            await asyncio.gather(*(rig.command("close_cover", slot) for slot in SLOTS))
            await until(lambda: len(rig.dongle.ends()) == len(SLOTS),
                        "all sixteen group commands completing", timeout=30)
            self.assertEqual({(b["identity"], b["action"], b["counter"]) for b in rig.dongle.bursts()},
                             {(identity(slot), CLOSE, first_counter(slot)) for slot in SLOTS})
            self.assertEqual(rig.dongle.ends(),
                             [{"kind": "end", "completed": 25, "stopped": False}] * len(SLOTS))
            await until(lambda: all(rig.state(slot).state == "closed" for slot in SLOTS),
                        "all sixteen covers closed")

    async def test_stop_preempts_a_running_movement_at_a_frame_boundary(self):
        async with self.system() as rig:
            await rig.dongle.hold()
            await rig.command("open_cover", 5)
            await until(lambda: len(rig.dongle.bursts()) == 1, "the movement burst")
            await rig.command("stop_cover", 5)
            await until(lambda: any(e["kind"] == "stop" for e in rig.dongle.rf), "the stop request")
            await rig.dongle.release()
            await self.burst_done(rig, 2)
            kinds = [e["kind"] for e in rig.dongle.rf]
            self.assertEqual(kinds, ["start", "stop", "end", "start", "end"])
            movement, stop = rig.dongle.bursts()
            self.assertEqual((movement["action"], stop["action"]), (OPEN, STOP))
            first, second = rig.dongle.ends()
            self.assertTrue(first["stopped"] and 1 <= first["completed"] < 25, first)
            self.assertEqual(second, {"kind": "end", "completed": 25, "stopped": False})
            self.assertEqual(stop["counter"], movement["counter"] + 1)
            await until(lambda: rig.node_diagnostic(6) == "Actif, position inconnue", "the position diagnostic")
            self.assertEqual(rig.state(5).state, "open")

    async def test_link_loss_cancels_motion_and_nothing_is_replayed(self):
        async with self.system() as rig:
            await rig.dongle.hold()
            await rig.command("close_cover", 4)
            await until(lambda: len(rig.dongle.bursts()) == 1, "the movement burst")
            await rig.dongle.disconnect()  # the host lets go while the burst is on the air
            await rig.dongle.release()
            await self.burst_done(rig, 1)
            cut = rig.dongle.ends()[0]
            self.assertTrue(cut["stopped"] and 1 <= cut["completed"] < 25, cut)  # whole frames only
            await rig.command("close_cover", 6)  # written to a port nobody is reading
            line = "7;1;1;1;30;1"
            await until(lambda: line in rig.dongle.from_host, "the lost command on the wire")
            await asyncio.sleep(0.3)
            await rig.dongle.connect()
            await until(lambda: rig.dongle.from_dongle.count(
                "0;255;3;0;14;Gateway startup complete.") == 2, "re-announce")
            await rig.hass.async_block_till_done()
            await asyncio.sleep(0.5)
            self.assertEqual(len(rig.dongle.bursts()), 1)  # no replay of either command
            self.assertEqual({rig.state(4).state, rig.state(6).state}, {"open"})
            self.assertIn("Interrompu", rig.diagnostic())  # the cut movement

    async def test_reload_keeps_every_entity_identity_and_does_not_transmit(self):
        async with self.system() as rig:
            before = rig.registry()
            states = {slot: state.state for slot, state in rig.covers().items()}
            self.assertEqual(len(before), 116)
            await rig.dongle.disconnect()
            await rig.hass.config_entries.async_reload(rig.entry.entry_id)
            await rig.hass.async_block_till_done()
            # Restored from pymysensors' JSON persistence before the dongle says anything.
            self.assertEqual(rig.registry(), before)
            self.assertEqual({slot: state.state for slot, state in rig.covers().items()}, states)
            # pymysensors saves outside HA's tracked tasks. need_save clears
            # only after the backup/new-file renames have finished.
            gateway = rig.hass.data[DOMAIN][MYSENSORS_GATEWAYS][rig.entry.entry_id]
            await until(lambda: not gateway.tasks.persistence.need_save,
                        "the reloaded gateway's persistence save")
            persisted = rig.persisted()
            self.assertEqual(sorted(map(int, persisted)), list(range(1, 18)))
            self.assertEqual(sorted(map(int, persisted[str(NODE)]["children"])), [17, 19, 20])
            self.assertEqual(sorted(map(int, persisted["4"]["children"])), list(range(1, 7)))
            self.assertEqual(persisted["4"]["children"]["1"]["values"],
                             {"29": "0", "30": "0", "31": "1", "2": "1"})
            await rig.dongle.connect()
            await until(lambda: rig.dongle.from_dongle.count("0;255;3;0;14;Gateway startup complete.") == 2,
                        "the second gateway-ready")
            await rig.hass.async_block_till_done()
            await asyncio.sleep(0.3)
            self.assertEqual(rig.registry(), before)
            self.assertEqual(len(rig.covers()), 16)
            self.assertEqual(rig.dongle.bursts(), [])

    async def test_cached_state_is_not_availability(self):
        """Documented limit: Home Assistant cannot see the dongle go away."""
        async with self.system() as rig:
            await rig.dongle.stop()  # the executable dies; the port itself stays open
            await rig.command("close_cover", 2)  # accepted by Home Assistant, delivered to nobody
            await asyncio.sleep(0.4)
            self.assertEqual({state.state for state in rig.covers().values()}, {"open"})
            self.assertEqual(rig.dongle.bursts(), [])

    async def test_journal_counters_survive_a_server_restart(self):
        with temporary_directory(prefix="x2d-journal-") as directory:
            args = (f"--journal={Path(directory) / 'journal.bin'}",)
            async with self.system(*args) as rig:
                await rig.command("open_cover", 2)
                await self.burst_done(rig, 1)
                await rig.dongle.restart(*args)
                self.assertEqual(rig.dongle.ready, [(16, "0123456789ABCDEF")] * 2)
                await until(lambda: rig.dongle.from_dongle.count("0;255;3;0;14;Gateway startup complete.") == 2,
                            "the restarted gateway-ready")
                await rig.hass.async_block_till_done()
                await rig.command("close_cover", 2)
                await self.burst_done(rig, 2)
                first, second = rig.dongle.bursts()
                self.assertEqual((first["counter"], second["counter"]), (200, 201))
                self.assertEqual((first["identity"], second["identity"]), (identity(2),) * 2)
                await until(lambda: rig.state(2).state == "closed", "the close")


    async def test_disabled_or_faulty_radio_never_claims_a_position(self):
        async with self.system() as rig:
            for server_args, outcome in ((("--no-tx",), "V4: Emission desactivee"),
                                         (("--fault=start",), "V4: Operation refusee"),
                                         (("--fault=poll",), "V4: Erreur radio")):
                with self.subTest(server_args=server_args):
                    known = len(rig.dongle.bursts())
                    await rig.dongle.restart(*server_args)
                    await until(lambda: rig.dongle.ready and rig.diagnostic() is not None, "reconnect")
                    await rig.hass.async_block_till_done()
                    await rig.command("close_cover", 3)
                    await until(lambda: rig.diagnostic() == outcome, f"diagnostic {outcome}")
                    await asyncio.sleep(0.2)
                    self.assertEqual(rig.state(3).state, "open")  # never closed without a verified emission
                    self.assertEqual(len(rig.dongle.bursts()) - known, 1 if "poll" in server_args[0] else 0)



    async def candidate(self, rig, node=2):
        """Explicit manager action; enrollment is observed only in simulated RF."""
        before = len(rig.dongle.ends())
        await rig.press(PAIR)
        await until(lambda: rig.switch(SERVICE, node) is not None
                    and rig.switch(ASSOCIATION, node) is not None,
                    f"native controls for candidate node {node}")
        await self.burst_done(rig, before + 2)
        await until(lambda: rig.node_diagnostic(node) == "Mouvement a confirmer",
                    f"node {node} awaiting human confirmation")
        self.assertEqual(rig.switch(PAIR).state, "on")
        self.assertEqual(rig.switch(SERVICE, node).state, "off")
        self.assertEqual(rig.switch(ASSOCIATION, node).state, "on")

    async def confirm(self, rig, node=2):
        await rig.press(SERVICE, node=node)
        await until(lambda: rig.state(node - 1) is not None
                    and rig.switch(SERVICE, node).state == "on",
                    f"node {node} confirmed and cover automatically discovered")
        await until(lambda: rig.switch(PAIR).state == "off"
                    and rig.switch(ASSOCIATION, node).state == "off",
                    "authoritative lifecycle controls after confirmation")
        await rig.hass.async_block_till_done()


    async def test_broadcast_actuator_sets_never_allocate_devices_or_emit_rf(self):
        async with self.system("--paired=0", "--enrollment", covers=0) as rig:
            before = rig.registry()
            # Injection is at the host PTY endpoint. The bridge fragments and
            # forwards these bytes unchanged, through the real Gateway framer.
            commands = "".join(f"{node};{child};1;1;{value_type};1\n"
                               for node in (0, 255)
                               for child, value_type in ((PAIR, V_STATUS), (COVER, 29),
                                                         (SERVICE, V_STATUS), (ASSOCIATION, V_STATUS)))
            os.write(rig.dongle.slave, commands.encode())
            await until(lambda: all(line in rig.dongle.from_host
                                    for line in commands.splitlines()), "broadcast SET bytes forwarded")
            await asyncio.sleep(0.4)
            self.assertEqual(rig.registry(), before)
            self.assertEqual(rig.covers(), {})
            self.assertEqual(rig.dongle.bursts(), [])
            self.assertEqual(rig.switch(PAIR).state, "off")

    async def test_fresh_manager_add_discovers_per_device_controls_then_confirmation_cover(self):
        async with self.system("--paired=0", "--enrollment", covers=0) as rig:
            await asyncio.sleep(0.2)
            self.assertEqual(rig.dongle.bursts(), [])
            self.assertIsNotNone(rig.device(NODE))
            await self.candidate(rig)
            self.assertEqual(rig.covers(), {})
            candidate = rig.device(2)
            self.assertIsNotNone(candidate)
            self.assertNotEqual(candidate.id, rig.device(NODE).id)
            candidate_entities = entity_registry.async_entries_for_device(
                entity_registry.async_get(rig.hass), candidate.id)
            self.assertEqual(Counter(e.domain for e in candidate_entities), {"switch": 4, "sensor": 2})
            self.assertEqual([b["counter"] for b in rig.dongle.bursts()], [0, 1])
            self.assertEqual({b["copies"] for b in rig.dongle.bursts()}, {24})
            self.assertEqual([b["action"] for b in rig.dongle.bursts()], [0x02, 0x20])
            rf_id = rig.dongle.bursts()[0]["identity"]
            self.assertEqual(rf_id & 0xFF, 1)  # public, hardware-unqualified candidate suffix
            self.assertEqual({b["identity"] for b in rig.dongle.bursts()}, {rf_id})
            await self.confirm(rig)
            cover = entity_registry.async_get(rig.hass).async_get(rig.cover(1))
            self.assertEqual((cover.unique_id, cover.device_id), (rig.unique_id(COVER, node=2), candidate.id))
            self.assertEqual(rig.state(1).attributes["child_id"], COVER)
            self.assertNotIn("current_position", rig.state(1).attributes)
            await rig.command("open_cover", 1)
            await self.burst_done(rig, 3)
            self.assertEqual((rig.dongle.bursts()[-1]["identity"], rig.dongle.bursts()[-1]["counter"]),
                             (rf_id, 2))
            # Firmware never invents battery measurements or percentages.
            self.assertFalse([line for line in rig.dongle.from_dongle
                              if (line.split(";")[2:5] == ["3", "0", "0"]
                                  or line.split(";")[2:5] == ["1", "0", "3"])])

    async def test_decline_and_double_click_consume_one_candidate_and_never_reuse_node(self):
        async with self.system("--paired=0", "--enrollment", covers=0) as rig:
            await self.candidate(rig)
            before = len(rig.dongle.from_dongle)
            await rig.press(PAIR)  # duplicate ON after the RF attempt is also idempotent
            await asyncio.sleep(0.3)
            self.assertEqual(len(rig.dongle.bursts()), 2)
            self.assertFalse(any(line.startswith("3;") for line in rig.dongle.from_dongle[before:]))
            await rig.press(ASSOCIATION, on=False, node=2)
            await until(lambda: rig.switch(PAIR).state == "off"
                        and rig.switch(ASSOCIATION, 2).state == "off", "declined candidate")
            self.assertEqual(rig.covers(), {})
            await self.candidate(rig, node=3)
            await self.confirm(rig, node=3)
            self.assertIsNone(rig.state(1))
            self.assertIsNotNone(rig.state(2))
            first, _, new, _ = rig.dongle.bursts()
            self.assertNotEqual(first["identity"], new["identity"])
            self.assertEqual(new["counter"], 0)

    async def test_retry_is_explicit_single_and_returns_authoritative_off(self):
        async with self.system("--paired=0", "--enrollment", covers=0) as rig:
            await self.candidate(rig)
            await rig.press(RETRY, node=2)
            await self.burst_done(rig, 4)
            await until(lambda: rig.node_diagnostic(2) == "Mouvement a confirmer"
                        and rig.switch(RETRY, 2).state == "off", "retry completed")
            self.assertEqual([b["counter"] for b in rig.dongle.bursts()], [0, 1, 2, 3])
            self.assertEqual(len({b["identity"] for b in rig.dongle.bursts()}), 1)
            mark = len(rig.dongle.from_dongle)
            await rig.press(RETRY, node=2)
            await until(lambda: rig.node_diagnostic(2) == "Essais epuises: annuler",
                        "retry-limit refusal on the shutter device")
            self.assertTrue(any("Essais epuises" in line
                                for line in rig.dongle.from_dongle[mark:]))
            self.assertEqual(rig.switch(SERVICE, 2).state, "off")
            self.assertEqual(rig.switch(ASSOCIATION, 2).state, "on")
            await until(lambda: rig.switch(RETRY, 2).state == "off", "rejected action reset")
            await asyncio.sleep(0.3)
            self.assertEqual(len(rig.dongle.bursts()), 4)
            await self.confirm(rig)
            await rig.command("close_cover", 1)
            await self.burst_done(rig, 5)
            self.assertEqual(rig.dongle.bursts()[-1]["counter"], 4)

    async def test_retire_then_slot_reuse_discovers_new_device_and_fences_old_ha_commands(self):
        async with self.system("--paired=1", "--enrollment", covers=1) as rig:
            old_device = rig.device(2)
            old_cover = rig.cover(1)
            old_uid = entity_registry.async_get(rig.hass).async_get(old_cover).unique_id
            mark = len(rig.dongle.from_dongle)
            await rig.press(RETIRE, node=2)  # enabled shutters cannot be retired
            await until(lambda: rig.node_diagnostic(2) == "Desactiver le volet",
                        "enabled retirement refusal on the shutter device")
            self.assertTrue(any("Desactiver" in line for line in rig.dongle.from_dongle[mark:]))
            self.assertEqual(rig.switch(SERVICE, 2).state, "on")
            await until(lambda: rig.switch(RETIRE, 2).state == "off", "retire refusal reset")
            await rig.press(SERVICE, on=False, node=2)
            await until(lambda: rig.switch(SERVICE, 2).state == "off", "disabled old shutter")
            await rig.press(RETIRE, node=2)
            await until(lambda: rig.node_diagnostic(2) == "Retire", "retired snapshot")
            await self.candidate(rig, node=3)
            await self.confirm(rig, node=3)
            self.assertNotEqual(rig.device(3).id, old_device.id)
            self.assertNotEqual(rig.cover(2), old_cover)
            self.assertNotEqual(entity_registry.async_get(rig.hass).async_get(rig.cover(2)).unique_id, old_uid)
            self.assertEqual(rig.cover(1), old_cover)  # stock HA keeps the retired device
            rf_before = len(rig.dongle.bursts())
            for service in ("open_cover", "close_cover", "stop_cover"):
                await rig.command(service, 1)
            for child in (SERVICE, ASSOCIATION, RETRY, RETIRE):
                await rig.press(child, node=2)
            await asyncio.sleep(0.5)
            self.assertEqual(len(rig.dongle.bursts()), rf_before)
            self.assertEqual(rig.switch(SERVICE, 2).state, "off")
            self.assertEqual(rig.switch(ASSOCIATION, 2).state, "off")
            self.assertEqual(rig.node_diagnostic(2), "Retire")
            await rig.command("close_cover", 2)
            await self.burst_done(rig, rf_before + 1)
            self.assertEqual(rig.dongle.bursts()[-1]["counter"], 2)
            self.assertNotEqual(rig.dongle.bursts()[-1]["identity"], identity(1))

    async def test_replacement_preserves_device_entity_ids_and_changes_rf_epoch(self):
        async with self.system("--paired=1", "--enrollment", covers=1) as rig:
            before = rig.registry()
            device_id = rig.device(2).id
            await rig.press(SERVICE, on=False, node=2)
            await until(lambda: rig.switch(SERVICE, 2).state == "off", "old RF disabled")
            await rig.press(ASSOCIATION, node=2)
            await self.burst_done(rig, 2)
            await until(lambda: rig.node_diagnostic(2) == "Mouvement a confirmer", "replacement pending")
            new_rf = rig.dongle.bursts()[0]["identity"]
            self.assertNotEqual(new_rf, identity(1))
            await rig.command("open_cover", 1)  # a cover already exists but must remain inhibited
            await asyncio.sleep(0.3)
            self.assertEqual(len(rig.dongle.bursts()), 2)
            await self.confirm(rig)
            self.assertEqual(rig.registry(), before)
            self.assertEqual(rig.device(2).id, device_id)
            await rig.command("close_cover", 1)
            await self.burst_done(rig, 3)
            self.assertEqual((rig.dongle.bursts()[-1]["identity"], rig.dongle.bursts()[-1]["counter"]),
                             (new_rf, 2))

    async def test_cancel_replacement_preserves_old_identity_but_keeps_it_disabled(self):
        async with self.system("--paired=1", "--enrollment", covers=1) as rig:
            before = rig.registry()
            await rig.press(SERVICE, on=False, node=2)
            await until(lambda: rig.switch(SERVICE, 2).state == "off", "disabled shutter")
            await rig.press(ASSOCIATION, node=2)
            await self.burst_done(rig, 2)
            await until(lambda: rig.node_diagnostic(2) == "Mouvement a confirmer", "replacement pending")
            await rig.press(ASSOCIATION, on=False, node=2)
            await until(lambda: rig.switch(ASSOCIATION, 2).state == "off", "replacement cancelled")
            self.assertEqual(rig.switch(SERVICE, 2).state, "off")
            self.assertEqual(rig.registry(), before)
            await rig.command("close_cover", 1)
            await asyncio.sleep(0.3)
            self.assertEqual(len(rig.dongle.bursts()), 2)
            await rig.press(SERVICE, node=2)  # only explicit reactivation authorizes the old RF again
            await until(lambda: rig.switch(SERVICE, 2).state == "on", "explicit old RF reactivation")
            await rig.command("close_cover", 1)
            await self.burst_done(rig, 3)
            self.assertEqual((rig.dongle.bursts()[-1]["identity"], rig.dongle.bursts()[-1]["counter"]),
                             (identity(1), first_counter(1)))

    async def test_pending_association_restart_never_replays_and_native_devices_persist(self):
        with temporary_directory(prefix="x2d-lifecycle-journal-") as directory:
            args = ("--paired=0", "--enrollment", f"--journal={Path(directory) / 'journal.bin'}")
            async with self.system(*args, covers=0) as rig:
                await self.candidate(rig)
                before = rig.registry()
                devices = {n: rig.device(n).id for n in (NODE, 2)}
                await rig.dongle.restart(*args)
                await until(lambda: rig.dongle.from_dongle.count(
                    "0;255;3;0;14;Gateway startup complete.") == 2, "restarted gateway")
                await asyncio.sleep(0.4)
                self.assertEqual(len(rig.dongle.bursts()), 2)
                self.assertEqual(rig.registry(), before)
                self.assertEqual({n: rig.device(n).id for n in devices}, devices)
                await self.confirm(rig)
                await rig.dongle.disconnect()
                before = rig.registry()
                await rig.hass.config_entries.async_reload(rig.entry.entry_id)
                await rig.hass.async_block_till_done()
                self.assertEqual(rig.registry(), before)
                self.assertEqual({n: rig.device(n).id for n in devices}, devices)
                gateway = rig.hass.data[DOMAIN][MYSENSORS_GATEWAYS][rig.entry.entry_id]
                await until(lambda: not gateway.tasks.persistence.need_save, "native persisted nodes")
                self.assertEqual(sorted(map(int, rig.persisted())), [1, 2])
                self.assertEqual(rig.persisted()["2"]["children"]["1"]["values"],
                                 {"29": "0", "30": "0", "31": "1", "2": "1"})
                await rig.dongle.connect()
                await asyncio.sleep(0.4)
                self.assertEqual(len(rig.dongle.bursts()), 2)


    async def test_power_cut_during_first_enrollment_never_replays_the_second_phase(self):
        with temporary_directory(prefix="x2d-enrollment-power-cut-") as directory:
            args = ("--paired=0", "--enrollment", f"--journal={Path(directory) / 'journal.bin'}")
            async with self.system(*args, covers=0) as rig:
                await rig.dongle.hold()
                await rig.press(PAIR)
                await until(lambda: len(rig.dongle.bursts()) == 1, "first enrollment phase started")
                await until(lambda: rig.switch(ASSOCIATION, 2) is not None, "candidate native device")
                device_id = rig.device(2).id
                await rig.dongle.restart(*args)  # abrupt exit with phase two still outstanding
                await until(lambda: rig.dongle.from_dongle.count(
                    "0;255;3;0;14;Gateway startup complete.") == 2, "power-cut restart")
                await asyncio.sleep(0.4)
                self.assertEqual(len(rig.dongle.bursts()), 1)
                self.assertEqual(rig.dongle.ends(), [])
                self.assertEqual(rig.device(2).id, device_id)
                self.assertEqual(rig.covers(), {})
                # Only an explicit user choice can start the next attempt.
                await rig.press(RETRY, node=2)
                await self.burst_done(rig, 2)
                self.assertEqual([b["counter"] for b in rig.dongle.bursts()], [0, 2, 3])
                self.assertEqual(len({b["identity"] for b in rig.dongle.bursts()}), 1)

    async def test_disabled_enrollment_restores_manager_state_without_rf(self):
        async with self.system("--paired=0", covers=0) as rig:
            mark = len(rig.dongle.from_dongle)
            await rig.press(PAIR)
            await until(lambda: any("Association indisponible" in line
                                   for line in rig.dongle.from_dongle[mark:]), "enrollment refusal")
            await until(lambda: rig.switch(PAIR).state == "off", "authoritative manager OFF")
            await rig.press(INITIALIZE)  # an initialized journal is not reset a second time
            await until(lambda: rig.diagnostic() == "Deja initialise", "repeat initialization refusal")
            self.assertEqual(rig.dongle.bursts(), [])
            self.assertEqual(rig.covers(), {})

    async def test_legacy_initialize_and_recovery_never_emit_or_restore_old_endpoints(self):
        with temporary_directory(prefix="x2d-legacy-") as directory:
            path = Path(directory) / "legacy.bin"
            legacy_id = 0xBEEF01
            write_legacy_journal(path, legacy_id)
            args = ("--paired=0", "--enrollment", f"--journal={path}")
            async with self.system(*args, covers=0) as rig:
                self.assertIn("Initialisation", rig.diagnostic())
                self.assertEqual(rig.dongle.bursts(), [])
                await rig.press(INITIALIZE)
                await until(lambda: rig.diagnostic() == "Pret"
                            and rig.switch(INITIALIZE).state == "off", "legacy reset completed")
                self.assertEqual(rig.dongle.bursts(), [])
                await rig.dongle.restart(*args)
                await until(lambda: rig.dongle.from_dongle.count(
                    "0;255;3;0;14;Gateway startup complete.") == 2, "recovered reset")
                await asyncio.sleep(0.3)
                self.assertEqual(rig.dongle.bursts(), [])
                self.assertEqual(rig.covers(), {})
                await self.candidate(rig)
                self.assertNotIn(legacy_id, {b["identity"] for b in rig.dongle.bursts()})

    async def test_power_cut_during_legacy_reset_recovers_without_rf_or_identity_reuse(self):
        with temporary_directory(prefix="x2d-reset-recovery-") as directory:
            path = Path(directory) / "journal.bin"
            legacy_id = 0xA00001  # first allocator draw: recovery must retain its exclusion
            write_legacy_journal(path, legacy_id)
            process = await asyncio.create_subprocess_exec(
                str(SERVER), f"--journal={path}", "--prepare-legacy-reset",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            self.assertEqual((process.returncode, stdout, stderr), (0, b"", b""))
            args = ("--paired=0", "--enrollment", f"--journal={path}")
            async with self.system(*args, covers=0) as rig:
                await until(lambda: rig.diagnostic() == "Pret", "boot reset recovery finalized")
                self.assertEqual(rig.dongle.bursts(), [])
                self.assertEqual(rig.covers(), {})
                await self.candidate(rig)
                self.assertNotIn(legacy_id, {b["identity"] for b in rig.dongle.bursts()})
                await self.confirm(rig)
                before = rig.registry()
                await rig.dongle.restart(*args)
                await until(lambda: rig.dongle.from_dongle.count(
                    "0;255;3;0;14;Gateway startup complete.") == 2, "restart after recovery")
                await asyncio.sleep(0.3)
                self.assertEqual(len(rig.dongle.bursts()), 2)
                self.assertEqual(rig.registry(), before)

    async def test_corrupt_storage_is_never_formatted_or_emitted_by_manager_actions(self):
        with temporary_directory(prefix="x2d-corrupt-") as directory:
            path = Path(directory) / "journal.bin"
            write_legacy_journal(path, 0xBEEF01)
            damaged = bytearray(path.read_bytes())
            damaged[100] ^= 1  # committed body CRC now disagrees
            path.write_bytes(damaged)
            async with self.system("--paired=0", "--enrollment", f"--journal={path}", covers=0) as rig:
                self.assertEqual(rig.diagnostic(), "Erreur memoire")
                for child in (PAIR, INITIALIZE):
                    await rig.press(child)
                await asyncio.sleep(0.4)
                self.assertEqual(rig.dongle.bursts(), [])
                self.assertEqual(path.read_bytes(), damaged)
                self.assertEqual(rig.covers(), {})


class FrontendChecks(unittest.TestCase):
    """Structure of the installed frontend bundle only; nothing is rendered."""

    def test_assumed_state_keeps_open_and_close_available(self):
        directory = Path(importlib.import_module("hass_frontend").__file__).parent / "frontend_latest"
        pattern = re.compile(
            rb'attributes\.assumed_state\|\|!function\((\w)\)\{return void 0!==\1\.attributes\.current_position'
            rb'(?:&&null!==\1\.attributes\.current_position)?\?(?:100|0)===\1\.attributes\.current_position:'
            rb'"(open|closed)"===\1\.state\}')
        found = set()
        for path in directory.glob("*.js"):
            data = path.read_bytes()
            if b"assumed_state" in data:
                found.update(match[2].decode() for match in pattern.finditer(data))
        self.assertEqual(found, {"open", "closed"})


if __name__ == "__main__":
    unittest.main()
