"""Athena SIM7600 GSM gateway integration for Home Assistant."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import GatewayApi
from .const import (
    DOMAIN,
    EVENT_CALL,
    EVENT_CALL_STATE,
    EVENT_GATEWAY,
    EVENT_MOTION,
    EVENT_SMS,
    PLATFORMS,
)
from .coordinator import AthenaSim7600Coordinator
from .services import async_setup_services


@dataclass
class RuntimeData:
    api: GatewayApi
    coordinator: AthenaSim7600Coordinator
    events_task: asyncio.Task | None = None


type AthenaConfigEntry = ConfigEntry


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    await async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AthenaConfigEntry) -> bool:
    data = entry.data
    api = GatewayApi(
        hass,
        data["url"],
        data["token"],
        data.get("verify_ssl", True),
    )
    coordinator = AthenaSim7600Coordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    runtime = RuntimeData(api=api, coordinator=coordinator)
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.events_task = entry.async_create_background_task(
        hass,
        _event_loop(hass, runtime),
        name=f"{DOMAIN}_events",
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AthenaConfigEntry) -> bool:
    runtime: RuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is not None and runtime.events_task is not None:
        runtime.events_task.cancel()
        try:
            await runtime.events_task
        except asyncio.CancelledError:
            pass
        runtime.events_task = None
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: AthenaConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _event_loop(hass: HomeAssistant, runtime: RuntimeData) -> None:
    from .api import GatewayAuthError

    coordinator = runtime.coordinator
    entry = coordinator.entry
    while True:
        try:
            async for event in runtime.api.events():
                coordinator.note_event(event)
                hass.bus.async_fire(EVENT_GATEWAY, event)
                event_type = event.get("type")
                data = event.get("data") or {}
                if event_type == "sms.incoming":
                    hass.bus.async_fire(EVENT_SMS, data)
                elif event_type == "call.incoming":
                    hass.bus.async_fire(EVENT_CALL, data)
                elif event_type == "call.state":
                    hass.bus.async_fire(EVENT_CALL_STATE, data)
                elif event_type == "camera.motion":
                    hass.bus.async_fire(EVENT_MOTION, data)
                if event_type in ("call.incoming", "call.state", "sms.incoming", "sms.sent", "modem.ready"):
                    try:
                        await coordinator.async_request_refresh()
                    except Exception:  # noqa: BLE001 - a failed poll must not drop SSE
                        coordinator.last_update_success = False
        except asyncio.CancelledError:
            raise
        except GatewayAuthError:
            entry.async_start_reauth(hass)
            await asyncio.sleep(60)
        except Exception:  # noqa: BLE001 - reconnect forever
            await asyncio.sleep(5)
