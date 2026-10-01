"""Load the distributable component in real HA Core against a simulated USB key."""

import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

from serialx import SerialPortInfo

from homeassistant import loader
from homeassistant.components.usb.models import USBDevice
from homeassistant.components.usb.utils import usb_device_matches_matcher
from homeassistant.config_entries import ConfigEntries, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry, frame, translation
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from test_gateway import IDENTITY, GENERATION, SimulatedGateway

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
                    result = await add_shutter(2, "Bedroom")
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
