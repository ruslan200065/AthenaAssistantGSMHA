"""Voice call helpers (VAD math and reply state machine)."""

from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.athena.client import http_base_url  # noqa: E402
from sim7600_gateway.config import AudioBridgeConfig  # noqa: E402
from sim7600_gateway.voice_call import CallVoiceSession, rms  # noqa: E402


def test_rms_silence_and_tone() -> None:
    silence = struct.pack("<320h", *([0] * 320))
    loud = struct.pack("<320h", *([1000] * 320))
    assert rms(silence) == 0
    assert rms(loud) == 1000


def test_http_base_url() -> None:
    assert http_base_url("ws://192.168.1.10:8000/api/device/ws") == "http://192.168.1.10:8000"
    assert http_base_url("wss://core.example/api/device/ws") == "https://core.example"
    assert http_base_url("") == ""


def test_reply_without_listen_ends_dialogue() -> None:
    async def scenario() -> None:
        sent: list[dict] = []

        async def send_json(frame: dict) -> bool:
            sent.append(frame)
            return True

        async def send_binary(frame: bytes) -> None:
            return None

        session = CallVoiceSession(AudioBridgeConfig(enabled=True), send_json, send_binary)
        session._active = True
        session._reply = {"text": "ok", "listen": False, "end": True}
        try:
            await session._after_playback()
            assert not session._listening
            assert sent[-1] == {"type": "conversation.end", "reason": "ended"}
        finally:
            await session.stop()

    asyncio.run(scenario())


def test_reply_with_listen_expects_follow_up() -> None:
    async def scenario() -> None:
        sent: list[dict] = []

        async def send_json(frame: dict) -> bool:
            sent.append(frame)
            return True

        async def send_binary(frame: bytes) -> None:
            return None

        session = CallVoiceSession(AudioBridgeConfig(enabled=True), send_json, send_binary)
        session._active = True
        session._reply = {
            "text": "Ещё что-нибудь?",
            "listen": True,
            "end": False,
            "follow_up_timeout_seconds": 8,
        }
        try:
            await session._after_playback()
            assert session._listening
            assert session._follow_up
            assert session._timeout_task is not None
        finally:
            await session.stop()

    asyncio.run(scenario())


def test_audio_start_has_real_sample_rate() -> None:
    async def scenario() -> None:
        sent: list[dict] = []

        async def send_json(frame: dict) -> bool:
            sent.append(frame)
            return True

        async def send_binary(frame: bytes) -> None:
            return None

        session = CallVoiceSession(AudioBridgeConfig(enabled=True), send_json, send_binary)
        session._active = True
        session._listening = True
        try:
            await session._begin_utterance()
            start = sent[0]
            assert start["type"] == "audio.start"
            assert start["sample_rate"] == 16000
            assert start["follow_up"] is False
            await session._end_utterance()
            assert sent[-1]["type"] == "audio.end"
        finally:
            await session.stop()

    asyncio.run(scenario())


def test_multi_part_audio_url_plays_in_order_then_done() -> None:
    async def scenario() -> None:
        async def send_json(frame: dict) -> bool:
            return True

        async def send_binary(frame: bytes) -> None:
            return None

        session = CallVoiceSession(AudioBridgeConfig(enabled=True), send_json, send_binary)
        session._active = True
        session._reply = {"text": "answer", "listen": False, "end": True}
        played: list[str] = []
        after: list[int] = []

        async def fake_play(frame: dict) -> None:
            played.append(str(frame.get("url")))

        async def fake_after() -> None:
            after.append(1)

        session._play_url_frame = fake_play  # type: ignore[method-assign]
        session._after_playback = fake_after  # type: ignore[method-assign]
        try:
            await session.handle_frame(
                {"type": "audio.url", "utterance_id": "u1", "part": 1, "url": "a"}
            )
            await session.handle_frame(
                {"type": "audio.url", "utterance_id": "u1", "part": 2, "url": "b"}
            )
            # Exact duplicate part must not be played again.
            await session.handle_frame(
                {"type": "audio.url", "utterance_id": "u1", "part": 2, "url": "b"}
            )
            assert after == []
            await session.handle_frame(
                {"type": "audio.done", "utterance_id": "u1", "parts": 2}
            )
            assert session._audio_task is not None
            await asyncio.wait_for(session._audio_task, 1)
            assert played == ["a", "b"]
            assert after == [1]
        finally:
            await session.stop()

    asyncio.run(scenario())


def test_legacy_single_audio_url_ends_after_playback() -> None:
    async def scenario() -> None:
        async def send_json(frame: dict) -> bool:
            return True

        async def send_binary(frame: bytes) -> None:
            return None

        session = CallVoiceSession(AudioBridgeConfig(enabled=True), send_json, send_binary)
        session._active = True
        session._reply = {"text": "answer", "listen": False, "end": True}
        played: list[str] = []
        after: list[int] = []

        async def fake_play(frame: dict) -> None:
            played.append(str(frame.get("url")))

        async def fake_after() -> None:
            after.append(1)

        session._play_url_frame = fake_play  # type: ignore[method-assign]
        session._after_playback = fake_after  # type: ignore[method-assign]
        try:
            await session.handle_frame(
                {"type": "audio.url", "utterance_id": "u2", "url": "solo"}
            )
            assert session._audio_task is not None
            await asyncio.wait_for(session._audio_task, 1)
            assert played == ["solo"]
            assert after == [1]
        finally:
            await session.stop()

    asyncio.run(scenario())


def test_playback_volume_scaling() -> None:
    pcm = struct.pack("<4h", 1000, -1000, 20000, -20000)
    scaled = CallVoiceSession._scale_pcm(pcm, 0.5)
    assert struct.unpack("<4h", scaled) == (500, -500, 10000, -10000)
