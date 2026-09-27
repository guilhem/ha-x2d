"""Export state without USB paths, persistent IDs or boot session identifiers."""

from homeassistant.components.diagnostics import async_redact_data


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    coordinator = entry.runtime_data
    return {
        "connected": coordinator.last_update_success,
        "info": async_redact_data(coordinator.info, {"device_id", "session"}),
        "status": coordinator.data,
    }
