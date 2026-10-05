"""Home Assistant sources + pymysensors sources + real C++ adapter over PTY.

Use Home Assistant's development venv, with both source clones on PYTHONPATH.
The file upload registry is populated as the HTTP uploader does; the actual
options flow, candidate storage, entity discovery and update.install run here.
Flash and RF remain simulated. This is not a physical device qualification.
"""

import asyncio
import contextlib
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from homeassistant import loader
from homeassistant.components import file_upload
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry, frame, translation

from test_native_ota import cpp_controller, rp2040_image, until, wire


@contextlib.asynccontextmanager
async def home_assistant(directory):
    hass = HomeAssistant(str(directory))
    hass.config.skip_pip = True
    # Networking/authentication is covered by HA's file_upload suite. This test
    # starts at the completed upload token, which the options flow consumes.
    hass.config.components.update({"http", "websocket_api", "file_upload"})
    loader.async_setup(hass)
    frame.async_setup(hass)
    translation.async_setup(hass)
    device_registry.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    try:
        yield hass
    finally:
        for entry in hass.config_entries.async_entries("mysensors"):
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_stop(force=True)


async def add_gateway(hass, device, *, port=None):
    manager = hass.config_entries.flow
    flow = await manager.async_init("mysensors", context={"source": "user"})
    step = "gw_serial" if port is None else "gw_tcp"
    flow = await manager.async_configure(flow["flow_id"], {"next_step_id": step})
    data = {"device": device, "version": "2.3"}
    data.update({"baud_rate": 115200} if port is None else {"tcp_port": port})
    flow = await manager.async_configure(flow["flow_id"], data)
    if flow["type"] != "create_entry":
        raise AssertionError(flow)
    await hass.async_block_till_done()
    entry = flow["result"]
    entry.runtime_data.gateway.send(wire(1, 3, 19))
    await until(lambda: bool(hass.states.async_all("update")), timeout=10)
    await hass.async_block_till_done()
    return entry


async def import_candidate(hass, entry, directory, data, version, node=1):
    root = Path(directory) / "upload"
    upload_id = uuid4().hex
    path = root / upload_id / "firmware.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    hass.data[file_upload.DOMAIN] = file_upload.FileUploadData(root, {upload_id: path.name})
    options = hass.config_entries.options
    flow = await options.async_init(entry.entry_id)
    if flow["type"] != "form" or flow["step_id"] != "init":
        raise AssertionError(flow)
    flow = await options.async_configure(flow["flow_id"], {"node_id": str(node)})
    if flow["type"] != "form" or flow["step_id"] != "import":
        raise AssertionError(flow)
    flow = await options.async_configure(flow["flow_id"], {
        "firmware_file": upload_id, "firmware_type": 0x5832, "firmware_version": version})
    if flow["type"] != "create_entry":
        raise AssertionError(flow)
    await hass.async_block_till_done()


class NativeHomeAssistantOTATests(unittest.IsolatedAsyncioTestCase):
    async def test_import_reload_and_install_with_real_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            async with cpp_controller(directory, start_gateway=False) as rig:
                _, peer, active, journal, staged, report = rig
                async with home_assistant(directory) as hass:
                    entry = await add_gateway(hass, peer.path)
                    updates = hass.states.async_all("update")
                    self.assertEqual(len(updates), 1)
                    entity_id = updates[0].entity_id
                    self.assertEqual(updates[0].attributes["installed_version"], "7")
                    before = journal.read_bytes()
                    await import_candidate(hass, entry, directory, rp2040_image(8), 8)
                    self.assertFalse(staged.exists())
                    self.assertEqual(peer.messages(3, 13, node=1), [])
                    self.assertEqual(hass.states.get(entity_id).attributes["latest_version"], "8")
                    await hass.config_entries.async_reload(entry.entry_id)
                    entry.runtime_data.gateway.send(wire(1, 3, 19))
                    await until(lambda: hass.states.get(entity_id).state == "on")
                    self.assertIn(1, entry.runtime_data.firmware.candidates)
                    self.assertFalse(staged.exists())
                    await hass.services.async_call(
                        "update", "install", {"entity_id": entity_id}, blocking=True)
                    await hass.async_block_till_done()
                    state = hass.states.get(entity_id)
                    self.assertEqual(state.attributes["installed_version"], "8")
                    self.assertFalse(state.attributes["in_progress"])
                    self.assertEqual(active.read_bytes(), rp2040_image(8))
                    self.assertEqual(journal.read_bytes(), before)
                    self.assertNotIn(1, entry.runtime_data.firmware.candidates)
                    self.assertEqual(list(entry.runtime_data.firmware.directory.glob("*.bin")), [])
                    self.assertEqual(peer.messages(3, 13, node=1), [""])
                    self.assertFalse(any("burst start" in line for line in report))
                    self.assertEqual(len(hass.states.async_all("update")), 1)


if __name__ == "__main__":
    unittest.main()
