"""Diagnostics support for Athena SIM7600."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from .api import GatewayError
from .const import CONF_TOKEN

TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    data: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "status": runtime.coordinator.data if runtime else None,
    }
    if runtime is not None:
        try:
            data["events"] = await runtime.api.recent_events(30)
        except GatewayError as exc:
            data["events_error"] = str(exc)
    return data
