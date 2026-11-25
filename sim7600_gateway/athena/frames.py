"""Builders for device-protocol/1 frames. All functions return plain dicts."""

from __future__ import annotations

from typing import Any

from .. import PROTOCOL, __version__

GSM_CAPABILITIES = ["calls", "sms", "dtmf", "audio_in", "audio_out", "camera"]


def hello(
    token: str,
    *,
    firmware: str | None = None,
    hardware: str = "SIM7600",
    device_id: str = "sim7600-gateway",
    room_hint: str = "",
    ip: str = "",
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "hello",
        "token": token,
        "kind": "gsm",
        "firmware": (firmware or f"sim7600-gateway/{__version__}")[:64],
        "hardware": hardware[:64],
        "capabilities": (capabilities or GSM_CAPABILITIES)[:64],
        "device_id": device_id,
        "room_hint": room_hint,
        "ip": ip,
        "protocol": PROTOCOL,
    }


def heartbeat(
    status: str,
    rssi: int | None,
    uptime_sec: int,
    *,
    temperature_c: float | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "heartbeat",
        "status": "online" if status == "online" else "degraded",
        "rssi": -113 if rssi is None else max(-113, min(0, int(rssi))),
        "uptime_sec": max(0, int(uptime_sec)),
    }
    if temperature_c is not None:
        frame["temperature_c"] = round(float(temperature_c), 1)
    if ip:
        frame["ip"] = ip
    return frame


def command_ack(command_id: int, status: str = "done", error: str | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "command.ack", "id": int(command_id), "status": status}
    if error:
        frame["error"] = str(error)[:2000]
    return frame


def sms_incoming(number: str, text: str) -> dict[str, Any]:
    return {"type": "sms.incoming", "number": number, "text": text[:2000]}


def call_incoming(number: str, at: str, **extra: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "call.incoming", "number": number, "at": at}
    frame.update({k: v for k, v in extra.items() if v is not None})
    return frame


def call_state(state: str, number: str, duration_sec: int | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "call.state", "state": state, "number": number}
    if duration_sec is not None:
        frame["duration_sec"] = max(0, int(duration_sec))
    return frame


def dtmf(digit: str) -> dict[str, Any]:
    return {"type": "dtmf", "digit": digit[:1]}


def device_event(event: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "device.event", "event": event[:120], "data": data or {}}


def call_bridge(
    *,
    room: str = "",
    target_endpoint_id: str | None = None,
    endpoint: str | None = None,
    call_number: str | None = None,
) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "call.bridge"}
    if target_endpoint_id:
        frame["target_endpoint_id"] = target_endpoint_id
    elif room:
        frame["room"] = room
    if endpoint:
        frame["endpoint"] = endpoint
    if call_number:
        frame["call_number"] = call_number
    return frame


def diag_modem(snapshot: dict[str, Any]) -> dict[str, Any]:
    data = {
        "csq": snapshot.get("signal_dbm"),
        "creg": snapshot.get("registration_status"),
        "uptime": snapshot.get("uptime_sec"),
        "sim_state": snapshot.get("sim_state"),
        "operator": snapshot.get("operator"),
        "network": snapshot.get("network"),
        "model": snapshot.get("model"),
    }
    return device_event("diag.modem", data)
