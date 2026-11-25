"""Sensors for the Athena SIM7600 gateway."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AthenaSim7600Coordinator
from .entity import AthenaSim7600Entity


@dataclass(frozen=True, kw_only=True)
class AthenaSensorDescription(SensorEntityDescription):
    value_key: str = ""
    nested: str = "modem"


SENSORS: tuple[AthenaSensorDescription, ...] = (
    AthenaSensorDescription(
        key="state",
        name="Состояние модема",
        translation_key="modem_state",
        icon="mdi:sim",
        value_key="state",
    ),
    AthenaSensorDescription(
        key="signal_dbm",
        name="Уровень сигнала",
        translation_key="signal_dbm",
        icon="mdi:signal",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="signal_dbm",
    ),
    AthenaSensorDescription(
        key="signal_percent",
        name="Сигнал",
        translation_key="signal_percent",
        icon="mdi:signal",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="signal_percent",
    ),
    AthenaSensorDescription(
        key="operator",
        name="Оператор",
        translation_key="operator",
        icon="mdi:radio-tower",
        value_key="operator",
    ),
    AthenaSensorDescription(
        key="network",
        name="Сеть",
        translation_key="network",
        icon="mdi:network",
        value_key="network",
    ),
    AthenaSensorDescription(
        key="registration",
        name="Регистрация",
        translation_key="registration",
        icon="mdi:check-network",
        value_key="registration_text",
    ),
    AthenaSensorDescription(
        key="sim_state",
        name="SIM",
        translation_key="sim_state",
        icon="mdi:sim-outline",
        value_key="sim_state",
    ),
    AthenaSensorDescription(
        key="model",
        name="Модель",
        translation_key="model",
        icon="mdi:chip",
        value_key="model",
        entity_registry_enabled_default=False,
    ),
    AthenaSensorDescription(
        key="firmware",
        name="Прошивка",
        translation_key="firmware",
        icon="mdi:package-variant",
        value_key="firmware",
        entity_registry_enabled_default=False,
    ),
    AthenaSensorDescription(
        key="imei",
        name="IMEI",
        translation_key="imei",
        icon="mdi:identifier",
        value_key="imei",
        entity_registry_enabled_default=False,
    ),
    AthenaSensorDescription(
        key="iccid",
        name="ICCID",
        translation_key="iccid",
        icon="mdi:card-account-details",
        value_key="iccid",
        entity_registry_enabled_default=False,
    ),
    AthenaSensorDescription(
        key="phone_number",
        name="Номер SIM",
        translation_key="phone_number",
        icon="mdi:phone",
        value_key="phone_number",
        entity_registry_enabled_default=False,
    ),
    AthenaSensorDescription(
        key="uptime",
        name="Время работы",
        translation_key="uptime",
        icon="mdi:timer-outline",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_key="uptime_sec",
    ),
    AthenaSensorDescription(
        key="sms_sent",
        name="SMS отправлено",
        translation_key="sms_sent",
        icon="mdi:message-arrow-right",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_key="sms_sent",
    ),
    AthenaSensorDescription(
        key="sms_received",
        name="SMS получено",
        translation_key="sms_received",
        icon="mdi:message-arrow-left",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_key="sms_received",
    ),
    AthenaSensorDescription(
        key="last_sms",
        name="Последняя SMS",
        translation_key="last_sms",
        icon="mdi:message-text-clock",
        value_key="last_sms",
        nested="event",
    ),
    AthenaSensorDescription(
        key="athena_connection",
        name="Athena",
        translation_key="athena_connection",
        icon="mdi:home-assistant",
        value_key="connected",
        nested="athena",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AthenaSim7600Coordinator = entry.runtime_data.coordinator
    async_add_entities(AthenaModemSensor(coordinator, description) for description in SENSORS)


class AthenaModemSensor(AthenaSim7600Entity, SensorEntity):
    entity_description: AthenaSensorDescription

    def __init__(
        self,
        coordinator: AthenaSim7600Coordinator,
        description: AthenaSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_translation_key = description.translation_key

    @property
    def native_value(self) -> Any:
        description = self.entity_description
        if description.nested == "event":
            sms = self.coordinator.last_sms or {}
            text = sms.get("text") or ""
            return text[:250] if text else None
        if description.nested == "athena":
            return "подключено" if self.athena.get(description.value_key) else "нет связи"
        value = self.modem.get(description.value_key)
        if isinstance(value, bool):
            return "да" if value else "нет"
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "last_sms":
            return None
        sms = self.coordinator.last_sms or {}
        if not sms:
            return None
        return {
            "number": sms.get("number"),
            "at": sms.get("at"),
        }
