"""Home Assistant services for the gateway."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from .api import GatewayError
from .const import (
    ATTR_ENTRY_ID,
    DOMAIN,
    SERVICE_ANSWER,
    SERVICE_BRIDGE,
    SERVICE_DIAL,
    SERVICE_HANGUP,
    SERVICE_RESET,
    SERVICE_SEND_SMS,
    SERVICE_SEND_USSD,
)

SERVICES = (
    SERVICE_SEND_SMS,
    SERVICE_SEND_USSD,
    SERVICE_DIAL,
    SERVICE_ANSWER,
    SERVICE_HANGUP,
    SERVICE_RESET,
    SERVICE_BRIDGE,
)


def _entries(hass: HomeAssistant) -> list:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if getattr(entry, "runtime_data", None)
    ]


def _runtime(hass: HomeAssistant, call: ServiceCall):
    entry_id = call.data.get(ATTR_ENTRY_ID)
    entries = _entries(hass)
    if entry_id:
        entries = [entry for entry in entries if entry.entry_id == entry_id]
    if not entries:
        raise HomeAssistantError("Athena SIM7600 gateway is not loaded")
    return entries[0].runtime_data


async def async_setup_services(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_services_registered"):
        return
    hass.data[f"{DOMAIN}_services_registered"] = True

    schema_base = {vol.Optional(ATTR_ENTRY_ID): str}

    async def handle_send_sms(call: ServiceCall) -> None:
        runtime = _runtime(hass, call)
        try:
            await runtime.api.send_sms(call.data["number"], call.data["text"])
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_send_ussd(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass, call)
        try:
            return await runtime.api.send_ussd(call.data["code"])
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_dial(call: ServiceCall) -> None:
        runtime = _runtime(hass, call)
        try:
            await runtime.api.dial(call.data["number"])
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_answer(call: ServiceCall) -> None:
        runtime = _runtime(hass, call)
        try:
            await runtime.api.answer()
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_hangup(call: ServiceCall) -> None:
        runtime = _runtime(hass, call)
        try:
            await runtime.api.hangup()
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_reset(call: ServiceCall) -> None:
        runtime = _runtime(hass, call)
        try:
            await runtime.api.reset_modem()
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def handle_bridge(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass, call)
        room = call.data.get("room") or None
        endpoint_id = call.data.get("endpoint_id") or None
        try:
            return await runtime.api.bridge_call(room, endpoint_id)
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_SMS,
        handle_send_sms,
        schema=vol.Schema(
            {
                **schema_base,
                vol.Required("number"): str,
                vol.Required("text"): vol.All(str, vol.Length(min=1, max=1000)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_USSD,
        handle_send_ussd,
        schema=vol.Schema({**schema_base, vol.Required("code"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DIAL,
        handle_dial,
        schema=vol.Schema({**schema_base, vol.Required("number"): str}),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ANSWER, handle_answer, schema=vol.Schema(schema_base)
    )
    hass.services.async_register(
        DOMAIN, SERVICE_HANGUP, handle_hangup, schema=vol.Schema(schema_base)
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET, handle_reset, schema=vol.Schema(schema_base)
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BRIDGE,
        handle_bridge,
        schema=vol.Schema(
            {
                **schema_base,
                vol.Optional("room"): str,
                vol.Optional("endpoint_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove global services when the last config entry is unloaded."""
    for service in SERVICES:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
    hass.data.pop(f"{DOMAIN}_services_registered", None)
