"""Serial PCM transport and voice session wiring (no hardware)."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.config import AudioBridgeConfig  # noqa: E402
from sim7600_gateway.pcm_serial import SerialPcm  # noqa: E402
from sim7600_gateway.voice_call import CallVoiceSession  # noqa: E402


class FakeSerial:
    instances: list[FakeSerial] = []

    def __init__(self, port, baudrate, **kwargs):
        self.port = port
        self.baudrate = baudrate
        self.incoming = [b"\x00" * 3200]
        self.written = bytearray()
        self.closed = False
        FakeSerial.instances.append(self)

    def read(self, size: int) -> bytes:
        if self.closed:
            return b""
        if self.incoming:
            return self.incoming.pop(0)
        time.sleep(0.01)
        return b""

    def write(self, data: bytes) -> int:
        if self.closed:
            raise OSError("closed")
        self.written.extend(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_serial(monkeypatch):
    module = types.ModuleType("serial")
    module.Serial = FakeSerial
    monkeypatch.setitem(sys.modules, "serial", module)
    FakeSerial.instances.clear()
    return module


@pytest.mark.asyncio
async def test_serial_transport_reads_and_writes(fake_serial) -> None:
    pcm = SerialPcm("/dev/fake-pcm", 115200)
    await pcm.open()
    frame = await asyncio.wait_for(pcm.read_frame(640), 2)
    assert frame == b"\x00" * 640
    await pcm.write(b"b" * 640)
    assert bytes(FakeSerial.instances[0].written) == b"b" * 640
    await pcm.close()
    assert FakeSerial.instances[0].closed


@pytest.mark.asyncio
async def test_voice_session_starts_with_serial(fake_serial, monkeypatch) -> None:
    monkeypatch.setattr(os.path, "exists", lambda path: path == "/dev/fake-pcm")
    events: list[dict] = []
    binary: list[bytes] = []

    async def send_json(frame: dict) -> bool:
        events.append(frame)
        return True

    async def send_binary(frame: bytes) -> None:
        binary.append(frame)

    config = AudioBridgeConfig(
        enabled=True,
        pcm_port="/dev/fake-pcm",
        audio_transport="auto",
    )
    session = CallVoiceSession(config, send_json, send_binary)
    assert await session.start()
    assert session.status["transport"] == "serial"
    await asyncio.sleep(0.05)
    await session.stop()
    assert not session.active
    assert not events
