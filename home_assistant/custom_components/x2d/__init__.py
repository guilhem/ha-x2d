"""Home Assistant adapter for the independent USB gateway library."""

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ._client import Gateway

DOMAIN = "x2d"
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]
LOGGER = logging.getLogger(__name__)


class GatewayCoordinator(DataUpdateCoordinator[dict]):
    """Keep the port open; after a failure, reconnect to the same physical key."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, LOGGER, name=DOMAIN, config_entry=entry,
            update_interval=timedelta(seconds=30),
        )
        self.entry = entry
        self.gateway: Gateway | None = None
        self.info: dict = {}

    async def _async_update_data(self) -> dict:
        try:
            if self.gateway is None:
                self.gateway = await Gateway.open(
                    self.entry.data[CONF_DEVICE], expected_device_id=self.entry.unique_id
                )
                self.info = self.gateway.info
            return await self.gateway.status()
        except (OSError, TimeoutError, ValueError) as exc:
            if self.gateway is not None:
                await self.gateway.close()
                self.gateway = None
            raise UpdateFailed(f"Gateway unavailable: {exc}") from exc

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self.gateway is not None:
            await self.gateway.close()
            self.gateway = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = GatewayCoordinator(hass, entry)
    entry.runtime_data = coordinator
    try:
        await coordinator.async_config_entry_first_refresh()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_shutdown()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
        return True
    return False
