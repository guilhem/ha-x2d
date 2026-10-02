"""One connection shared by diagnostic entities and shutter subentries."""

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ._client import Gateway, GatewayError

DOMAIN = "x2d"
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.COVER]
LOGGER = logging.getLogger(__name__)


def default_title(device_id: str) -> str:
    return f"X2D Gateway {device_id[-6:]}"


class GatewayCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=timedelta(seconds=30))
        self.entry = entry
        self.gateway: Gateway | None = None
        self._validated_gateway: Gateway | None = None
        self.info: dict = {}
        self.device_id = ""  # gateway device registry id; set before platforms load

    async def _async_update_data(self) -> dict:
        try:
            if self.gateway is None or self.gateway.closed:
                if self.gateway is not None:
                    await self.gateway.close()
                self.gateway = await Gateway.open(
                    self.entry.data[CONF_DEVICE], expected_device_id=self.entry.unique_id
                )
                self.info = self.gateway.info
                self.gateway.on_disconnect = self._disconnected
            status = await self.gateway.status()
            shutters = await self.gateway.shutters()
            if status["storage"]["generation"] != shutters["generation"]:
                raise ValueError("Storage generation changed during refresh")
            self._validated_gateway = self.gateway
            return {**status, **shutters}
        except (OSError, TimeoutError, ValueError, GatewayError) as exc:
            if self.gateway is not None:
                await self.gateway.close()
                self.gateway = None
            raise UpdateFailed(f"Gateway unavailable: {exc}") from exc

    def _disconnected(self, exc: Exception) -> None:
        self.async_set_update_error(UpdateFailed(f"Gateway disconnected: {exc}"))

    def shutter(self, slot: int, generation: str) -> dict | None:
        if not self.last_update_success or not self.data or self.data["generation"] != generation:
            return None
        return next((s for s in self.data["shutters"] if s["shutter_id"] == slot), None)

    def ready(self, slot: int, generation: str) -> bool:
        shutter = self.shutter(slot, generation)
        return bool(self.gateway is not None and not self.gateway.closed
                    and self.gateway is self._validated_gateway
                    and shutter and shutter["state"] == "paired"
                    and self.data["storage"]["state"] == "ready"
                    and self.data["tx_enabled"] and self.data["radio"]["detected"]
                    and "command" in self.info["capabilities"])

    async def async_command(self, slot: int, generation: str, action: str) -> None:
        # Use only this connection's validated snapshot; polling must not delay STOP.
        if not self.ready(slot, generation) or self.gateway is None:
            raise GatewayError("shutter_unavailable")
        try:
            await self.gateway.command(slot, action)
        finally:
            await self.async_refresh()

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self.gateway is not None:
            self.gateway.on_disconnect = None
            await self.gateway.close()
            self.gateway = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Rename previous default titles; retain names chosen by the user.
    if entry.unique_id and entry.title in (
        f"X2D USB {entry.unique_id[-6:]}", f"X2D USB Gateway {entry.unique_id[-6:]}"
    ):
        hass.config_entries.async_update_entry(entry, title=default_title(entry.unique_id))
    coordinator = GatewayCoordinator(hass, entry)
    entry.runtime_data = coordinator
    try:
        await coordinator.async_config_entry_first_refresh()
        # One owner for the gateway device; platforms only reference its identifier.
        coordinator.device_id = device_registry.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, coordinator.info["device_id"])},
            name=entry.title, manufacturer="ha-x2d", model="X2D Gateway",
            sw_version=coordinator.info["firmware"],
        ).id
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_shutdown()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
        return True
    return False
