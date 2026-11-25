"""Data coordinator for the Athena SIM7600 integration."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GatewayApi, GatewayError
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

import logging

_LOGGER = logging.getLogger(__name__)


class AthenaSim7600Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: GatewayApi):
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.entry_id}",
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
        )
        self.api = api
        self.entry = entry
        self.last_motion: float | None = None
        self.last_motion_info: dict[str, Any] = {}
        self.last_sms: dict[str, Any] = {}
        self.last_call: dict[str, Any] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.api.status()
        except GatewayError as exc:
            raise UpdateFailed(str(exc)) from exc

    def note_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        data = event.get("data") or {}
        if event_type == "camera.motion":
            self.last_motion = time.time()
            self.last_motion_info = data
        elif event_type == "sms.incoming":
            self.last_sms = data
        elif event_type == "call.incoming":
            self.last_call = data

    @property
    def motion_active(self) -> bool:
        from .const import MOTION_HOLD_SECONDS

        return bool(self.last_motion and time.time() - self.last_motion < MOTION_HOLD_SECONDS)
