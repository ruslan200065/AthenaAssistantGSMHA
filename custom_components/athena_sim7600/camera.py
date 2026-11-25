"""Camera entity for the Athena SIM7600 gateway."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AthenaSim7600Coordinator
from .entity import AthenaSim7600Entity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AthenaSim7600Coordinator = entry.runtime_data.coordinator
    async_add_entities([AthenaCamera(coordinator)])


class AthenaCamera(AthenaSim7600Entity, Camera):
    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: AthenaSim7600Coordinator) -> None:
        AthenaSim7600Entity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_camera"
        self._attr_name = "Камера"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        try:
            return await self.coordinator.api.snapshot()
        except Exception as exc:  # noqa: BLE001 - camera errors are non-fatal
            _LOGGER.debug("camera snapshot failed: %s", exc)
            return None

    async def stream_source(self) -> str | None:
        return self.coordinator.api.stream_url

    @property
    def available(self) -> bool:
        return bool(self.camera.get("available")) and self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        camera = self.camera
        return {
            "device": camera.get("device"),
            "resolution": f"{camera.get('width')}x{camera.get('height')}",
            "fps": camera.get("fps"),
            "frames": camera.get("frames"),
            "motion_detection": camera.get("motion_detection"),
            "last_frame_at": camera.get("last_frame_at"),
        }
