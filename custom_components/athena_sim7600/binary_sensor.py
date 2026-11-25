"""Binary sensors for the Athena SIM7600 gateway."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AthenaSim7600Coordinator
from .entity import AthenaSim7600Entity


@dataclass(frozen=True, kw_only=True)
class AthenaBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[AthenaSim7600Coordinator], bool] = lambda _: False


BINARY_SENSORS: tuple[AthenaBinaryDescription, ...] = (
    AthenaBinaryDescription(
        key="online",
        name="Шлюз онлайн",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda c: bool((c.data or {}).get("modem", {}).get("online")),
    ),
    AthenaBinaryDescription(
        key="registered",
        name="Зарегистрирован в сети",
        translation_key="registered",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:signal-4g",
        value_fn=lambda c: bool((c.data or {}).get("modem", {}).get("registered")),
    ),
    AthenaBinaryDescription(
        key="call_active",
        name="Идёт звонок",
        translation_key="call_active",
        device_class=BinarySensorDeviceClass.SOUND,
        icon="mdi:phone-in-talk",
        value_fn=lambda c: bool((c.data or {}).get("modem", {}).get("call_active")),
    ),
    AthenaBinaryDescription(
        key="ringing",
        name="Входящий звонок",
        translation_key="ringing",
        device_class=BinarySensorDeviceClass.SOUND,
        icon="mdi:phone-ring",
        value_fn=lambda c: bool((c.data or {}).get("modem", {}).get("ringing")),
    ),
    AthenaBinaryDescription(
        key="athena_connected",
        name="Athena подключена",
        translation_key="athena_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda c: bool((c.data or {}).get("athena", {}).get("connected")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AthenaSim7600Coordinator = entry.runtime_data.coordinator
    async_add_entities(
        AthenaBinarySensor(coordinator, description) for description in BINARY_SENSORS
    )


class AthenaBinarySensor(AthenaSim7600Entity, BinarySensorEntity):
    entity_description: AthenaBinaryDescription

    def __init__(
        self,
        coordinator: AthenaSim7600Coordinator,
        description: AthenaBinaryDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_translation_key = description.translation_key

    @property
    def is_on(self) -> bool:
        try:
            return bool(self.entity_description.value_fn(self.coordinator))
        except (TypeError, KeyError):
            return False

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "ringing":
            return None
        caller = self.modem.get("caller") or (self.coordinator.last_call or {}).get("number")
        return {"caller": caller} if caller else None
