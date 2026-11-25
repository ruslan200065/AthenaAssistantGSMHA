"""Buttons for the Athena SIM7600 gateway."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import GatewayError
from .coordinator import AthenaSim7600Coordinator
from .entity import AthenaSim7600Entity


@dataclass(frozen=True, kw_only=True)
class AthenaButtonDescription(ButtonEntityDescription):
    action: Callable[[AthenaSim7600Coordinator], Awaitable[object]]


BUTTONS: tuple[AthenaButtonDescription, ...] = (
    AthenaButtonDescription(
        key="answer",
        name="Ответить на звонок",
        translation_key="answer",
        icon="mdi:phone-in-talk",
        action=lambda coordinator: coordinator.api.answer(),
    ),
    AthenaButtonDescription(
        key="hangup",
        name="Сбросить звонок",
        translation_key="hangup",
        icon="mdi:phone-hangup",
        action=lambda coordinator: coordinator.api.hangup(),
    ),
    AthenaButtonDescription(
        key="reset",
        name="Перезапустить модем",
        translation_key="reset",
        icon="mdi:restart",
        action=lambda coordinator: coordinator.api.reset_modem(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AthenaSim7600Coordinator = entry.runtime_data.coordinator
    async_add_entities(AthenaButton(coordinator, description) for description in BUTTONS)


class AthenaButton(AthenaSim7600Entity, ButtonEntity):
    entity_description: AthenaButtonDescription

    def __init__(
        self,
        coordinator: AthenaSim7600Coordinator,
        description: AthenaButtonDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_translation_key = description.translation_key

    async def async_press(self) -> None:
        try:
            await self.entity_description.action(self.coordinator)
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await self.coordinator.async_request_refresh()
