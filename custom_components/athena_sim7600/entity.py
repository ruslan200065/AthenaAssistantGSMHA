"""Shared entity base for Athena SIM7600."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import AthenaSim7600Coordinator


class AthenaSim7600Entity(CoordinatorEntity[AthenaSim7600Coordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: AthenaSim7600Coordinator) -> None:
        super().__init__(coordinator)
        self._entry = coordinator.entry

    @property
    def device_info(self) -> DeviceInfo:
        modem = (self.coordinator.data or {}).get("modem") or {}
        gateway = (self.coordinator.data or {}).get("gateway") or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Athena GSM",
            manufacturer=MANUFACTURER,
            model=modem.get("model") or "SIM7600",
            sw_version=modem.get("firmware") or gateway.get("version"),
            serial_number=modem.get("imei"),
        )

    @property
    def modem(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("modem") or {}


    @property
    def athena(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("athena") or {}
