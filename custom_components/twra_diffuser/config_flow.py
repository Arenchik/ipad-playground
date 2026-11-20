"""Config flow for the TWRA Aroma Diffuser."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from bleak.exc import BleakError
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME, CONF_PASSWORD
from homeassistant.core import callback

from .const import CONF_KEEP_CONNECTED, DEFAULT_KEEP_CONNECTED, DOMAIN
from .twra import InvalidPassword, PasswordRequired, TwraClient, TwraError

_LOGGER = logging.getLogger(__name__)

NAME_PREFIX = "TWRA"


class TwraConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._candidates: dict[str, BluetoothServiceInfoBleak] = {}
        self._name: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TwraOptionsFlow:
        return TwraOptionsFlow()

    def _default_name(self) -> str:
        if (self.hass.config.language or "").startswith("ru"):
            return "Аромадиффузор"
        return "Aroma Diffuser"

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_confirm()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._discovery = self._candidates[user_input[CONF_ADDRESS]]
            await self.async_set_unique_id(self._discovery.address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_confirm()

        configured = self._async_current_ids(include_ignore=False)
        self._candidates = {
            info.address: info
            for info in async_discovered_service_info(self.hass, connectable=True)
            if (info.name or "").upper().startswith(NAME_PREFIX) and info.address not in configured
        }
        if not self._candidates:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {addr: f"{info.name} ({addr})" for addr, info in self._candidates.items()}
                    )
                }
            ),
        )

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._discovery is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            self._name = user_input[CONF_NAME]
            result = await self._async_try_connect(None)
            if result == "password_required":
                return await self.async_step_password()
            if result is None:
                return self._create_entry(None)
            errors["base"] = result
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME, default=self._name or self._default_name()): str}
            ),
            description_placeholders={"name": self._discovery.name},
            errors=errors,
        )

    async def async_step_password(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_try_connect(user_input[CONF_PASSWORD])
            if result is None:
                return self._create_entry(user_input[CONF_PASSWORD])
            errors["base"] = "invalid_password" if result == "password_required" else result
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): vol.All(str, vol.Length(4, 4))}),
            errors=errors,
        )

    async def _async_try_connect(self, password: str | None) -> str | None:
        """Open one session to prove the device answers; return an error key."""
        assert self._discovery is not None
        client = TwraClient(self._discovery.device, password=password, keep_connected=True)
        try:
            await client.connect()
        except (PasswordRequired, InvalidPassword):
            return "password_required"
        except (BleakError, TimeoutError, TwraError) as err:
            _LOGGER.debug("Connection test failed: %s", err)
            return "cannot_connect"
        finally:
            await client.disconnect()
        return None

    def _create_entry(self, password: str | None) -> ConfigFlowResult:
        assert self._discovery is not None
        data = {CONF_ADDRESS: self._discovery.address}
        if password:
            data[CONF_PASSWORD] = password
        return self.async_create_entry(
            title=self._name or self._default_name(),
            data=data,
            options={CONF_KEEP_CONNECTED: DEFAULT_KEEP_CONNECTED},
        )


class TwraOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_KEEP_CONNECTED, DEFAULT_KEEP_CONNECTED)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_KEEP_CONNECTED, default=current): bool}),
        )
