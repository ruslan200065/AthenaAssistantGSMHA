"""Voice assistant over a GSM call.

After the gateway answers an incoming call (``ATA``), the SIM7600 USB audio
interface becomes a normal voice device:

    caller speech -> local VAD -> audio.start + PCM + audio.end -> core
    core reply    -> assistant.reply + audio.url -> aplay into the phone line

The core treats this exactly like a speaker without a wake word
(``mode: "assistant"`` in the ``call`` command). This module owns ALSA
subprocesses, the VAD and playback; signalling stays in ``GatewayBridge``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import shutil
import struct
import time
import urllib.request
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from .audio_bridge import resolve_alsa_device
from .config import AudioBridgeConfig
from .log import get_logger

log = get_logger("audio.call")

FRAME_BYTES = 640  # 20 ms, 16 kHz, mono, s16le
SAMPLE_RATE = 16000
FRAME_SECONDS = FRAME_BYTES / 2 / SAMPLE_RATE
SILENCE_FRAMES = 35  # ~700 ms
PRE_ROLL_FRAMES = 15  # ~300 ms
MAX_UTTERANCE_FRAMES = 1000  # 20 s
MIN_THRESHOLD = 220.0
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

JsonSender = Callable[[dict[str, Any]], Awaitable[bool]]
BinarySender = Callable[[bytes], Awaitable[None]]


class CallVoiceSession:
    def __init__(
        self,
        config: AudioBridgeConfig,
        send_json: JsonSender,
        send_binary: BinarySender,
        *,
        token: str = "",
        http_base_url: str = "",
    ):
        self.config = config
        self._send_json = send_json
        self._send_binary = send_binary
        self._token = token
        self._http_base = http_base_url.rstrip("/")

        self._active = False
        self._arec: asyncio.subprocess.Process | None = None
        self._aplay: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []

        self._playing = False
        self._listening = False
        self._follow_up = False
        self._speaking = False
        self._awaiting_reply = False
        self._utterance_id: str | None = None
        self._utterance_started = 0.0
        self._speech_frames = 0
        self._silence_frames = 0
        self._noise = 180.0
        self._pre_roll: deque[bytes] = deque(maxlen=PRE_ROLL_FRAMES)
        self._reply: dict[str, Any] | None = None
        self._reply_task: asyncio.Task | None = None
        self._timeout_task: asyncio.Task | None = None
        self._reply_wait_task: asyncio.Task | None = None
        self._play_lock = asyncio.Lock()
        self._last_error: str | None = None
        self._frames_in = 0
        self._frames_out = 0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def status(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "playing": self._playing,
            "listening": self._listening,
            "speaking": self._speaking,
            "frames_in": self._frames_in,
            "frames_out": self._frames_out,
            "last_error": self._last_error,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        if self._active:
            return True
        if not self.config.enabled:
            log.warning("голосовой цикл выключен: audio_bridge.enabled = false")
            return False
        if not shutil.which("arecord") or not shutil.which("aplay"):
            self._last_error = "arecord/aplay not installed"
            log.warning("нет arecord/aplay — голос в звонке недоступен")
            return False
        capture = resolve_alsa_device(self.config.capture_device)
        playback = resolve_alsa_device(self.config.playback_device)
        if not capture or not playback:
            self._last_error = "ALSA capture/playback device not found"
            log.warning("не найдено аудиоустройство SIM7600 (arecord -l)")
            return False
        rate = str(SAMPLE_RATE)
        try:
            self._arec = await asyncio.create_subprocess_exec(
                "arecord",
                "-q",
                "-D",
                capture,
                "-f",
                "S16_LE",
                "-r",
                rate,
                "-c",
                "1",
                "-t",
                "raw",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._aplay = await asyncio.create_subprocess_exec(
                "aplay",
                "-q",
                "-D",
                playback,
                "-f",
                "S16_LE",
                "-r",
                rate,
                "-c",
                "1",
                "-t",
                "raw",
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._last_error = str(exc)
            log.error("не удалось открыть аудио для звонка: %s", exc)
            await self.stop()
            return False

        self._active = True
        self._listening = True
        self._follow_up = False
        self._speaking = False
        self._awaiting_reply = False
        self._reply = None
        self._pre_roll.clear()
        self._tasks = [
            asyncio.create_task(self._capture_loop(), name="call-capture"),
            asyncio.create_task(self._stderr_loop(self._arec, "arecord"), name="call-arec-err"),
            asyncio.create_task(self._stderr_loop(self._aplay, "aplay"), name="call-aplay-err"),
        ]
        log.info("голосовой цикл звонка запущен (%s / %s, 16 кГц)", capture, playback)
        return True

    async def stop(self) -> None:
        self._active = False
        self._listening = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for timer in (self._reply_task, self._timeout_task, self._reply_wait_task):
            if timer is not None:
                timer.cancel()
        self._reply_task = None
        self._timeout_task = None
        self._reply_wait_task = None
        for process in (self._arec, self._aplay):
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=2)
        self._arec = None
        self._aplay = None

    # ── core frames ──────────────────────────────────────────────────────────

    async def handle_frame(self, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        if kind == "assistant.reply":
            self._reply = frame
            if self._reply_wait_task is not None:
                self._reply_wait_task.cancel()
                self._reply_wait_task = None
            if self._reply_task is not None:
                self._reply_task.cancel()
            self._reply_task = asyncio.create_task(self._reply_fallback(), name="call-reply")
            return
        if kind == "audio.url":
            if self._reply_task is not None:
                self._reply_task.cancel()
                self._reply_task = None
            await self._play_url(frame)
            return
        if kind == "assistant.error":
            log.warning("голосовой цикл: %s — %s", frame.get("code"), frame.get("message"))
            await self._end_dialogue("error")
            return
        if kind == "conversation.end":
            self._listening = False
            return
        if kind in ("asr.partial", "asr.final"):
            log.debug("ASR: %s", frame.get("text"))

    async def _reply_fallback(self) -> None:
        # audio.url normally follows assistant.reply immediately. If TTS was
        # unavailable, try the device TTS endpoint and then close the turn.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(1.8)
        if not self._active or self._playing:
            return
        text = str((self._reply or {}).get("text") or "").strip()
        played = False
        if text:
            played = await self._play_tts(text)
        if not played:
            await self._after_playback()

    async def _play_url(self, frame: dict[str, Any]) -> None:
        url = str(frame.get("url") or "").strip()
        if not url:
            await self._play_tts(str(frame.get("text") or (self._reply or {}).get("text") or ""))
            await self._after_playback()
            return
        try:
            data = await asyncio.to_thread(self._download, url)
        except Exception as exc:
            self._last_error = str(exc)
            log.warning("не удалось скачать audio.url: %s", exc)
            await self._play_tts(str(frame.get("text") or (self._reply or {}).get("text") or ""))
            await self._after_playback()
            return
        duration = frame.get("duration")
        await self._play_encoded(data, float(duration) if duration else None)
        await self._after_playback()

    async def _after_playback(self) -> None:
        if not self._active:
            return
        reply = self._reply or {}
        listen = bool(reply.get("listen"))
        timeout = int(reply.get("follow_up_timeout_seconds") or 0)
        if listen and not reply.get("end", False):
            self._listening = True
            self._follow_up = True
            self._schedule_followup_timeout(max(3, timeout or 8))
            log.info("звонок: жду продолжения диалога")
        else:
            self._listening = False
            self._follow_up = False
            await self._end_dialogue("ended")

    async def _end_dialogue(self, reason: str) -> None:
        self._listening = False
        self._speaking = False
        self._awaiting_reply = False
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            self._timeout_task = None
        await self._emit({"type": "conversation.end", "reason": reason})

    def _schedule_followup_timeout(self, seconds: int) -> None:
        if self._timeout_task is not None:
            self._timeout_task.cancel()

        async def wait_and_end() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(seconds)
            if self._active and not self._speaking and not self._playing:
                log.info("звонок: продолжения не было за %s c", seconds)
                await self._end_dialogue("timeout")

        self._timeout_task = asyncio.create_task(wait_and_end(), name="call-followup-timeout")

    # ── capture + VAD ────────────────────────────────────────────────────────

    async def _capture_loop(self) -> None:
        assert self._arec is not None and self._arec.stdout is not None
        stream = self._arec.stdout
        try:
            while self._active:
                try:
                    frame = await stream.readexactly(FRAME_BYTES)
                except asyncio.IncompleteReadError:
                    break
                if self._playing:
                    continue
                level = rms(frame)
                if not self._speaking:
                    if not self._listening or self._awaiting_reply:
                        self._pre_roll.clear()
                        continue
                    self._pre_roll.append(frame)
                    threshold = max(MIN_THRESHOLD, self._noise * 2.4)
                    if level > threshold:
                        self._speech_frames += 1
                        if self._speech_frames >= 2:
                            await self._begin_utterance()
                            for buffered in self._pre_roll:
                                await self._send_frame(buffered)
                            self._pre_roll.clear()
                    else:
                        self._speech_frames = 0
                        self._noise = self._noise * 0.99 + level * 0.01
                else:
                    await self._send_frame(frame)
                    threshold = max(MIN_THRESHOLD, self._noise * 2.4)
                    if level < threshold * 0.8:
                        self._silence_frames += 1
                    else:
                        self._silence_frames = 0
                    elapsed = time.monotonic() - self._utterance_started
                    if self._silence_frames >= SILENCE_FRAMES or elapsed > 20:
                        await self._end_utterance()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - hardware/ALSA errors
            self._last_error = str(exc)
            log.warning("захват речи в звонке остановился: %s", exc)
        finally:
            if self._active:
                self._active = False
                log.warning("arecord в звонке завершился")

    async def _begin_utterance(self) -> None:
        self._speaking = True
        self._silence_frames = 0
        self._utterance_started = time.monotonic()
        self._utterance_id = f"gsm-{uuid.uuid4().hex[:12]}"
        await self._emit(
            {
                "type": "audio.start",
                "utterance_id": self._utterance_id,
                "sample_rate": SAMPLE_RATE,
                "format": "pcm_s16le",
                "follow_up": self._follow_up,
            }
        )
        log.info("звонок: речь абонента → audio.start%s", " (follow_up)" if self._follow_up else "")

    async def _end_utterance(self) -> None:
        if not self._speaking:
            return
        self._speaking = False
        self._awaiting_reply = True
        self._listening = False
        self._follow_up = False
        utterance_id, self._utterance_id = self._utterance_id, None
        await self._emit({"type": "audio.end", "utterance_id": utterance_id})
        log.info("звонок: audio.end, жду ответ ядра")
        if self._reply_wait_task is not None:
            self._reply_wait_task.cancel()

        async def wait_for_reply() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(45)
            if self._active and self._awaiting_reply:
                log.warning("звонок: ядро не ответило за 45 c")
                await self._end_dialogue("timeout")

        self._reply_wait_task = asyncio.create_task(wait_for_reply(), name="call-reply-wait")

    async def _send_frame(self, frame: bytes) -> None:
        self._frames_in += 1
        try:
            await self._send_binary(frame)
        except Exception as exc:
            log.debug("отправка PCM в ядро не удалась: %s", exc)

    # ── playback ─────────────────────────────────────────────────────────────

    async def _play_tts(self, text: str) -> bool:
        text = text.strip()
        if not text or not self._http_base or not self._token:
            return False
        try:
            pcm, rate = await asyncio.to_thread(self._request_tts, text)
        except Exception as exc:
            self._last_error = str(exc)
            log.info("локальный TTS ядра недоступен: %s", exc)
            return False
        if not pcm:
            return False
        await self._play_pcm(pcm, rate)
        return True

    async def _play_encoded(self, data: bytes, duration: float | None) -> None:
        async with self._play_lock:
            self._playing = True
            self._listening = False
            try:
                process = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    "pipe:0",
                    "-f",
                    "s16le",
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-ac",
                    "1",
                    "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                pcm, _ = await process.communicate(data)
                await self._play_pcm(pcm, SAMPLE_RATE, duration)
            except Exception as exc:  # pragma: no cover - ffmpeg/ALSA
                self._last_error = str(exc)
                log.warning("воспроизведение ответа в линию не удалось: %s", exc)
            finally:
                self._playing = False
                await self._emit({"type": "playback.stopped", "reason": "done"})

    async def _play_pcm(self, pcm: bytes, rate: int, duration: float | None = None) -> None:
        if not pcm or self._aplay is None or self._aplay.stdin is None:
            return
        if rate and rate != SAMPLE_RATE:
            pcm = await asyncio.to_thread(self._resample, pcm, rate, SAMPLE_RATE)
        total = len(pcm) / 2 / SAMPLE_RATE
        if duration:
            total = min(total, duration)
        deadline = time.monotonic() + total
        offset = 0
        while offset < len(pcm) and self._active and time.monotonic() < deadline:
            chunk = pcm[offset : offset + FRAME_BYTES]
            offset += len(chunk)
            try:
                self._aplay.stdin.write(chunk)
                await self._aplay.stdin.drain()
            except (ConnectionResetError, BrokenPipeError):
                return
            self._frames_out += 1
            await asyncio.sleep(FRAME_SECONDS)
        log.info("звонок: ответ проигран в линию (%.1f c)", total)

    def _download(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "sim7600-gateway/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError("audio.url is too large")
        return data

    def _request_tts(self, text: str) -> tuple[bytes, int]:
        body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._http_base}/api/device/tts",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Device-Token": self._token,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            rate = int(response.headers.get("X-Sample-Rate") or SAMPLE_RATE)
            return response.read(), rate

    @staticmethod
    def _resample(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
        if source_rate == target_rate or not pcm:
            return pcm
        try:
            import audioop  # noqa: PLC0415 - stdlib, removed in Python 3.13
        except ImportError:
            return pcm
        converted, _ = audioop.ratecv(pcm, 2, 1, source_rate, target_rate, None)
        return converted

    async def _stderr_loop(self, process: asyncio.subprocess.Process | None, name: str) -> None:
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                self._last_error = text
                log.debug("%s: %s", name, text)

    async def _emit(self, frame: dict[str, Any]) -> None:
        try:
            await self._send_json(frame)
        except Exception as exc:
            log.debug("кадр %s не отправлен: %s", frame.get("type"), exc)


def rms(frame: bytes) -> float:
    if len(frame) < 2:
        return 0.0
    samples = struct.unpack("<" + "h" * (len(frame) // 2), frame[: len(frame) // 2 * 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))
