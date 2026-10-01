"""Slot references, never optimistic position or motor reception claims."""

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, GatewayCoordinator
from ._client import GatewayError


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    parent = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.runtime_data.info["device_id"])},
        name=entry.title, manufacturer="ha-x2d", model="YD-RP2040 / CC1101",
        sw_version=entry.runtime_data.info["firmware"],
    )
    for subentry in entry.subentries.values():
        if subentry.subentry_type == "shutter":
            async_add_entities([Shutter(entry.runtime_data, subentry, parent.id)],
                               config_subentry_id=subentry.subentry_id)


class Shutter(CoordinatorEntity[GatewayCoordinator], CoverEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_device_class = CoverDeviceClass.SHUTTER
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.STOP | CoverEntityFeature.CLOSE
    _attr_is_closed = None
    _attr_current_cover_position = None
    _attr_is_opening = None
    _attr_is_closing = None

    def __init__(self, coordinator, subentry, parent_device_id):
        super().__init__(coordinator)
        self.slot = subentry.data["shutter_id"]
        self.generation = subentry.data["state_generation"]
        identity = coordinator.info["device_id"]
        reference = f"{identity}_{self.generation}_{self.slot}"
        self._attr_unique_id = reference
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, reference)}, name=subentry.title,
            manufacturer="ha-x2d", model="X2D shutter controller",
            via_device_id=parent_device_id,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.ready(self.slot, self.generation)

    @property
    def extra_state_attributes(self) -> dict:
        shutter = self.coordinator.shutter(self.slot, self.generation)
        return {"last_command_intent": shutter["last_command"] if shutter else None}

    async def _command(self, action):
        try:
            await self.coordinator.async_command(self.slot, self.generation, action)
        except (GatewayError, OSError, TimeoutError, ValueError) as exc:
            raise HomeAssistantError(f"X2D command failed: {exc}") from exc

    async def async_open_cover(self, **kwargs):
        await self._command("open")

    async def async_close_cover(self, **kwargs):
        await self._command("close")

    async def async_stop_cover(self, **kwargs):
        await self._command("stop")
