"""Native port selection, precise USB discovery and physical identity checking."""

import re

import voluptuous as vol

from homeassistant.components import usb
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_DEVICE
from homeassistant.helpers.selector import SerialPortSelector
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from . import DOMAIN
from ._client import Gateway, ProtocolError


class X2DConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _usb_device: str
    _usb_identity: str

    async def _identify(self, device: str, expected_id: str | None = None) -> tuple[str, dict]:
        device = await self.hass.async_add_executor_job(usb.get_serial_by_id, device)
        gateway = await Gateway.open(device, expected_device_id=expected_id)
        try:
            return device, gateway.info
        finally:
            await gateway.close()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_DEVICE: user_input[CONF_DEVICE]})
            try:
                device, info = await self._identify(user_input[CONF_DEVICE])
            except ProtocolError:
                errors["base"] = "invalid_gateway"
            except (OSError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(info["device_id"])
                self._abort_if_unique_id_configured(updates={CONF_DEVICE: device})
                return self.async_create_entry(
                    title=f"X2D USB {info['device_id'][-6:]}", data={CONF_DEVICE: device}
                )
        return self.async_show_form(
            step_id="user", errors=errors,
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): SerialPortSelector()}),
        )

    async def async_step_usb(self, discovery_info: UsbServiceInfo) -> ConfigFlowResult:
        serial = (discovery_info.serial_number or "").upper()
        if re.fullmatch(r"[0-9A-F]{16}", serial) is None:
            return self.async_abort(reason="invalid_gateway")
        self._usb_device = await self.hass.async_add_executor_job(
            usb.get_serial_by_id, discovery_info.device
        )
        self._usb_identity = serial
        await self.async_set_unique_id(serial)
        self._abort_if_unique_id_configured(updates={CONF_DEVICE: self._usb_device})
        self.context["title_placeholders"] = {"name": f"X2D USB {serial[-6:]}"}
        return await self.async_step_usb_confirm()

    async def async_step_usb_confirm(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                device, info = await self._identify(self._usb_device, self._usb_identity)
            except ProtocolError:
                errors["base"] = "invalid_gateway"
            except (OSError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                self._abort_if_unique_id_configured(updates={CONF_DEVICE: device})
                return self.async_create_entry(
                    title=f"X2D USB {info['device_id'][-6:]}", data={CONF_DEVICE: device}
                )
        return self.async_show_form(
            step_id="usb_confirm", data_schema=vol.Schema({}), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            try:
                device, _ = await self._identify(user_input[CONF_DEVICE], entry.unique_id)
            except ProtocolError:
                errors["base"] = "invalid_gateway"
            except (OSError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_DEVICE: device})
        return self.async_show_form(
            step_id="reconfigure", errors=errors,
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): SerialPortSelector()}),
        )
