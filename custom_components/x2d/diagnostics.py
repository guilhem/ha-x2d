"""No connection addresses, radio identities/counters, sessions or journal IDs."""

from homeassistant.components.diagnostics import async_redact_data


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    coordinator = entry.runtime_data
    return async_redact_data({
        "connected": coordinator.last_update_success,
        "info": coordinator.info,
        "status": coordinator.data,
    }, {"device_id", "session", "generation", "identity", "counter"})
