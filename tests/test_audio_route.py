"""Audio route selection: serial PCM first, ALSA fallback."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway import audio_bridge  # noqa: E402
from sim7600_gateway.audio_bridge import audio_devices, frame_bytes  # noqa: E402
from sim7600_gateway.config import AudioBridgeConfig  # noqa: E402


@pytest.mark.asyncio
async def test_serial_port_is_preferred(monkeypatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda path: path == "/dev/ttyUSB4")
    config = AudioBridgeConfig(enabled=True, pcm_port="/dev/ttyUSB4")
    ok, capture, playback, error = await audio_devices(config)
    assert ok and error is None
    assert capture == playback == "serial:/dev/ttyUSB4"


@pytest.mark.asyncio
async def test_serial_transport_requires_port(monkeypatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda path: False)
    config = AudioBridgeConfig(
        enabled=True,
        pcm_port="/dev/ttyUSB9",
        audio_transport="serial",
    )
    ok, capture, playback, error = await audio_devices(config)
    assert not ok
    assert capture is None and playback is None
    assert "serial PCM port not found" in (error or "")


@pytest.mark.asyncio
async def test_alsa_fallback_when_serial_missing(monkeypatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda path: False)
    monkeypatch.setattr(audio_bridge.shutil, "which", lambda name: None)
    config = AudioBridgeConfig(enabled=True, pcm_port="/dev/ttyUSB4")
    ok, _capture, _playback, error = await audio_devices(config)
    assert not ok
    assert "arecord/aplay" in (error or "")


@pytest.mark.asyncio
async def test_alsa_mode_skips_serial(monkeypatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda path: True)
    monkeypatch.setattr(audio_bridge.shutil, "which", lambda name: None)
    config = AudioBridgeConfig(
        enabled=True,
        pcm_port="/dev/ttyUSB4",
        audio_transport="alsa",
    )
    ok, _capture, _playback, error = await audio_devices(config)
    assert not ok
    assert "arecord/aplay" in (error or "")


def test_frame_size_is_640_bytes() -> None:
    assert frame_bytes(AudioBridgeConfig(sample_rate=16000, chunk_ms=20)) == 640
