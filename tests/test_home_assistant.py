"""Load the distributable component in real HA Core against a simulated USB key."""

import asyncio
from collections import deque
import contextlib
from copy import deepcopy
import importlib
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

from serialx import SerialPortInfo

from homeassistant import loader
from homeassistant.components.usb.models import USBDevice
from homeassistant.components.usb.utils import usb_device_matches_matcher
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry, frame, translation
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from test_gateway import IDENTITY, GENERATION, SimulatedGateway, SimulatedTCPGateway, wait_requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_component import build


class PackagingChecks(unittest.TestCase):
    def test_release_layout_client_and_reproducibility(self):
        hacs = json.loads((ROOT / "hacs.json").read_text())
        self.assertTrue(hacs["zip_release"])
        self.assertTrue(hacs["hide_default_branch"])
        self.assertTrue(hacs["render_readme"])
        self.assertEqual(hacs["homeassistant"], "2026.9.4")
        with tempfile.TemporaryDirectory(prefix="ha-x2d-package-check-") as directory:
            output = Path(directory)
            manual = build(output)
            release = build(output, hacs=True)
            self.assertEqual(release.name, hacs["filename"])
            self.assertEqual(release.read_bytes(), build(output / "repeat", hacs=True).read_bytes())
            with ZipFile(manual) as old, ZipFile(release) as bundle:
                names = bundle.namelist()
                self.assertEqual(names, sorted(names))
                self.assertEqual(old.namelist(), [f"custom_components/x2d/{name}" for name in names])
                for entry in bundle.infolist():
                    self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
                    self.assertEqual(entry.external_attr >> 16, 0o100644)
                    self.assertEqual(bundle.read(entry.filename), old.read(f"custom_components/x2d/{entry.filename}"))
                    self.assertNotIn("__pycache__", entry.filename)
                    self.assertFalse(entry.filename.endswith(".pyc"))
                self.assertIn("cover.py", names)
                self.assertIn("translations/fr.json", names)
                self.assertNotIn("_client/__main__.py", names)
                for icon, size in (("brand/icon.png", 256), ("brand/icon@2x.png", 512)):
                    self.assertIn(icon, names)
                    png = bundle.read(icon)
                    self.assertEqual(png, (ROOT / "custom_components/x2d" / icon).read_bytes())
                    width, height, _, color_type = struct.unpack(">IIBB", png[16:26])
                    self.assertEqual((png[:8], width, height, color_type),
                                     (b"\x89PNG\r\n\x1a\n", size, size, 6), icon)
                for source in (ROOT / "python/src/x2d_gateway").rglob("*.py"):
                    if "__pycache__" not in source.parts and source.name != "__main__.py":
                        name = source.relative_to(ROOT / "python/src/x2d_gateway").as_posix()
                        self.assertEqual(bundle.read(f"_client/{name}"), source.read_bytes())
                manifest = json.loads(bundle.read("manifest.json"))
                self.assertEqual(manifest, json.loads((ROOT / "custom_components/x2d/manifest.json").read_text()))
                for key in ("domain", "documentation", "issue_tracker", "codeowners", "name", "version"):
                    self.assertTrue(manifest[key], key)
                self.assertEqual(manifest["name"], hacs["name"])
                self.assertEqual(manual.name, f"x2d-{manifest['version']}.zip")

    def test_translations_separate_recorded_shutters_from_new_pairing(self):
        component = ROOT / "custom_components/x2d"
        for language, pairing, nothing in (("en", "pairing", "nothing is paired"),
                                           ("fr", "association", "rien n’est associé")):
            localized = json.loads((component / f"translations/{language}.json").read_text())
            shutter = localized["config_subentries"]["shutter"]
            new, recorded = shutter["step"]["user"], shutter["step"]["recorded"]
            self.assertIn(pairing, new["description"], language)
            self.assertNotIn("{recorded}", new["description"], language)
            self.assertIn("{recorded}", recorded["description"], language)
            self.assertIn(nothing, recorded["description"], language)
            self.assertNotEqual(new["title"], recorded["title"], language)
            self.assertEqual(set(recorded["data"]), {"name", "shutter_id"}, language)
            for reason in ("enrollment_unavailable", "no_slots", "cannot_connect"):
                self.assertTrue(shutter["abort"][reason], (language, reason))


@contextlib.asynccontextmanager
async def home_assistant(*, tcp=False):
    """Real HA Core loading the packaged component against a PTY or TCP peer."""
    with tempfile.TemporaryDirectory(prefix="ha-x2d-check-") as directory:
        config = Path(directory)
        with ZipFile(build(config, hacs=True)) as bundle:
            bundle.extractall(config / "custom_components/x2d")
        sys.path.insert(0, directory)
        hass = HomeAssistant(directory)
        hass.config.skip_pip = True
        hass.config.components.update({"http", "websocket_api"})
        loader.async_setup(hass)
        frame.async_setup(hass)
        translation.async_setup(hass)
        device_registry.async_setup(hass)
        hass.config_entries = ConfigEntries(hass, {})
        await hass.config_entries.async_initialize()
        await device_registry.async_load(hass)
        await entity_registry.async_load(hass)
        peer = await SimulatedTCPGateway.start() if tcp else SimulatedGateway()
        try:
            with patch("homeassistant.components.usb.async_setup", AsyncMock(return_value=True)):
                yield hass, peer
        finally:
            await hass.async_stop(force=True)
            if tcp:
                await peer.aclose()
            else:
                peer.close()
            sys.path.remove(directory)
            for name in list(sys.modules):
                if name == "custom_components" or name.startswith("custom_components."):
                    del sys.modules[name]


COMMANDS_ONLY = ["status", "shutters", "command"]
OLD_TITLE = f"X2D USB {IDENTITY[-6:]}"  # 0.3.0 default entry title


def paired(slot, last_command=None):
    return {"shutter_id": slot, "state": "paired", "last_command": last_command}


class RecordedShutterChecks(unittest.IsolatedAsyncioTestCase):
    """Commands-only recovery of shutters already recorded by the gateway."""

    ops = frozenset({"provision", "pair", "confirm"})

    async def add_gateway(self, hass, peer):
        flow = await hass.config_entries.flow.async_init("x2d", context={"source": "user"})
        flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {"device": peer.device})
        self.assertEqual(flow["type"], "create_entry", flow)
        await hass.async_block_till_done()
        return flow["result"]

    async def offer(self, hass, entry):
        return await hass.config_entries.subentries.async_init(
            (entry.entry_id, "shutter"), context={"source": "user"})

    async def adopt(self, hass, flow, action, **form):
        manager = hass.config_entries.subentries
        result = await manager.async_configure(flow["flow_id"], form)
        self.assertEqual(result["step_id"], "test", result)
        result = await manager.async_configure(result["flow_id"], {"action": action})
        self.assertEqual(result["step_id"], "test_confirm", result)
        result = await manager.async_configure(result["flow_id"], {"test_observed": True})
        self.assertEqual(result["type"], "create_entry", result)
        await hass.async_block_till_done()

    @staticmethod
    def choices(flow):
        schema = flow["data_schema"].schema
        key = next((k for k in schema if k.schema == "shutter_id"), None)
        return None if key is None else schema[key].container

    async def test_network_configuration_reconnect_identity_and_diagnostics(self):
        async with home_assistant(tcp=True) as (hass, peer):
            peer.info["capabilities"] = COMMANDS_ONLY
            peer.shutters = {1: paired(1, "close")}
            with patch("homeassistant.components.usb.get_serial_by_id", side_effect=AssertionError("URL resolved as USB")):
                entry = await self.add_gateway(hass, peer)
                self.assertEqual(dict(entry.data), {"device": peer.device})
                self.assertEqual(entry.title, f"X2D Gateway {IDENTITY[-6:]}")
                await self.adopt(hass, await self.offer(hass, entry), "stop", name="Network shutter")
                coordinator = entry.runtime_data
                registry = entity_registry.async_get(hass)
                entities = entity_registry.async_entries_for_config_entry(registry, entry.entry_id)
                cover = next(e for e in entities if e.domain == "cover")
                identities = {e.entity_id: e.unique_id for e in entities}
                subentries = dict(entry.subentries)
                self.assertEqual(cover.unique_id, f"{IDENTITY}_{GENERATION}_1")

                # Losing the link after ACK leaves the command uncertain; even
                # the command's final diagnostic refresh must never replay it.
                peer.auto_tx = False
                start = len(peer.requests)
                opening = asyncio.create_task(hass.services.async_call(
                    "cover", "open_cover", {"entity_id": cover.entity_id}, blocking=True))
                await wait_requests(peer, start + 1)
                self.assertEqual(peer.requests[-1]["args"], {"shutter_id": 1, "action": "open"})
                peer.info["device_id"] = "FFFFFFFFFFFFFFFF"
                peer.unplug()
                with self.assertRaisesRegex(Exception, "command_uncertain"):
                    await opening
                self.assertFalse(coordinator.last_update_success)
                self.assertIsNone(coordinator.gateway)
                self.assertEqual(hass.states.get(cover.entity_id).state, "unavailable")
                await coordinator.async_refresh()
                self.assertFalse(coordinator.last_update_success)  # wrong identity still rejected

                peer.info.update(device_id=IDENTITY, session="BBBBBBBBBBBBBBBB", firmware="portable-test")
                peer.info["capabilities"] = ["status", "shutters"]
                await coordinator.async_refresh()
                self.assertTrue(coordinator.last_update_success)
                self.assertEqual(coordinator.info, peer.info)
                self.assertFalse(coordinator.ready(1, GENERATION))  # capabilities from the new hello
                await coordinator.gateway.close()
                peer.info["capabilities"] = COMMANDS_ONLY
                await coordinator.async_refresh()
                self.assertTrue(coordinator.ready(1, GENERATION))
                self.assertEqual(hass.states.get(cover.entity_id).state, "unknown")
                self.assertEqual(dict(entry.subentries), subentries)
                self.assertEqual({e.entity_id: e.unique_id for e in
                                  entity_registry.async_entries_for_config_entry(registry, entry.entry_id)}, identities)
                self.assertEqual([r["op"] for r in peer.requests[start:]].count("command"), 1)
                self.assertFalse(any(r["op"] in self.ops for r in peer.requests))

                diagnostics = importlib.import_module("custom_components.x2d.diagnostics")
                exported = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
                self.assertNotIn("hardware", exported)
                self.assertEqual(exported["info"]["firmware"], "portable-test")
                self.assertEqual(exported["info"]["capabilities"], COMMANDS_ONLY)
                for private in (IDENTITY, GENERATION, peer.info["session"], peer.device, "RP2040"):
                    self.assertNotIn(private, json.dumps(exported))
                peer.status["storage"]["generation"] = "3333333344444444"
                await coordinator.async_refresh()
                self.assertFalse(coordinator.ready(1, GENERATION))
                self.assertEqual(hass.states.get(cover.entity_id).state, "unavailable")
                self.assertTrue(await hass.config_entries.async_unload(entry.entry_id))
                self.assertIsNone(coordinator.gateway)

    async def test_reconfigure_local_and_network_preserves_shutter_references(self):
        async with home_assistant() as (hass, peer):
            peer.info["capabilities"] = COMMANDS_ONLY
            peer.shutters = {1: paired(1)}
            # The local path is resolved before hello and the resolved value is saved.
            with patch("homeassistant.components.usb.get_serial_by_id", return_value=peer.device) as resolve:
                flow = await hass.config_entries.flow.async_init("x2d", context={"source": "user"})
                result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"device": "/dev/ttyACM-test"})
                resolve.assert_called_once_with("/dev/ttyACM-test")
            self.assertEqual(result["type"], "create_entry", result)
            entry = result["result"]
            await hass.async_block_till_done()
            await self.adopt(hass, await self.offer(hass, entry), "stop", name="Living room")
            hass.config_entries.async_update_entry(entry, title="My gateway")
            await hass.async_block_till_done()
            registry = entity_registry.async_get(hass)
            identities = {e.entity_id: e.unique_id for e in
                          entity_registry.async_entries_for_config_entry(registry, entry.entry_id)}
            subentries = dict(entry.subentries)
            network = await SimulatedTCPGateway.start()
            network.shutters = deepcopy(peer.shutters)
            network.info["capabilities"] = COMMANDS_ONLY
            try:
                flow = await hass.config_entries.flow.async_init(
                    "x2d", context={"source": "reconfigure", "entry_id": entry.entry_id})
                self.assertEqual(flow["step_id"], "reconfigure")
                with patch("homeassistant.components.usb.get_serial_by_id", side_effect=AssertionError("URL resolved as USB")):
                    result = await hass.config_entries.flow.async_configure(
                        flow["flow_id"], {"device": "socket://127.0.0.1:not-a-port"})
                    self.assertEqual(result["errors"], {"base": "cannot_connect"})
                    network.info["device_id"] = "FFFFFFFFFFFFFFFF"
                    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"device": network.device})
                    self.assertEqual(result["errors"], {"base": "invalid_gateway"})
                    self.assertEqual(entry.data["device"], peer.device)
                    self.assertTrue(entry.runtime_data.ready(1, GENERATION))
                    network.info["device_id"] = IDENTITY
                    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"device": network.device})
                    self.assertEqual(result["reason"], "reconfigure_successful")
                    await hass.async_block_till_done()
                self.assertEqual((entry.title, entry.unique_id, entry.data["device"]), ("My gateway", IDENTITY, network.device))
                self.assertEqual(dict(entry.subentries), subentries)
                self.assertTrue(entry.runtime_data.ready(1, GENERATION))
                self.assertEqual({e.entity_id: e.unique_id for e in
                                  entity_registry.async_entries_for_config_entry(registry, entry.entry_id)}, identities)
                self.assertFalse(any(r["op"] in self.ops | {"command"} for r in network.requests))
                self.assertEqual(entry.state, ConfigEntryState.LOADED)

                # Returning to a local path uses the same resolver and identity check.
                flow = await hass.config_entries.flow.async_init(
                    "x2d", context={"source": "reconfigure", "entry_id": entry.entry_id})
                with patch("homeassistant.components.usb.get_serial_by_id", return_value=peer.device) as resolve:
                    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"device": "/dev/ttyACM-test"})
                    resolve.assert_called_once_with("/dev/ttyACM-test")
                self.assertEqual(result["reason"], "reconfigure_successful")
                await hass.async_block_till_done()
                self.assertEqual(entry.data["device"], peer.device)
                self.assertTrue(entry.runtime_data.ready(1, GENERATION))
                self.assertEqual({e.entity_id: e.unique_id for e in
                                  entity_registry.async_entries_for_config_entry(registry, entry.entry_id)}, identities)
            finally:
                await network.aclose()

    async def test_one_recorded_shutter_is_selected_with_a_name_only_form(self):
        async with home_assistant() as (hass, peer):
            peer.info["capabilities"] = COMMANDS_ONLY
            peer.shutters = {3: {"shutter_id": 3, "state": "pending", "last_command": None},
                             5: paired(5, "open")}
            entry = await self.add_gateway(hass, peer)
            self.assertEqual(entry.title, f"X2D Gateway {IDENTITY[-6:]}")
            start = len(peer.requests)
            for language, label in (("en", "Recorded shutter 5 (last command: open)"),
                                    ("fr", "Volet enregistré 5 (dernière commande : montée)")):
                hass.config.language = language
                flow = await self.offer(hass, entry)
                self.assertEqual(flow["step_id"], "recorded", flow)
                self.assertIsNone(self.choices(flow))
                self.assertEqual({k.schema for k in flow["data_schema"].schema}, {"name"})
                self.assertEqual(flow["description_placeholders"], {"recorded": f"- {label}"})
                hass.config_entries.subentries.async_abort(flow["flow_id"])
            # Pending slot 3 is not recorded as paired and is never offered.
            hass.config.language = "en"
            stale, other = await self.offer(hass, entry), await self.offer(hass, entry)
            await self.adopt(hass, other, "open", name="Kitchen")
            self.assertEqual([r["args"] for r in peer.requests[start:] if r["op"] == "command"],
                             [{"shutter_id": 5, "action": "open"}])
            # A name-only form cannot adopt a shutter another flow already claimed.
            result = await hass.config_entries.subentries.async_configure(stale["flow_id"], {"name": "Again"})
            self.assertEqual(result["reason"], "enrollment_unavailable")
            result = await self.offer(hass, entry)
            self.assertEqual(result["reason"], "enrollment_unavailable")
            self.assertFalse(any(r["op"] in self.ops for r in peer.requests))
            self.assertEqual(len(entry.subentries), 1)
            # "no_slots" stays reserved for a gateway whose 16 slots are really used.
            flow_module = importlib.import_module("custom_components.x2d.config_flow")
            with patch.object(flow_module.ShutterFlow, "_used", return_value=set(range(1, 17))):
                result = await self.offer(hass, entry)
            self.assertEqual(result["reason"], "no_slots")

    async def test_name_only_offer_is_void_after_journal_replacement(self):
        async with home_assistant() as (hass, peer):
            peer.info["capabilities"] = COMMANDS_ONLY
            peer.shutters = {5: paired(5)}
            entry = await self.add_gateway(hass, peer)
            flow = await self.offer(hass, entry)
            peer.status["storage"]["generation"] = "3333333344444444"
            start = len(peer.requests)
            result = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"name": "Kitchen"})
            self.assertEqual(result.get("reason"), "generation_changed", result)
            self.assertFalse(any(r["op"] in {"command", *self.ops} for r in peer.requests[start:]))
            peer.unplug()
            result = await self.offer(hass, entry)
            self.assertEqual(result["reason"], "cannot_connect")

    async def test_multiple_choice_offer_is_void_after_journal_replacement(self):
        async with home_assistant() as (hass, peer):
            peer.info["capabilities"] = COMMANDS_ONLY
            peer.shutters = {1: paired(1), 5: paired(5)}
            entry = await self.add_gateway(hass, peer)
            flow = await self.offer(hass, entry)
            self.assertEqual(set(self.choices(flow)), {1, 5})
            peer.status["storage"]["generation"] = "3333333344444444"
            start = len(peer.requests)
            result = await hass.config_entries.subentries.async_configure(
                flow["flow_id"], {"name": "Kitchen", "shutter_id": 5})
            self.assertEqual(result.get("reason"), "generation_changed", result)
            self.assertFalse(any(r["op"] in {"command", *self.ops} for r in peer.requests[start:]))
            self.assertFalse(entry.subentries)

    async def test_two_recorded_shutters_route_independently_under_one_gateway(self):
        async with home_assistant() as (hass, peer):
            peer.info["capabilities"] = COMMANDS_ONLY
            peer.shutters = {1: paired(1, "close"), 3: {"shutter_id": 3, "state": "pending", "last_command": None},
                             5: paired(5, "open"), 9: paired(9, "stop")}
            entry = await self.add_gateway(hass, peer)
            start = len(peer.requests)
            flow = await self.offer(hass, entry)
            self.assertEqual(flow["step_id"], "recorded", flow)
            options = self.choices(flow)
            self.assertEqual(set(options), {1, 5, 9})  # recorded paired shutters, not slots 1-16
            self.assertEqual(options[9], "Recorded shutter 9 (last command: stop)")
            await self.adopt(hass, flow, "open", name="Kitchen", shutter_id=5)
            flow = await self.offer(hass, entry)
            self.assertEqual(set(self.choices(flow)), {1, 9})
            await self.adopt(hass, flow, "stop", name="Garage", shutter_id=9)
            flow = await self.offer(hass, entry)
            self.assertIsNone(self.choices(flow))  # one left: name only
            await self.adopt(hass, flow, "close", name="Living room")
            result = await self.offer(hass, entry)
            self.assertEqual(result["reason"], "enrollment_unavailable")
            self.assertEqual([r["args"] for r in peer.requests[start:] if r["op"] == "command"],
                             [{"shutter_id": 5, "action": "open"}, {"shutter_id": 9, "action": "stop"},
                              {"shutter_id": 1, "action": "close"}])
            self.assertFalse(any(r["op"] in self.ops for r in peer.requests))

            dev_reg, ent_reg = device_registry.async_get(hass), entity_registry.async_get(hass)
            gateway = dev_reg.async_get_device_by_identifier(("x2d", IDENTITY), entry.entry_id)
            self.assertEqual((gateway.name, gateway.manufacturer, gateway.model, gateway.sw_version),
                             (entry.title, "ha-x2d", "X2D Gateway", "0.3.0"))
            self.assertIsNone(gateway.hw_version)
            entities = entity_registry.async_entries_for_config_entry(ent_reg, entry.entry_id)
            covers = {e.unique_id: e for e in entities if e.domain == "cover"}
            self.assertEqual(set(covers), {f"{IDENTITY}_{GENERATION}_{slot}" for slot in (1, 5, 9)})
            for entity in entities:
                device = dev_reg.async_get(entity.device_id)
                if entity.domain == "cover":
                    self.assertEqual(device.via_device_id, gateway.id)
                    self.assertEqual(device.model, "X2D shutter controller")
                else:
                    self.assertEqual(device.id, gateway.id)
            for device in device_registry.async_entries_for_config_entry(dev_reg, entry.entry_id):
                self.assertNotIn("RP2040", f"{device.model} {device.hw_version} {device.name}")
                self.assertNotIn("CC1101", f"{device.model} {device.hw_version} {device.name}")
            diagnostics = importlib.import_module("custom_components.x2d.diagnostics")
            self.assertNotIn("hardware", await diagnostics.async_get_config_entry_diagnostics(hass, entry))

            async def send(slot, service):
                start = len(peer.requests)
                await hass.services.async_call("cover", service, {
                    "entity_id": covers[f"{IDENTITY}_{GENERATION}_{slot}"].entity_id}, blocking=True)
                return [r["args"] for r in peer.requests[start:] if r["op"] == "command"]
            self.assertEqual(await send(1, "open_cover"), [{"shutter_id": 1, "action": "open"}])
            self.assertEqual(await send(5, "close_cover"), [{"shutter_id": 5, "action": "close"}])
            self.assertEqual(await send(9, "stop_cover"), [{"shutter_id": 9, "action": "stop"}])
            self.assertEqual({s: peer.shutters[s]["last_command"] for s in peer.shutters},
                             {1: "open", 3: None, 5: "close", 9: "stop"})

            # Reload/restore is read-only: same entities and devices, no replay.
            before = {e.unique_id: (e.entity_id, e.device_id) for e in entities}
            start = len(peer.requests)
            self.assertTrue(await hass.config_entries.async_reload(entry.entry_id))
            await hass.async_block_till_done()
            self.assertEqual({r["op"] for r in peer.requests[start:]} - {"hello", "status", "shutters"}, set())
            self.assertEqual(before, {e.unique_id: (e.entity_id, e.device_id) for e in
                                      entity_registry.async_entries_for_config_entry(ent_reg, entry.entry_id)})
            for slot, intent in ((1, "open"), (5, "close"), (9, "stop")):
                state = hass.states.get(covers[f"{IDENTITY}_{GENERATION}_{slot}"].entity_id)
                self.assertEqual((state.state, state.attributes["last_command_intent"]), ("unknown", intent))

            # A new journal generation invalidates every old reference, with no RF.
            coordinator = entry.runtime_data
            peer.status["storage"]["generation"] = "3333333344444444"
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            start = len(peer.requests)
            for slot in (1, 5, 9):
                self.assertFalse(coordinator.ready(slot, GENERATION))
                self.assertEqual(hass.states.get(covers[f"{IDENTITY}_{GENERATION}_{slot}"].entity_id).state,
                                 "unavailable")
                with self.assertRaisesRegex(Exception, "shutter_unavailable"):
                    await coordinator.async_command(slot, GENERATION, "stop")
            self.assertFalse(any(r["op"] == "command" for r in peer.requests[start:]))
            peer.status["storage"]["generation"] = GENERATION
            await coordinator.async_refresh()
            self.assertTrue(all(coordinator.ready(slot, GENERATION) for slot in (1, 5, 9)))

    async def test_enrollment_capable_gateway_keeps_the_slot_flow(self):
        async with home_assistant() as (hass, peer):
            entry = await self.add_gateway(hass, peer)
            flow = await self.offer(hass, entry)
            self.assertEqual(flow["step_id"], "user", flow)
            self.assertEqual(list(self.choices(flow)), list(range(1, 17)))
            self.assertIsNone(flow.get("description_placeholders"))
            self.assertFalse(any(r["op"] in self.ops for r in peer.requests))

    async def test_030_state_is_upgraded_without_changing_ids_or_user_names(self):
        subentry = {"subentry_id": "01SUBENTRYVOLETC00000000001", "subentry_type": "shutter",
                    "title": "Volet C", "unique_id": f"{GENERATION}_1",
                    "data": {"shutter_id": 1, "state_generation": GENERATION}}
        for title, expected in ((OLD_TITLE, f"X2D Gateway {IDENTITY[-6:]}"),
                                ("Roof gateway", "Roof gateway"),
                                ("X2D USB 123456", "X2D USB 123456"),
                                (f"X2D USB Gateway {IDENTITY[-6:]}", f"X2D Gateway {IDENTITY[-6:]}"),
                                (f"X2D Gateway {IDENTITY[-6:]}", f"X2D Gateway {IDENTITY[-6:]}")):
            with self.subTest(title=title):
                async with home_assistant() as (hass, peer):
                    peer.info["capabilities"] = COMMANDS_ONLY
                    peer.shutters = {1: paired(1, "close")}
                    entry = ConfigEntry(
                        data={"device": peer.device}, discovery_keys=MappingProxyType({}), domain="x2d",
                        minor_version=1, options={}, source="usb", subentries_data=[subentry],
                        title=title, unique_id=IDENTITY, version=1)
                    hass.config_entries._entries[entry.entry_id] = entry  # persisted, not yet set up
                    dev_reg, ent_reg = device_registry.async_get(hass), entity_registry.async_get(hass)
                    gateway = dev_reg.async_get_or_create(
                        config_entry_id=entry.entry_id, identifiers={("x2d", IDENTITY)}, name=OLD_TITLE,
                        manufacturer="ha-x2d", model="YD-RP2040 / CC1101", sw_version="0.3.0")
                    dev_reg.async_update_device(gateway.id, name_by_user="Passerelle salon")
                    shutter = dev_reg.async_get_or_create(
                        config_entry_id=entry.entry_id, config_subentry_id=subentry["subentry_id"],
                        identifiers={("x2d", f"{IDENTITY}_{GENERATION}_1")}, name="Volet C",
                        manufacturer="ha-x2d", model="X2D shutter controller", via_device_id=gateway.id)
                    dev_reg.async_update_device(shutter.id, name_by_user="Volet du salon")
                    seeded = {}
                    for domain, unique_id, object_id, device, sub in (
                            ("sensor", f"{IDENTITY}_radio_status", "x2d_usb_abcdef_cc1101_status", gateway, None),
                            ("binary_sensor", f"{IDENTITY}_connectivity", "x2d_usb_abcdef_usb_connection", gateway, None),
                            ("cover", f"{IDENTITY}_{GENERATION}_1", "volet_c", shutter, subentry["subentry_id"])):
                        seeded[unique_id] = ent_reg.async_get_or_create(
                            domain, "x2d", unique_id, suggested_object_id=object_id, config_entry=entry,
                            config_subentry_id=sub, device_id=device.id).entity_id
                    self.assertEqual(seeded[f"{IDENTITY}_{GENERATION}_1"], "cover.volet_c")
                    self.assertTrue(await hass.config_entries.async_setup(entry.entry_id))
                    await hass.async_block_till_done()
                    self.assertEqual(entry.state, ConfigEntryState.LOADED)
                    self.assertEqual((entry.title, entry.unique_id, dict(entry.data)), (expected, IDENTITY, {"device": peer.device}))
                    self.assertEqual(entry.subentries[subentry["subentry_id"]].title, "Volet C")
                    self.assertEqual({r["op"] for r in peer.requests} - {"hello", "status", "shutters"}, set())
                    self.assertEqual(len(device_registry.async_entries_for_config_entry(dev_reg, entry.entry_id)), 2)
                    upgraded = dev_reg.async_get(gateway.id)
                    self.assertEqual((upgraded.name, upgraded.name_by_user, upgraded.model),
                                     (entry.title, "Passerelle salon", "X2D Gateway"))
                    child = dev_reg.async_get(shutter.id)
                    self.assertEqual((child.name, child.name_by_user, child.via_device_id),
                                     ("Volet C", "Volet du salon", gateway.id))
                    entities = entity_registry.async_entries_for_config_entry(ent_reg, entry.entry_id)
                    self.assertEqual({e.unique_id: e.entity_id for e in entities}, seeded)
                    self.assertEqual(hass.states.get("cover.volet_c").state, "unknown")
                    self.assertEqual(hass.states.get("cover.volet_c").attributes["last_command_intent"], "close")


class BrandApiChecks(unittest.IsolatedAsyncioTestCase):
    async def test_ha_serves_both_packaged_icons_for_the_integration(self):
        async with home_assistant() as (hass, _):
            from homeassistant.components import brands
            from homeassistant.components.http import KEY_AUTHENTICATED

            class Request(dict):
                query: dict = {}
                headers: dict = {}

            hass.data[brands.DOMAIN] = deque(["unused"])
            view = brands.BrandsIntegrationView(hass)
            served = {}
            for image in ("icon.png", "icon@2x.png"):
                response = await view.get(Request({KEY_AUTHENTICATED: True}), "x2d", image)
                self.assertEqual((response.status, response.content_type), (200, "image/png"), image)
                self.assertEqual(response.body, (ROOT / "custom_components/x2d/brand" / image).read_bytes())
                served[image] = response.body
            self.assertNotEqual(served["icon.png"], served["icon@2x.png"])  # no fallback to the 1x file


class HomeAssistantChecks(unittest.IsolatedAsyncioTestCase):
    async def test_packaged_flow_entities_reconnect_and_unload(self):
        with tempfile.TemporaryDirectory(prefix="ha-x2d-check-") as directory:
            config = Path(directory)
            archive = build(config, hacs=True)
            component = config / "custom_components/x2d"
            component.mkdir(parents=True)
            with ZipFile(archive) as bundle:
                bundle.extractall(component)
            self.assertEqual(
                (config / "custom_components/x2d/_client/__init__.py").read_bytes(),
                (ROOT / "python/src/x2d_gateway/__init__.py").read_bytes(),
            )
            sys.path.insert(0, directory)
            hass = HomeAssistant(directory)
            hass.config.skip_pip = True
            # These servers are not part of this adapter test.
            hass.config.components.update({"http", "websocket_api"})
            loader.async_setup(hass)
            frame.async_setup(hass)
            translation.async_setup(hass)
            device_registry.async_setup(hass)
            hass.config_entries = ConfigEntries(hass, {})
            await hass.config_entries.async_initialize()
            await device_registry.async_load(hass)
            await entity_registry.async_load(hass)
            peer = SimulatedGateway()
            try:
                # USB enumeration is host-specific; everything from flow to serial IO
                # and entity registration runs unchanged. Never scan real hardware.
                with patch("homeassistant.components.usb.async_setup", AsyncMock(return_value=True)):
                    matchers = [m for m in await loader.async_get_usb(hass) if m["domain"] == "x2d"]
                    self.assertEqual(len(matchers), 1)
                    usb_properties = {
                        "device": peer.device, "vid": "2E8A", "pid": "800A",
                        "serial_number": IDENTITY, "manufacturer": "ha-x2d",
                    }
                    port = SerialPortInfo(
                        device=peer.device, resolved_device=peer.device,
                        vid=0x2E8A, pid=0x800A, serial_number=IDENTITY,
                        manufacturer="ha-x2d", bcd_device=None, interface_num=0,
                        product="HA-X2D Gateway", interface_description="Pico Serial",
                    )
                    self.assertEqual(port.description, "HA-X2D Gateway - Pico Serial")
                    self.assertTrue(usb_device_matches_matcher(
                        USBDevice(**usb_properties, description=port.description), matchers[0]
                    ))
                    self.assertTrue(usb_device_matches_matcher(
                        USBDevice(**usb_properties, description="HA-X2D Gateway"), matchers[0]
                    ))
                    self.assertFalse(usb_device_matches_matcher(
                        USBDevice(**usb_properties, description="VCC-GND YD RP2040"), matchers[0]
                    ))
                    flow = await hass.config_entries.flow.async_init(
                        "x2d", context={"source": "user"}
                    )
                    self.assertEqual(flow["type"], "form")
                    flow = await hass.config_entries.flow.async_configure(
                        flow["flow_id"], {"device": peer.device}
                    )
                    self.assertEqual(flow["type"], "create_entry", flow)
                    await hass.async_block_till_done()
                    entry = flow["result"]
                    self.assertEqual(entry.unique_id, IDENTITY)
                    self.assertEqual(entry.state, ConfigEntryState.LOADED)
                    coordinator = entry.runtime_data
                    entities = entity_registry.async_entries_for_config_entry(
                        entity_registry.async_get(hass), entry.entry_id
                    )
                    self.assertEqual(len(entities), 2)
                    states = {e.domain: hass.states.get(e.entity_id) for e in entities}
                    self.assertEqual(states["sensor"].state, "idle")
                    self.assertEqual(states["binary_sensor"].state, "on")
                    registered = device_registry.async_get(hass).async_get(entities[0].device_id)
                    self.assertEqual(registered.sw_version, "0.3.0")

                    flow_module = importlib.import_module("custom_components.x2d.config_flow")
                    manager = hass.config_entries.subentries
                    component = config / "custom_components/x2d"
                    strings = json.loads((component / "strings.json").read_text())
                    self.assertEqual(strings, json.loads((component / "translations/en.json").read_text()))
                    for language, procedure in (
                        ("en", ("normal mode N", "Hold STOP", "then release", "Within 1 minute", "identity C only")),
                        ("fr", ("mode normal N", "Maintenez STOP", "puis relâchez", "Dans la minute", "uniquement la nouvelle identité C")),
                    ):
                        instructions = flow_module.ENROLLMENT_INSTRUCTIONS[language]
                        for detail in ("Franciasoft", "Well'com", "BASE", *procedure):
                            self.assertIn(detail, instructions)
                        localized = json.loads((component / f"translations/{language}.json").read_text())
                        steps = localized["config_subentries"]["shutter"]["step"]
                        self.assertTrue(localized["config_subentries"]["shutter"]["abort"]["enrollment_unavailable"])
                        self.assertIn("{instructions}", steps["enroll"]["description"])
                        self.assertIn("C", steps["confirm"]["data"]["motor_response"])

                    async def add_shutter(slot, name):
                        result = await manager.async_init((entry.entry_id, "shutter"), context={"source": "user"})
                        self.assertEqual(result["step_id"], "user", result)
                        result = await manager.async_configure(result["flow_id"], {"name": name, "shutter_id": slot})
                        self.assertEqual(result["step_id"], "test" if peer.shutters[slot]["state"] == "paired" else "enroll", result)
                        return result

                    # The operational unverified firmware can advertise only
                    # status/shutters: HA must not provision any controller.
                    original_caps = peer.info["capabilities"]
                    peer.info["capabilities"] = ["status", "shutters"]
                    peer.status["tx_enabled"] = False
                    await coordinator.gateway.close()
                    await coordinator.async_refresh()
                    start = len(peer.requests)
                    result = await manager.async_init((entry.entry_id, "shutter"), context={"source": "user"})
                    self.assertEqual(result["reason"], "profile_unverified")
                    self.assertEqual(peer.shutters, {})
                    self.assertFalse(any(r["op"] == "provision" for r in peer.requests[start:]))
                    peer.info["capabilities"] = original_caps
                    peer.status["tx_enabled"] = True
                    await coordinator.gateway.close()
                    await coordinator.async_refresh()

                    # A commands-only key without a paired controller cannot
                    # enroll one, rather than having exhausted all its slots.
                    peer.info["capabilities"] = ["status", "shutters", "command"]
                    await coordinator.gateway.close()
                    await coordinator.async_refresh()
                    start = len(peer.requests)
                    result = await manager.async_init((entry.entry_id, "shutter"), context={"source": "user"})
                    self.assertEqual(result["reason"], "enrollment_unavailable")
                    self.assertEqual(peer.shutters, {})
                    self.assertFalse(any(r["op"] in {"provision", "pair", "confirm", "command"}
                                         for r in peer.requests[start:]))
                    peer.info["capabilities"] = original_caps
                    await coordinator.gateway.close()
                    await coordinator.async_refresh()

                    # Interrupted enrollment leaves pending identity on the gateway.
                    for language, expected in (("fr", "fr"), ("en", "en"), ("de", "en")):
                        hass.config.language = language
                        pending = await add_shutter(1, "Living room")
                        self.assertEqual(pending["description_placeholders"]["instructions"],
                                         flow_module.ENROLLMENT_INSTRUCTIONS[expected])
                        manager.async_abort(pending["flow_id"])
                    hass.config.language = "en"
                    before = deepcopy(peer.shutters[1])
                    pending = await add_shutter(1, "Living room")
                    self.assertEqual(peer.shutters[1], before)
                    self.assertFalse(any(r["op"] == "pair" for r in peer.requests))
                    rejected = await manager.async_configure(pending["flow_id"], {"physical_ready": False})
                    self.assertEqual(rejected["errors"]["base"], "physical_ready_required")
                    start = len(peer.requests)
                    peer.status["tx_enabled"] = False
                    rejected = await manager.async_configure(pending["flow_id"], {"physical_ready": True})
                    self.assertEqual(rejected["errors"]["base"], "profile_unverified")
                    self.assertEqual(peer.shutters[1]["state"], "pending")
                    peer.status["tx_enabled"] = True
                    peer.info["capabilities"] = [cap for cap in original_caps if cap != "pair"]
                    await coordinator.gateway.close()
                    await coordinator.async_refresh()
                    rejected = await manager.async_configure(pending["flow_id"], {"physical_ready": True})
                    self.assertEqual(rejected["errors"]["base"], "profile_unverified")
                    self.assertFalse(any(r["op"] in ("pair", "confirm", "command") for r in peer.requests[start:]))
                    peer.info["capabilities"] = original_caps
                    await coordinator.gateway.close()
                    await coordinator.async_refresh()
                    start = len(peer.requests)
                    result = await manager.async_configure(pending["flow_id"], {"physical_ready": True})
                    self.assertEqual(result["step_id"], "confirm", result)
                    self.assertEqual([r["op"] for r in peer.requests[start:] if r["op"] in ("pair", "confirm", "command")], ["pair"])
                    self.assertEqual(next(r["args"] for r in peer.requests[start:] if r["op"] == "pair"), {"shutter_id": 1})
                    self.assertEqual(peer.shutters[1]["state"], "pending")
                    self.assertEqual(len(entry.subentries), 0)
                    result = await manager.async_configure(result["flow_id"], {"motor_response": False})
                    self.assertEqual(result["errors"]["base"], "motor_response_required")
                    self.assertFalse(any(r["op"] == "confirm" for r in peer.requests))
                    result = await manager.async_configure(result["flow_id"], {"motor_response": True})
                    self.assertEqual(result["step_id"], "test", result)
                    self.assertEqual(peer.shutters[1]["state"], "paired")
                    self.assertEqual(len(entry.subentries), 0)
                    # Interruption after durable pairing resumes at the test;
                    # neither enrollment nor confirmation is replayed.
                    manager.async_abort(result["flow_id"])
                    start = len(peer.requests)
                    result = await add_shutter(1, "Living room")
                    self.assertEqual(result["step_id"], "test")
                    self.assertFalse(any(r["op"] in ("pair", "confirm", "command") for r in peer.requests[start:]))
                    stale = await add_shutter(1, "Concurrent stale flow")
                    result = await manager.async_configure(result["flow_id"], {"action": "open"})
                    self.assertEqual(result["step_id"], "test_confirm", result)
                    result = await manager.async_configure(result["flow_id"], {"test_observed": False})
                    self.assertEqual(result["errors"]["base"], "test_observed_required")
                    self.assertEqual(len(entry.subentries), 0)
                    # Selecting another test sends nothing until its action is
                    # explicitly submitted, including STOP and CLOSE.
                    for action in ("stop", "close"):
                        start = len(peer.requests)
                        result = await manager.async_configure(result["flow_id"], {"test_observed": False, "test_again": True})
                        self.assertEqual(result["step_id"], "test")
                        self.assertEqual(len(peer.requests), start)
                        result = await manager.async_configure(result["flow_id"], {"action": action})
                        self.assertEqual(result["step_id"], "test_confirm")
                    result = await manager.async_configure(result["flow_id"], {"test_observed": True})
                    self.assertEqual(result["type"], "create_entry", result)
                    await hass.async_block_till_done()
                    coordinator = entry.runtime_data
                    first_subentry = next(iter(entry.subentries.values()))
                    self.assertEqual(dict(first_subentry.data), {"shutter_id": 1, "state_generation": GENERATION})
                    start = len(peer.requests)
                    stale = await manager.async_configure(stale["flow_id"], {"action": "stop"})
                    self.assertEqual(stale["reason"], "already_configured")
                    self.assertEqual(len(peer.requests), start)
                    self.assertEqual(len(entry.subentries), 1)

                    # One paired controller already adopted on a 16-slot
                    # commands-only key does not mean that 16 slots are used.
                    peer.info["capabilities"] = ["status", "shutters", "command"]
                    await coordinator.gateway.close()
                    await coordinator.async_refresh()
                    before = deepcopy(peer.shutters)
                    start = len(peer.requests)
                    result = await manager.async_init((entry.entry_id, "shutter"), context={"source": "user"})
                    self.assertEqual(result["reason"], "enrollment_unavailable")
                    self.assertEqual(peer.shutters, before)
                    self.assertFalse(any(r["op"] in {"provision", "pair", "confirm", "command"}
                                         for r in peer.requests[start:]))

                    # Keep the capacity error for firmware that can enroll
                    # controllers when every slot is actually configured.
                    peer.info["capabilities"] = original_caps
                    await coordinator.gateway.close()
                    await coordinator.async_refresh()
                    with patch.object(flow_module.ShutterFlow, "_used", return_value=set(range(1, 17))):
                        result = await manager.async_init((entry.entry_id, "shutter"), context={"source": "user"})
                    self.assertEqual(result["reason"], "no_slots")
                    pending = await add_shutter(2, "Bedroom")
                    result = await manager.async_configure(pending["flow_id"], {"physical_ready": True})
                    result = await manager.async_configure(result["flow_id"], {"motor_response": True})
                    self.assertEqual(result["step_id"], "test", result)
                    self.assertEqual(peer.shutters[2]["state"], "paired")

                    # Adopt a paired identity from commands-only firmware without
                    # allocating, enrolling or confirming it a second time.
                    manager.async_abort(result["flow_id"])
                    peer.info["capabilities"] = ["status", "shutters", "command"]
                    await coordinator.gateway.close()
                    await coordinator.async_refresh()
                    start = len(peer.requests)
                    # The single recorded shutter is offered with a name-only form.
                    result = await manager.async_init((entry.entry_id, "shutter"), context={"source": "user"})
                    self.assertEqual(result["step_id"], "recorded", result)
                    self.assertEqual({k.schema for k in result["data_schema"].schema}, {"name"})
                    result = await manager.async_configure(result["flow_id"], {"name": "Bedroom"})
                    self.assertEqual(result["step_id"], "test", result)
                    self.assertFalse(any(r["op"] in {"provision", "pair", "confirm"}
                                         for r in peer.requests[start:]))
                    result = await manager.async_configure(result["flow_id"], {"action": "stop"})
                    result = await manager.async_configure(result["flow_id"], {"test_observed": True})
                    self.assertEqual(result["type"], "create_entry", result)
                    await hass.async_block_till_done()
                    coordinator = entry.runtime_data
                    peer.info["capabilities"] = original_caps
                    await coordinator.gateway.close()
                    await coordinator.async_refresh()
                    # Journal replacement while awaiting human confirmation must
                    # abort before a confirm write, leaving a pending record.
                    pending = await add_shutter(3, "Interrupted")
                    result = await manager.async_configure(pending["flow_id"], {"physical_ready": True})
                    peer.status["storage"]["generation"] = "3333333344444444"
                    start = len(peer.requests)
                    result = await manager.async_configure(result["flow_id"], {"motor_response": True})
                    self.assertEqual(result["reason"], "generation_changed")
                    self.assertFalse(any(r["op"] == "confirm" for r in peer.requests[start:]))
                    self.assertEqual(peer.shutters[3]["state"], "pending")
                    peer.status["storage"]["generation"] = GENERATION
                    await coordinator.async_refresh()
                    for storage_state in ("corrupt", "full"):
                        peer.status["storage"] = {"state": storage_state, "generation": None if storage_state == "corrupt" else GENERATION}
                        saved = peer.shutters
                        if storage_state == "corrupt":
                            peer.shutters = {}
                        result = await manager.async_init((entry.entry_id, "shutter"), context={"source": "user"})
                        self.assertEqual(result["reason"], "storage_" + storage_state)
                        peer.shutters = saved
                    peer.status["storage"] = {"state": "ready", "generation": GENERATION}
                    await coordinator.async_refresh()

                    entities = entity_registry.async_entries_for_config_entry(entity_registry.async_get(hass), entry.entry_id)
                    covers = [e for e in entities if e.domain == "cover"]
                    self.assertEqual(len(covers), 2)
                    self.assertEqual({e.config_subentry_id for e in covers}, set(entry.subentries))
                    for cover in covers:
                        state = hass.states.get(cover.entity_id)
                        self.assertEqual(state.state, "unknown")
                        self.assertNotIn("current_position", state.attributes)
                        self.assertEqual(state.attributes["supported_features"], 11)
                        self.assertEqual(state.attributes["last_command_intent"], "close" if cover.config_subentry_id == first_subentry.subentry_id else "stop")
                    cover1 = next(e for e in covers if e.config_subentry_id == first_subentry.subentry_id)
                    for service in ("open_cover", "stop_cover", "close_cover"):
                        await hass.services.async_call("cover", service, {"entity_id": cover1.entity_id}, blocking=True)
                    self.assertEqual(peer.shutters[1]["last_command"], "close")
                    state = hass.states.get(cover1.entity_id)
                    self.assertEqual(state.state, "unknown")
                    self.assertEqual(state.attributes["last_command_intent"], "close")
                    self.assertEqual(peer.shutters[2]["last_command"], "stop")

                    # Native HA services must also let STOP through an OPEN TX wait.
                    peer.auto_tx = False
                    opening = asyncio.create_task(hass.services.async_call("cover", "open_cover", {"entity_id": cover1.entity_id}, blocking=True))
                    async with asyncio.timeout(2):
                        while not (peer.requests[-1]["op"] == "command" and peer.requests[-1]["args"]["action"] == "open"):
                            await asyncio.sleep(0.001)
                    open_request = peer.requests[-1]
                    stopping = asyncio.create_task(hass.services.async_call("cover", "stop_cover", {"entity_id": cover1.entity_id}, blocking=True))
                    async with asyncio.timeout(2):
                        while not (peer.requests[-1]["op"] == "command" and peer.requests[-1]["args"]["action"] == "stop"):
                            await asyncio.sleep(0.001)
                    peer.tx_result(peer.requests[-1])
                    await stopping
                    self.assertFalse(opening.done())
                    peer.tx_result(open_request, "cancelled")
                    with self.assertRaisesRegex(Exception, "tx_cancelled"):
                        await opening
                    peer.auto_tx = True

                    # STOP must be written before a delayed status reply is
                    # released, even while the coordinator refresh owns its lock.
                    delayed_status = []
                    def hold_status(request, response):
                        if request["op"] == "status":
                            delayed_status.append(response)
                            return None
                        return response
                    peer.response_filter = hold_status
                    refresh = asyncio.create_task(coordinator.async_refresh())
                    async with asyncio.timeout(1):
                        while not delayed_status:
                            await asyncio.sleep(0.001)
                    start = len(peer.requests)
                    stopping = asyncio.create_task(hass.services.async_call(
                        "cover", "stop_cover", {"entity_id": cover1.entity_id}, blocking=True
                    ))
                    try:
                        async with asyncio.timeout(0.5):
                            while not any(r["op"] == "command" and r["args"]["action"] == "stop" for r in peer.requests[start:]):
                                await asyncio.sleep(0.001)
                        self.assertFalse(refresh.done())
                        self.assertEqual([r["op"] for r in peer.requests[start:]], ["command"])
                    finally:
                        peer.response_filter = lambda _, response: response
                        for response in delayed_status:
                            peer.send(response)
                        await refresh
                        await stopping

                    # A newly opened connection cannot use the previous
                    # connection's ready snapshot before its journal is checked.
                    previous_gateway = coordinator.gateway
                    await previous_gateway.close()
                    peer.status["storage"]["generation"] = "3333333344444444"
                    delayed_status.clear()
                    peer.response_filter = hold_status
                    refresh = asyncio.create_task(coordinator.async_refresh())
                    try:
                        async with asyncio.timeout(1):
                            while not delayed_status:
                                await asyncio.sleep(0.001)
                        self.assertIsNot(coordinator.gateway, previous_gateway)
                        self.assertFalse(coordinator.ready(1, GENERATION))
                        start = len(peer.requests)
                        with self.assertRaisesRegex(Exception, "shutter_unavailable"):
                            await coordinator.async_command(1, GENERATION, "stop")
                        self.assertEqual(len(peer.requests), start)
                    finally:
                        peer.response_filter = lambda _, response: response
                        for response in delayed_status:
                            peer.send(response)
                        await refresh
                    self.assertFalse(coordinator.ready(1, GENERATION))
                    peer.status["storage"]["generation"] = GENERATION
                    await coordinator.async_refresh()
                    self.assertTrue(coordinator.ready(1, GENERATION))

                    # A USB rediscovery must not open a second session or duplicate
                    # the entry while its port is owned by the coordinator.
                    request_count = len(peer.requests)
                    duplicate = await hass.config_entries.flow.async_init(
                        "x2d", context={"source": "usb"},
                        data=UsbServiceInfo(
                            device=peer.device, vid="2E8A", pid="800A",
                            serial_number=IDENTITY.lower(), manufacturer="ha-x2d",
                            description="HA-X2D Gateway",
                        ),
                    )
                    self.assertEqual(duplicate["reason"], "already_configured")
                    self.assertEqual(len(peer.requests), request_count)

                    diagnostics = importlib.import_module("custom_components.x2d.diagnostics")
                    exported = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
                    self.assertNotIn(IDENTITY, json.dumps(exported))
                    self.assertNotIn(peer.info["session"], json.dumps(exported))
                    self.assertNotIn(peer.device, json.dumps(exported))

                    peer.unplug()
                    await coordinator.async_refresh()
                    start = len(peer.requests)
                    with self.assertRaisesRegex(Exception, "shutter_unavailable"):
                        await coordinator.async_command(1, GENERATION, "stop")
                    self.assertEqual(len(peer.requests), start)
                    await hass.async_block_till_done()
                    states = {e.domain: hass.states.get(e.entity_id) for e in entities}
                    self.assertEqual(states["binary_sensor"].state, "off")
                    self.assertTrue(all(hass.states.get(e.entity_id).state == "unavailable" for e in covers))
                    self.assertEqual(states["sensor"].state, "unavailable")
                    saved_slots = deepcopy(peer.shutters)
                    peer.close()
                    peer = SimulatedGateway()
                    peer.shutters = saved_slots
                    peer.info["session"] = "BBBBBBBBBBBBBBBB"
                    hass.config_entries.async_update_entry(entry, data={"device": peer.device})
                    await coordinator.async_refresh()
                    self.assertTrue(coordinator.last_update_success)
                    self.assertEqual(coordinator.info["device_id"], IDENTITY)
                    self.assertFalse(any(r["op"] in ("pair", "confirm", "command", "provision") for r in peer.requests))
                    await hass.async_block_till_done()
                    self.assertTrue(all(hass.states.get(e.entity_id).state == "unknown" for e in covers))
                    peer.status["storage"]["generation"] = "3333333344444444"
                    await coordinator.async_refresh()
                    self.assertTrue(all(hass.states.get(e.entity_id).state == "unavailable" for e in covers))
                    start = len(peer.requests)
                    with self.assertRaisesRegex(Exception, "shutter_unavailable"):
                        await coordinator.async_command(1, GENERATION, "stop")
                    self.assertEqual(len(peer.requests), start)
                    peer.status["storage"]["generation"] = GENERATION
                    await coordinator.async_refresh()
                    for key, bad in (("tx_enabled", False), ("storage", {"state": "full", "generation": GENERATION}), ("radio", {"detected": False, "partnum": None, "version": None, "marcstate": None})):
                        previous = deepcopy(peer.status[key])
                        peer.status[key] = bad
                        await coordinator.async_refresh()
                        self.assertTrue(all(hass.states.get(e.entity_id).state == "unavailable" for e in covers))
                        peer.status[key] = previous
                    await coordinator.async_refresh()
                    peer.shutters[1]["state"] = "pending"
                    await coordinator.async_refresh()
                    self.assertEqual(hass.states.get(cover1.entity_id).state, "unavailable")
                    peer.shutters[1]["state"] = "paired"
                    await coordinator.async_refresh()

                    # Deleting the HA entity and subentry performs no RF or resets.
                    start = len(peer.requests)
                    entity_registry.async_get(hass).async_remove(cover1.entity_id)
                    hass.config_entries.async_remove_subentry(entry, first_subentry.subentry_id)
                    await hass.async_block_till_done()
                    coordinator = entry.runtime_data
                    self.assertEqual(peer.shutters[1]["state"], "paired")
                    self.assertFalse(any(r["op"] in ("pair", "confirm", "command", "provision") for r in peer.requests[start:]))

                    await coordinator.gateway.close()
                    coordinator.gateway = None
                    peer.info["device_id"] = "FFFFFFFFFFFFFFFF"
                    await coordinator.async_refresh()
                    self.assertFalse(coordinator.last_update_success)
                    self.assertIsNone(coordinator.gateway)
                    peer.info["device_id"] = IDENTITY
                    await coordinator.async_refresh()
                    self.assertTrue(coordinator.last_update_success)
                    self.assertTrue(await hass.config_entries.async_unload(entry.entry_id))
                    self.assertIsNone(coordinator.gateway)
            finally:
                peer.close()
                await hass.async_stop(force=True)
                sys.path.remove(directory)
                for name in list(sys.modules):
                    if name == "custom_components" or name.startswith("custom_components."):
                        del sys.modules[name]


if __name__ == "__main__":
    unittest.main()
