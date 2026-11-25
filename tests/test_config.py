"""Configuration loading and validation."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.config import load_config, validate  # noqa: E402


def test_defaults(monkeypatch) -> None:
    for name in list(__import__("os").environ):
        if name.startswith(("ATHENA_", "MODEM_", "CAMERA_", "BRIDGE_", "API_TOKEN", "STATE_DIR")):
            monkeypatch.delenv(name, raising=False)
    config = load_config("does-not-exist.toml")
    assert config.modem.baudrate == 115200
    assert config.camera.width == 1280
    assert config.modem.init_sequence[0] == "ATE0"
    assert config.state_dir.name


def test_toml_and_env_override(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[bridge]
port = 7000
api_token = "secret"

[athena]
enabled = true
url = "ws://core:8000/api/device/ws"
token = "dev_abc"

[modem]
port = "/dev/ttyUSB3"
unknown_callers = "reject"

[camera]
width = 640
height = 480
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BRIDGE_PORT", "7100")
    monkeypatch.setenv("MODEM_MOCK", "true")
    config = load_config(path)
    assert config.bridge.port == 7100
    assert config.bridge.api_token == "secret"
    assert config.modem.port == "/dev/ttyUSB3"
    assert config.modem.mock is True
    assert config.camera.width == 640
    assert config.athena.token == "dev_abc"


def test_validation_warnings() -> None:
    config = load_config("missing.toml")
    config.athena.enabled = True
    config.athena.url = ""
    config.athena.token = "nope"
    config.bridge.api_token = "change-me-please"
    warnings = validate(config)
    assert any("athena.url" in warning for warning in warnings)
    assert any("dev_" in warning for warning in warnings)
    assert any("api_token" in warning for warning in warnings)
