"""Load the distributable component in real HA Core against a simulated USB key."""

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

from test_gateway import IDENTITY, SimulatedGateway

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_component import build


class HomeAssistantChecks(unittest.IsolatedAsyncioTestCase):
    async def test_packaged_flow_entities_reconnect_and_unload(self):
        with tempfile.TemporaryDirectory(prefix="ha-x2d-check-") as directory:
            config = Path(directory)
            archive = build(config)
            with ZipFile(archive) as bundle:
                bundle.extractall(config)
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
                    self.assertEqual(registered.sw_version, "0.1.0")

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
                    states = {e.domain: hass.states.get(e.entity_id) for e in entities}
                    self.assertEqual(states["binary_sensor"].state, "off")
                    self.assertEqual(states["sensor"].state, "unavailable")
                    peer.close()
                    peer = SimulatedGateway()
                    hass.config_entries.async_update_entry(entry, data={"device": peer.device})
                    await coordinator.async_refresh()
                    self.assertTrue(coordinator.last_update_success)
                    self.assertEqual(coordinator.info["device_id"], IDENTITY)

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
