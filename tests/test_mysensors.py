"""Native Home Assistant MySensors over a PTY, against the shared C++ adapter.

Nothing here simulates MySensors: the executable is lib/x2d-core's real
ha_x2d::mysensors::Gateway over simulated flash and radio, Home Assistant is the
installed 2026.9.4 core with its own mysensors integration (real config flow,
pymysensors 0.26.0 AsyncSerialGateway, real config entry, entity and device
registries), and the PTY bridge below only forwards bytes in small fragments and
observes them. RF is read from the server's stderr report, which decodes every
burst back to identity, action byte and counter. Simulated radio and flash do
not qualify real RF, USB timing or motors.

Prerequisites: build the mysensors_server CMake target (X2D_MYSENSORS_SERVER can
override build/x2d-core/mysensors_server) and install pymysensors==0.26.0 and
paho-mqtt==2.1.0 (Home Assistant's mysensors imports the mqtt integration).
"""

import asyncio
from collections import Counter, OrderedDict
import contextlib
import importlib
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import tty
import unittest

from homeassistant import loader  # first: the package aliases voluptuous before pymysensors imports it
from homeassistant.components.cover import CoverEntityFeature
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
SERVER = Path(os.environ.get("X2D_MYSENSORS_SERVER", ROOT / "build/x2d-core/mysensors_server"))

NODE, PAIR, CONFIRM, DIAGNOSTIC = 1, 17, 18, 19
V_STATUS, V_VAR1 = 2, 24
OPEN, CLOSE, STOP = 0x81, 0x82, 0x04
SLOTS = range(1, 17)
CUSTOMIZE = {"customize_glob": {"cover.x2d_*": {"assumed_state": True}}}  # the one accepted YAML rule


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

    def unique_id(self, child, value_type=V_STATUS):
        return f"{self.entry.entry_id}-{NODE}-{child}-{value_type}"

    def entity_id(self, domain, child, value_type=V_STATUS):
        return entity_registry.async_get(self.hass).async_get_entity_id(
            domain, "mysensors", self.unique_id(child, value_type))

    def cover(self, slot):
        return self.entity_id("cover", slot)

    def state(self, slot):
        entity_id = self.cover(slot)
        return self.hass.states.get(entity_id) if entity_id else None

    def covers(self):
        return {slot: self.state(slot) for slot in SLOTS if self.state(slot) is not None}

    def switch(self, child):
        return self.hass.states.get(self.entity_id("switch", child))

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

    async def press(self, child, on=True):
        await self.hass.services.async_call(
            "switch", "turn_on" if on else "turn_off", {"entity_id": self.entity_id("switch", child)},
            blocking=True)

    async def settled(self, covers=len(SLOTS)):
        await until(lambda: len(self.covers()) == covers and self.diagnostic() is not None
                    and self.switch(PAIR) is not None and self.switch(CONFIRM) is not None,
                    f"{covers} covers, both pairing switches and the diagnostic")
        await self.hass.async_block_till_done()

    async def remove_device(self):
        """The UI's Delete device: the integration hook, then the registry."""
        await asyncio.sleep(0.3)  # entity updates are debounced; a pending one would find the node gone
        registry = device_registry.async_get(self.hass)
        device, = device_registry.async_entries_for_config_entry(registry, self.entry.entry_id)
        component = importlib.import_module("homeassistant.components.mysensors")
        self.test.assertTrue(await component.async_remove_config_entry_device(self.hass, self.entry, device))
        registry.async_remove_device(device.id)
        await self.hass.async_block_till_done()

    def persisted(self):
        return json.loads(Path(self.hass.config.path(f"mysensors_{self.entry.entry_id}.json")).read_text())


class NativeMySensorsChecks(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if not SERVER.is_file() or not os.access(SERVER, os.X_OK):
            raise AssertionError(
                f"mysensors_server executable missing: {SERVER}. Build the mysensors_server CMake "
                "target (devenv test does) or set X2D_MYSENSORS_SERVER.")

    @contextlib.asynccontextmanager
    async def system(self, *server_args, customize=False, covers=len(SLOTS)):
        """The real config flow against the PTY, then the host opens the port."""
        dongle = Dongle()
        try:
            with tempfile.TemporaryDirectory(prefix="ha-x2d-mysensors-") as directory:
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
                             {"cover": 16, "switch": 2, "sensor": 2})  # + diagnostic and node battery
            self.assertEqual(sorted(e.unique_id for e in entities if e.domain == "cover"),
                             sorted(rig.unique_id(slot) for slot in SLOTS))
            for slot, state in rig.covers().items():
                with self.subTest(slot=slot):
                    attributes = state.attributes
                    self.assertEqual(state.state, "open")  # unknown position is shown as the default
                    self.assertEqual(attributes["supported_features"],
                                     CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP)
                    self.assertNotIn("current_position", attributes)
                    self.assertNotIn("current_tilt_position", attributes)
                    self.assertNotIn("assumed_state", attributes)  # supplied only by the YAML rule
                    self.assertEqual({k: v for k, v in attributes.items() if k.startswith("V_")},
                                     {"V_UP": "off", "V_DOWN": "off", "V_STOP": "on", "V_STATUS": "on"})
                    self.assertEqual(attributes["node_id"], NODE)
                    self.assertEqual(attributes["child_id"], slot)
            self.assertIn("pos_unknown", rig.diagnostic())
            self.assertEqual(rig.switch(PAIR).state, "off")
            self.assertEqual(rig.switch(CONFIRM).state, "off")
            self.assertEqual(rig.dongle.bursts(), [])  # discovery never reaches the radio
            self.assertIn(f"{NODE};255;0;0;17;2.3.2", rig.dongle.from_dongle)

    async def test_one_global_assumed_state_rule_reaches_every_cover(self):
        async with self.system(customize=True) as rig:
            for slot, state in rig.covers().items():
                with self.subTest(slot=slot):
                    self.assertTrue(state.entity_id.startswith("cover.x2d_"), state.entity_id)
                    self.assertIs(state.attributes["assumed_state"], True)
            for child in (PAIR, CONFIRM):
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
                        ack = f"{NODE};{slot};1;1;{sub};1"  # the request with its ack flag, echoed
                        self.assertIn(ack, rig.dongle.from_host)
                        await until(lambda: ack in rig.dongle.from_dongle, f"the echo {ack}")
                        diagnostic = f"{NODE};{DIAGNOSTIC};1;0;{V_VAR1};{slot}:"
                        final = f"{diagnostic}pos_unknown" if action == STOP else f"{diagnostic}emitted"
                        await until(lambda: final in rig.dongle.from_dongle[mark:], f"the diagnostic {final}")
                        wire = rig.dongle.from_dongle[mark:]  # receipt first, then the terminal outcome
                        self.assertLess(wire.index(f"{diagnostic}accepted"), wire.index(final))
                        await until(lambda: rig.state(slot).state == resulting, f"{slot} {resulting}")
                        self.assertEqual(rig.state(slot).attributes["V_STOP"], "on")
                        self.assertNotIn("current_position", rig.state(slot).attributes)
                        sent.append(burst["identity"])
                await until(lambda: rig.diagnostic() == f"{slot}:pos_unknown", "the STOP diagnostic")
            self.assertEqual(set(sent), {identity(16)})
            self.assertEqual({b["identity"] for b in rig.dongle.bursts()}, {identity(3), identity(16)})
            states = {state for entity_id, state in rig.history if entity_id.startswith("cover.")}
            self.assertLessEqual(states, {"open", "closed"})  # no echo ever renders as motion

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
            await until(lambda: rig.diagnostic() == "5:pos_unknown", "the position diagnostic")
            self.assertEqual(rig.state(5).state, "open")

    async def test_momentary_controls_refuse_off_and_return_off_after_reconnect(self):
        async with self.system() as rig:
            for child in (PAIR, CONFIRM):
                with self.subTest(child=child):
                    await rig.press(child, on=True)  # association is not authorized in this build
                    await until(lambda: rig.diagnostic() == "association_disabled", "the refusal")
                    await until(lambda: rig.switch(child).state == "off", "the momentary return to off")
                    await rig.press(child, on=False)
                    await asyncio.sleep(0.2)
                    self.assertEqual(rig.switch(child).state, "off")
            self.assertEqual(rig.dongle.bursts(), [])
            await rig.dongle.disconnect()
            await rig.press(PAIR, on=True)  # lost with the link: the server was not listening
            await asyncio.sleep(0.2)
            await rig.dongle.connect()
            await until(lambda: rig.dongle.from_dongle.count("0;255;3;0;14;Gateway startup complete.") == 2,
                        "the second gateway-ready")
            await rig.hass.async_block_till_done()
            await asyncio.sleep(0.3)
            self.assertEqual({rig.switch(PAIR).state, rig.switch(CONFIRM).state}, {"off"})
            self.assertEqual(len(rig.covers()), 16)
            self.assertEqual(rig.dongle.bursts(), [])

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
            line = f"{NODE};6;1;1;30;1"
            await until(lambda: line in rig.dongle.from_host, "the lost command on the wire")
            await asyncio.sleep(0.3)
            await rig.dongle.connect()
            await until(lambda: rig.dongle.from_dongle.count(f"{NODE};255;0;0;17;2.3.2") == 2, "re-announce")
            await rig.hass.async_block_till_done()
            await asyncio.sleep(0.5)
            self.assertEqual(len(rig.dongle.bursts()), 1)  # no replay of either command
            self.assertEqual({rig.state(4).state, rig.state(6).state}, {"open"})
            self.assertEqual(rig.diagnostic(), "4:usb_lost,pos_unknown")  # the cut movement

    async def test_reload_keeps_every_entity_identity_and_does_not_transmit(self):
        async with self.system() as rig:
            before = rig.registry()
            states = {slot: state.state for slot, state in rig.covers().items()}
            self.assertEqual(len(before), 20)
            await rig.dongle.disconnect()
            await rig.hass.config_entries.async_reload(rig.entry.entry_id)
            await rig.hass.async_block_till_done()
            # Restored from pymysensors' JSON persistence before the dongle says anything.
            self.assertEqual(rig.registry(), before)
            self.assertEqual({slot: state.state for slot, state in rig.covers().items()}, states)
            persisted = rig.persisted()
            self.assertEqual(sorted(map(int, persisted[str(NODE)]["children"])), list(range(1, 20)))
            self.assertEqual(persisted[str(NODE)]["children"]["3"]["values"],
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
        with tempfile.TemporaryDirectory(prefix="x2d-journal-") as directory:
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

    async def test_native_mysensors_cannot_tell_that_the_journal_was_wiped(self):
        """Documented limitation, not a protection: no MySensors field carries a generation.

        Wiping the dongle's journal without cleaning Home Assistant first leaves
        its stored node, children and entity ids in place, and an old entity drives
        whichever shutter now occupies its slot. Nothing detects or fences this;
        the only supported order is test_cleanup_precedes_a_journal_wipe.
        """
        with tempfile.TemporaryDirectory(prefix="x2d-journal-") as directory:
            old, new = (str(Path(directory) / name) for name in ("old.bin", "new.bin"))
            async with self.system(f"--journal={old}") as rig:
                before = rig.registry()
                await rig.dongle.restart(f"--journal={new}", "--generation=FEDCBA9876543210",
                                         "--identity-base=B00000")
                self.assertEqual(rig.dongle.ready[-1], (16, "FEDCBA9876543210"))
                await until(lambda: rig.dongle.from_dongle.count("0;255;3;0;14;Gateway startup complete.") == 2,
                            "the new gateway-ready")
                await rig.hass.async_block_till_done()
                self.assertEqual(rig.registry(), before)  # Home Assistant saw nothing change
                self.assertFalse([line for line in rig.dongle.from_dongle if "FEDCBA9876543210" in line])
                await rig.command("open_cover", 3)
                await self.burst_done(rig, 1)
                self.assertEqual(rig.dongle.bursts()[0]["identity"], identity(3, 0xB00000))

    async def test_cleanup_precedes_a_journal_wipe(self):
        """The supported order: delete the device in Home Assistant, then reset the dongle."""
        with tempfile.TemporaryDirectory(prefix="x2d-journal-") as directory:
            old, new = (str(Path(directory) / name) for name in ("old.bin", "new.bin"))
            async with self.system(f"--journal={old}") as rig:
                before = rig.registry()
                await rig.remove_device()
                self.assertEqual(rig.registry(), {})
                await rig.dongle.restart(f"--journal={new}", "--generation=FEDCBA9876543210",
                                         "--identity-base=B00000")
                await rig.settled()  # everything is announced and discovered as new
                self.assertEqual(set(rig.registry().values()), set(before.values()))
                await rig.command("open_cover", 3)
                await self.burst_done(rig, 1)
                burst = rig.dongle.bursts()[0]
                self.assertEqual((burst["identity"], burst["counter"]), (identity(3, 0xB00000), first_counter(3)))

    async def test_disabled_or_faulty_radio_never_claims_a_position(self):
        async with self.system() as rig:
            for server_args, outcome in ((("--no-tx",), "3:tx_off,pos_unknown"),
                                         (("--fault=start",), "3:radio_start_failed"),
                                         (("--fault=poll",), "3:rf_fault,pos_unknown")):
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

    async def test_pairing_switches_drive_association_and_the_new_cover_appears_once_confirmed(self):
        async with self.system("--paired=0", "--enrollment", "--authorize=1:5A:0", covers=0) as rig:
            self.assertEqual(rig.covers(), {})
            await rig.press(PAIR)
            await self.burst_done(rig, 2)  # the two enrollment bursts, about two seconds apart
            await until(lambda: rig.diagnostic() == "1:awaiting_confirmation", "the awaiting diagnostic")
            await until(lambda: rig.switch(PAIR).state == "off", "the momentary return to off")
            self.assertEqual(rig.covers(), {})  # nothing is a shutter until a human confirms
            await rig.press(CONFIRM)
            await until(lambda: len(rig.covers()) == 1, "the confirmed cover")
            await until(lambda: rig.diagnostic() == "1:paired,pos_unknown", "the paired diagnostic")
            self.assertEqual(rig.state(1).state, "open")
            await rig.command("open_cover", 1)
            await self.burst_done(rig, 3)
            command = rig.dongle.bursts()[2]
            self.assertEqual((command["action"], command["counter"], command["identity"] & 0xFF),
                             (OPEN, 2, 0x5A))  # the enrollment reserved counters 0 and 1


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
