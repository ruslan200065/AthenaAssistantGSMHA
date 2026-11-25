"""Notify entity: send SMS through the gateway."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import GatewayError
from .const import CONF_DEFAULT_NUMBER
from .coordinator import AthenaSim7600Coordinator
from .entity import AthenaSim7600Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AthenaSim7600Coordinator = entry.runtime_data.coordinator
    async_add_entities([AthenaSmsNotify(coordinator)])


class AthenaSmsNotify(AthenaSim7600Entity, NotifyEntity):
    _attr_translation_key = "sms"
    _attr_icon = "mdi:message-text"

    def __init__(self, coordinator: AthenaSim7600Coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_notify_sms"

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        number = (title or "").strip() or self.coordinator.entry.options.get(CONF_DEFAULT_NUMBER)
        if not number:
            raise HomeAssistantError(
                "Укажите номер в title или настройте default_sms_number в опциях интеграции"
            )
        if not message:
            raise HomeAssistantError("Пустое сообщение")
        try:
            await self.coordinator.api.send_sms(number, message[:1000])
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc
