"""Config and options flow for Athena SIM7600."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult

from .api import GatewayApi, GatewayAuthError, GatewayError
from .const import (
    CONF_DEFAULT_NUMBER,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)


async def _validate(hass: HomeAssistant, url: str, token: str, verify_ssl: bool) -> None:
    api = GatewayApi(hass, url, token, verify_ssl)
    await api.status()


class AthenaSim7600ConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            try:
                await _validate(
                    self.hass,
                    url,
                    user_input[CONF_TOKEN],
                    user_input.get(CONF_VERIFY_SSL, True),
                )
            except GatewayAuthError:
                errors["base"] = "invalid_auth"
            except GatewayError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input.get("name") or "Athena GSM",
                    data={
                        CONF_URL: url,
                        CONF_TOKEN: user_input[CONF_TOKEN],
                        CONF_VERIFY_SSL: user_input.get(CONF_VERIFY_SSL, True),
                    },
                    options={
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                        CONF_DEFAULT_NUMBER: "",
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional("name", default="Athena GSM"): str,
                    vol.Required(CONF_URL, default="http://192.168.1.50:8099"): str,
                    vol.Required(CONF_TOKEN): str,
                    vol.Optional(CONF_VERIFY_SSL, default=True): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        errors: dict[str, str] = {}
        if entry is not None and user_input is not None:
            try:
                await _validate(
                    self.hass,
                    entry.data[CONF_URL],
                    user_input[CONF_TOKEN],
                    entry.data.get(CONF_VERIFY_SSL, True),
                )
            except GatewayError:
                errors["base"] = "invalid_auth"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_TOKEN: user_input[CONF_TOKEN]},
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
            description_placeholders={"url": entry.data.get(CONF_URL, "") if entry else ""},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AthenaSim7600OptionsFlow(config_entry)


class AthenaSim7600OptionsFlow(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry | None = None) -> None:
        # HA 2024.10+ injects ``self.config_entry``; older versions pass it here.
        self._fallback_entry = config_entry

    @property
    def _entry(self) -> ConfigEntry | None:
        return getattr(self, "config_entry", None) or self._fallback_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        entry = self._entry
        options = entry.options if entry is not None else {}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(int, vol.Range(min=5, max=300)),
                    vol.Optional(
                        CONF_DEFAULT_NUMBER,
                        default=options.get(CONF_DEFAULT_NUMBER, ""),
                    ): str,
                }
            ),
        )
