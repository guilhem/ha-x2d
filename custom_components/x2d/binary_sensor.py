"""Gateway connectivity becomes false after a failed diagnostic poll."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, GatewayCoordinator


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([GatewayConnectivity(entry.runtime_data)])


class GatewayConnectivity(CoordinatorEntity[GatewayCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "connectivity"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: GatewayCoordinator) -> None:
        super().__init__(coordinator)
        identity = coordinator.info["device_id"]
        self._attr_unique_id = f"{identity}_connectivity"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, identity)})

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success
