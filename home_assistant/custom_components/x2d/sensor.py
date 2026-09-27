"""SPI detection diagnostic; no inferred cover state."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, GatewayCoordinator


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([RadioStatus(entry.runtime_data)])


class RadioStatus(CoordinatorEntity[GatewayCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "radio_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:radio-tower"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["not_detected", "idle", "detected"]

    def __init__(self, coordinator: GatewayCoordinator) -> None:
        super().__init__(coordinator)
        identity = coordinator.info["device_id"]
        self._attr_unique_id = f"{identity}_radio_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity)},
            name=f"X2D USB {identity[-6:]}",
            manufacturer="ha-x2d",
            model="YD-RP2040 / CC1101",
            sw_version=coordinator.info["firmware"],
        )

    @property
    def native_value(self) -> str:
        radio = self.coordinator.data["radio"]
        if not radio["detected"]:
            return "not_detected"
        return "idle" if radio["marcstate"] == 1 else "detected"
