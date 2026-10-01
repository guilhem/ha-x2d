"""Native port selection, precise USB discovery and physical identity checking."""

import re

import voluptuous as vol

from homeassistant.components import usb
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, ConfigSubentryFlow
from homeassistant.const import CONF_DEVICE
from homeassistant.helpers.selector import SerialPortSelector, SelectSelector, SelectSelectorConfig
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from . import DOMAIN
from ._client import Gateway, GatewayError, ProtocolError


class X2DConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    @classmethod
    def async_get_supported_subentry_types(cls, config_entry):
        return {"shutter": ShutterFlow}

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
            except (OSError, TimeoutError, ValueError, GatewayError):
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
            except (OSError, TimeoutError, ValueError, GatewayError):
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
            except (OSError, TimeoutError, ValueError, GatewayError):
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICE: device})
                return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(
            step_id="reconfigure", errors=errors,
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): SerialPortSelector()}),
        )


# Well'book pp. 87/89 (S03/S04 in docs/DOSSIER_TECHNIQUE.md).
# Physical prerequisites do not qualify the firmware's radio profile.
ENROLLMENT_INSTRUCTIONS: dict[str, str] = {
    "en": (
        "For the observed Franciasoft / Well'com motor only. Use its original "
        "controller identified as the BASE transmitter (one mechanical acknowledgment).\n\n"
        "1. Set the existing BASE controller to normal mode N.\n"
        "2. Hold STOP until the motor gives a mechanical acknowledgment, then release.\n"
        "3. Within 1 minute of that acknowledgment, confirm below to enroll the "
        "gateway's new identity C only. If the minute has elapsed, repeat steps 1 and 2.\n\n"
        "Keep the shutter in view. Do not reset the motor or remove existing controllers."
    ),
    "fr": (
        "Uniquement pour le moteur Franciasoft / Well'com observé. Utilisez sa "
        "télécommande d’origine identifiée comme émetteur de BASE (un accusé mécanique).\n\n"
        "1. Placez la télécommande de BASE existante en mode normal N.\n"
        "2. Maintenez STOP jusqu’à l’accusé mécanique du moteur, puis relâchez.\n"
        "3. Dans la minute suivant cet accusé, confirmez ci-dessous pour inscrire "
        "uniquement la nouvelle identité C de la passerelle. Si la minute est écoulée, "
        "reprenez les étapes 1 et 2.\n\n"
        "Gardez le volet sous surveillance. Ne réinitialisez pas le moteur et ne "
        "supprimez pas les télécommandes existantes."
    ),
}


def _flow_error(exc: Exception) -> str:
    if isinstance(exc, GatewayError):
        if exc.code in {"profile_unverified", "storage_corrupt", "storage_full", "counter_exhausted", "not_paired", "command_uncertain", "instructions_unavailable", "shutter_unavailable"}:
            return exc.code
        return "tx_failed"
    if isinstance(exc, ProtocolError):
        return "invalid_gateway"
    return "cannot_connect"


class ShutterFlow(ConfigSubentryFlow):
    """Provision → enrollment → durable pairing → explicit test → observation."""

    def __init__(self):
        self._slot: int | None = None
        self._generation: str | None = None
        self._name = ""
        self._emitted = False
        self._test_emitted = False

    async def _coordinator(self):
        entry = self._get_entry()
        coordinator = entry.runtime_data
        await coordinator.async_refresh()
        if not coordinator.last_update_success or coordinator.gateway is None:
            raise ConnectionError("Gateway unavailable")
        return coordinator

    def _used(self, generation):
        return {s.data["shutter_id"] for s in self._get_entry().subentries.values()
                if s.subentry_type == "shutter" and s.data["state_generation"] == generation}

    def _instructions(self):
        language = self.hass.config.language
        return ENROLLMENT_INSTRUCTIONS.get(language, ENROLLMENT_INSTRUCTIONS.get("en", ""))

    async def async_step_user(self, user_input=None):
        errors = {}
        try:
            coordinator = await self._coordinator()
            data = coordinator.data
            capabilities = coordinator.info["capabilities"]
            can_provision = "provision" in capabilities
            paired = {s["shutter_id"]: s for s in data["shutters"] if s["state"] == "paired"}
            if not can_provision and "command" not in capabilities:
                return self.async_abort(reason="profile_unverified")
            if data["storage"]["state"] in ("corrupt", "full"):
                return self.async_abort(reason=f"storage_{data['storage']['state']}")
            used = self._used(data["generation"])
            available = [slot for slot in range(1, coordinator.info["max_shutters"] + 1)
                         if slot not in used and (can_provision or slot in paired)]
            if not available:
                return self.async_abort(
                    reason="no_slots" if can_provision else "enrollment_unavailable"
                )
            if user_input is not None:
                slot = user_input["shutter_id"]
                if type(slot) is not int or slot not in available:
                    return self.async_abort(reason="already_configured")
                record = ({**paired[slot], "generation": data["generation"]} if slot in paired
                          else await coordinator.gateway.provision(slot))
                if data["generation"] is not None and record["generation"] != data["generation"]:
                    return self.async_abort(reason="generation_changed")
                self._slot = slot
                self._generation = record["generation"]
                self._name = user_input["name"]
                if record["state"] == "paired":
                    return await self.async_step_test()
                return await self.async_step_enroll()
        except (GatewayError, OSError, TimeoutError, ValueError) as exc:
            errors["base"] = _flow_error(exc)
            available = list(range(1, 17))
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required("name"): vol.All(str, vol.Length(min=1, max=100)),
            vol.Required("shutter_id", default=available[0]): vol.In(available),
        }))

    async def _check_reference(self, *, refresh=True):
        coordinator = await self._coordinator() if refresh else self._get_entry().runtime_data
        if not coordinator.last_update_success or not coordinator.data:
            raise GatewayError("shutter_unavailable")
        if coordinator.data["generation"] != self._generation:
            raise GatewayError("generation_changed")
        if self._slot in self._used(self._generation):
            raise GatewayError("already_configured")
        return coordinator

    async def async_step_enroll(self, user_input=None):
        errors = {}
        if user_input is not None:
            if not user_input.get("physical_ready"):
                errors["base"] = "physical_ready_required"
            else:
                try:
                    coordinator = await self._check_reference()
                    if not coordinator.data["tx_enabled"] or "pair" not in coordinator.info["capabilities"]:
                        raise GatewayError("profile_unverified")
                    if not self._instructions():
                        raise GatewayError("instructions_unavailable")
                    await coordinator.gateway.pair(self._slot)
                except GatewayError as exc:
                    if exc.code in {"generation_changed", "already_configured"}:
                        return self.async_abort(reason=exc.code)
                    errors["base"] = _flow_error(exc)
                except (OSError, TimeoutError, ValueError) as exc:
                    errors["base"] = _flow_error(exc)
                else:
                    self._emitted = True
                    return await self.async_step_confirm()
        return self.async_show_form(step_id="enroll", errors=errors,
            description_placeholders={"instructions": self._instructions()},
            data_schema=vol.Schema({vol.Required("physical_ready", default=False): bool}))

    async def async_step_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            if not self._emitted or not user_input.get("motor_response"):
                errors["base"] = "motor_response_required"
            else:
                try:
                    coordinator = await self._check_reference()
                    record = await coordinator.gateway.confirm(self._slot)
                    if record["generation"] != self._generation:
                        return self.async_abort(reason="generation_changed")
                    await coordinator.async_refresh()
                except GatewayError as exc:
                    if exc.code in {"generation_changed", "already_configured"}:
                        return self.async_abort(reason=exc.code)
                    errors["base"] = _flow_error(exc)
                except (OSError, TimeoutError, ValueError) as exc:
                    errors["base"] = _flow_error(exc)
                else:
                    return await self.async_step_test()
        return self.async_show_form(step_id="confirm", errors=errors,
            data_schema=vol.Schema({vol.Required("motor_response", default=False): bool}))

    async def async_step_test(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._test_emitted = False
            try:
                coordinator = await self._check_reference(refresh=False)
                await coordinator.async_command(self._slot, self._generation, user_input["action"])
            except GatewayError as exc:
                if exc.code in {"generation_changed", "already_configured"}:
                    return self.async_abort(reason=exc.code)
                errors["base"] = _flow_error(exc)
            except (OSError, TimeoutError, ValueError) as exc:
                errors["base"] = _flow_error(exc)
            else:
                self._test_emitted = True
                return await self.async_step_test_confirm()
        return self.async_show_form(step_id="test", errors=errors,
            data_schema=vol.Schema({vol.Required("action"): SelectSelector(
                SelectSelectorConfig(options=["open", "stop", "close"], translation_key="shutter_action"))}))

    async def async_step_test_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("test_again"):
                self._test_emitted = False
                return await self.async_step_test()
            if not self._test_emitted or not user_input.get("test_observed"):
                errors["base"] = "test_observed_required"
            else:
                try:
                    coordinator = await self._check_reference()
                    if not coordinator.ready(self._slot, self._generation):
                        raise GatewayError("shutter_unavailable")
                except GatewayError as exc:
                    if exc.code in {"generation_changed", "already_configured"}:
                        return self.async_abort(reason=exc.code)
                    errors["base"] = _flow_error(exc)
                except (OSError, TimeoutError, ValueError) as exc:
                    errors["base"] = _flow_error(exc)
                else:
                    return self.async_create_entry(title=self._name,
                        unique_id=f"{self._generation}_{self._slot}",
                        data={"shutter_id": self._slot, "state_generation": self._generation})
        return self.async_show_form(step_id="test_confirm", errors=errors,
            data_schema=vol.Schema({vol.Required("test_observed", default=False): bool,
                                   vol.Optional("test_again", default=False): bool}))
