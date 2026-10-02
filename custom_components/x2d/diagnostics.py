"""No USB paths, radio identities/counters, sessions or journal identifiers."""

from homeassistant.components.diagnostics import async_redact_data


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    coordinator = entry.runtime_data
    return async_redact_data({
        "connected": coordinator.last_update_success,
        "hardware": "YD-RP2040 / CC1101",
        "info": coordinator.info,
        "status": coordinator.data,
    }, {"device_id", "session", "generation", "identity", "counter"})
