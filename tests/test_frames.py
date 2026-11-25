"""Protocol frame builders."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.athena import frames  # noqa: E402


def test_hello() -> None:
    frame = frames.hello("dev_test", room_hint="Прихожая", ip="192.168.1.5")
    assert frame["type"] == "hello"
    assert frame["token"] == "dev_test"
    assert frame["kind"] == "gsm"
    assert "sms" in frame["capabilities"]
    assert frame["firmware"].startswith("sim7600-gateway/")


def test_heartbeat() -> None:
    frame = frames.heartbeat("degraded", None, 120)
    assert frame["status"] == "degraded"
    assert frame["rssi"] == -113
    assert frames.heartbeat("online", -69, 10)["rssi"] == -69


def test_command_ack() -> None:
    frame = frames.command_ack(12, "failed", "boom")
    assert frame == {"type": "command.ack", "id": 12, "status": "failed", "error": "boom"}
    assert "error" not in frames.command_ack(13, "done")


def test_events_are_flat() -> None:
    assert frames.sms_incoming("+7999", "x" * 3000)["text"] == "x" * 2000
    incoming = frames.call_incoming("+79991234567", "2026-01-01T00:00:00Z")
    assert incoming["type"] == "call.incoming"
    assert incoming["number"] == "+79991234567"
    assert frames.call_state("ended", "+79991234567", 12)["duration_sec"] == 12
    assert frames.dtmf("5") == {"type": "dtmf", "digit": "5"}
    assert frames.device_event("ussd.result", {"code": "*100#"})["event"] == "ussd.result"
    bridge = frames.call_bridge(room="Кухня", call_number="+79991234567")
    assert bridge["room"] == "Кухня"
    assert bridge["type"] == "call.bridge"
    assert "target_endpoint_id" not in bridge


def test_diag_frame() -> None:
    frame = frames.diag_modem({"signal_dbm": -65, "registration_status": 1, "uptime_sec": 5})
    assert frame["type"] == "device.event"
    assert frame["event"] == "diag.modem"
    assert frame["data"]["csq"] == -65
