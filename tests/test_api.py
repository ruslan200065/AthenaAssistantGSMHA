"""REST API tests in mock mode."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from sim7600_gateway.app import create_app  # noqa: E402
from sim7600_gateway.config import Config  # noqa: E402

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(tmp_path):
    config = Config()
    config.bridge.api_token = TOKEN
    config.bridge.state_dir = str(tmp_path)
    config.athena.enabled = False
    config.modem.mock = True
    config.camera.enabled = False
    config.camera.mock = True
    config.modem.bridge_room = ""
    with TestClient(create_app(config)) as test_client:
        deadline = time.time() + 5
        while time.time() < deadline:
            response = test_client.get("/api/status", headers=HEADERS)
            if response.status_code == 200 and response.json()["modem"]["state"] not in ("off", "init"):
                break
            time.sleep(0.1)
        yield test_client


def test_health_is_public(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_status_requires_token(client) -> None:
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-Api-Token": TOKEN}).status_code == 200


def test_status_payload(client) -> None:
    payload = client.get("/api/status", headers=HEADERS).json()
    assert payload["modem"]["registered"] is True
    assert payload["modem"]["model"] == "SIM7600E-H"
    assert payload["gateway"]["mock"] is True
    assert payload["athena"]["connected"] is False


def test_send_sms(client) -> None:
    response = client.post(
        "/api/sms",
        headers=HEADERS,
        json={"number": "+79991234567", "text": "Привет"},
    )
    assert response.status_code == 200
    assert response.json()["parts"] == 1
    recent = client.get("/api/sms/recent", headers=HEADERS).json()
    assert recent[0]["direction"] == "out"


def test_ussd(client) -> None:
    response = client.post("/api/ussd", headers=HEADERS, json={"code": "*100#"})
    assert response.status_code == 200
    assert "Баланс" in response.json()["response"]


def test_simulated_incoming_call_creates_event(client) -> None:
    response = client.post("/api/dev/call", headers=HEADERS, json={"number": "+79995550011"})
    assert response.status_code == 200
    time.sleep(0.8)
    events = client.get("/api/events/recent", headers=HEADERS).json()
    assert any(event["type"] == "call.incoming" for event in events)
    calls = client.get("/api/calls/recent", headers=HEADERS).json()
    assert calls[0]["number"] == "+79995550011"


def test_diagnostic_at_allowlist(client) -> None:
    denied = client.post("/api/modem/at", headers=HEADERS, json={"command": "AT+CFUN=0"})
    assert denied.status_code == 403
    allowed = client.post("/api/modem/at", headers=HEADERS, json={"command": "AT+CSQ"})
    assert allowed.status_code == 200
    assert allowed.json()["ok"] is True


def test_unknown_route(client) -> None:
    assert client.get("/api/nope", headers=HEADERS).status_code == 404
