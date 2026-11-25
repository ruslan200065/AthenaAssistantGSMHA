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
import struct
import sys
import time
import urllib.request
import uuid
from array import array
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from .audio_bridge import audio_devices, frame_bytes
from .config import AudioBridgeConfig
from .log import get_logger
from .pcm_serial import PcmClosed, SerialPcm, route_port

log = get_logger("audio.call")

PROCESS_READY_DELAY = 0.15
FIRST_FRAME_TIMEOUT = 1.0
SILENCE_SECONDS = 0.7
PRE_ROLL_SECONDS = 0.3
MAX_UTTERANCE_SECONDS = 20.0
MIN_THRESHOLD = 220.0
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
PLAYBACK_REASONS = frozenset({"user", "finished", "error", "transfer", "interrupted"})

JsonSender = Callable[[dict[str, Any]], Awaitable[bool]]
BinarySender = Callable[[bytes], Awaitable[None]]
StoppedHandler = Callable[[str], Awaitable[None] | None]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class CallVoiceSession:
    def __init__(
        self,
        config: AudioBridgeConfig,
        send_json: JsonSender,
        send_binary: BinarySender,
        *,
        token: str = "",
        http_base_url: str = "",
        on_stopped: StoppedHandler | None = None,
    ):
        self.config = config
        self._send_json = send_json
        self._send_binary = send_binary
        self._token = token
        self._http_base = http_base_url.rstrip("/")
        self._http_host = urlparse(self._http_base).hostname
        self._on_stopped = on_stopped

        self._frame_bytes = frame_bytes(config)
        self._frame_seconds = self._frame_bytes / 2 / max(1, config.sample_rate)
        self._silence_limit = max(1, round(SILENCE_SECONDS / self._frame_seconds))
        self._pre_roll_len = max(1, round(PRE_ROLL_SECONDS / self._frame_seconds))
        self._max_utterance_frames = max(1, round(MAX_UTTERANCE_SECONDS / self._frame_seconds))

        self._active = False
        self._arec: asyncio.subprocess.Process | None = None
        self._aplay: asyncio.subprocess.Process | None = None
        self._serial: SerialPcm | None = None
        self._tasks: list[asyncio.Task] = []

        self._playing = False
        self._listening = False
        self._follow_up = False
        self._speaking = False
        self._awaiting_reply = False
        self._utterance_id: str | None = None
        self._utterance_started = 0.0
        self._utterance_frames = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._noise = 180.0
        self._pre_roll: deque[bytes] = deque(maxlen=self._pre_roll_len)
        self._reply: dict[str, Any] | None = None
        self._reply_task: asyncio.Task | None = None
        self._reply_audio_pending = False
        self._reply_utterance_id: str | None = None
        self._fallback_spoken_for: str | None = None
        self._played_utterances: deque[str] = deque(maxlen=8)
        self._timeout_task: asyncio.Task | None = None
        self._reply_wait_task: asyncio.Task | None = None
        self._play_lock = asyncio.Lock()
        self._interrupted = False
        self._capture_ready = asyncio.Event()
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
            "awaiting_reply": self._awaiting_reply,
            "sample_rate": self.config.sample_rate,
            "transport": "serial" if self._serial is not None else "alsa",
            "frames_in": self._frames_in,
            "frames_out": self._frames_out,
            "last_error": self._last_error,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        if self._active:
            return True
        ok, capture, playback, error = await audio_devices(self.config)
        if not ok:
            self._last_error = error
            log.warning("голосовой цикл недоступен: %s", error)
            return False

        serial_port = route_port(capture)
        if serial_port:
            self._serial = SerialPcm(serial_port, self.config.pcm_baudrate)
            try:
                await self._serial.open()
            except Exception as exc:
                self._last_error = str(exc)
                log.error("не удалось открыть PCM-порт %s: %s", serial_port, exc)
                self._serial = None
                return False
            self._activate()
            self._capture_ready.set()
            self._tasks = [
                asyncio.create_task(self._capture_loop(), name="call-capture-serial"),
            ]
            log.info(
                "голосовой цикл звонка запущен (PCM %s, %s Гц, кадр %s байт)",
                serial_port,
                self.config.sample_rate,
                self._frame_bytes,
            )
            return True

        assert capture is not None and playback is not None
        rate = str(self.config.sample_rate)
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

        self._activate()
        self._tasks = [
            asyncio.create_task(self._capture_loop(), name="call-capture"),
            asyncio.create_task(self._stderr_loop(self._arec, "arecord"), name="call-arec-err"),
            asyncio.create_task(self._stderr_loop(self._aplay, "aplay"), name="call-aplay-err"),
        ]

        # arecord/aplay report a missing/busy device asynchronously.
        await asyncio.sleep(PROCESS_READY_DELAY)
        if not self._processes_alive():
            self._last_error = self._last_error or "arecord/aplay exited during startup"
            log.error("голосовой цикл: ALSA-процесс завершился на старте")
            await self.stop()
            return False
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._capture_ready.wait(), FIRST_FRAME_TIMEOUT)
        if not self._processes_alive():
            self._last_error = self._last_error or "arecord/aplay exited during startup"
            await self.stop()
            return False
        log.info(
            "голосовой цикл звонка запущен (%s / %s, %s Гц, кадр %s байт)",
            capture,
            playback,
            self.config.sample_rate,
            self._frame_bytes,
        )
        return True

    def _activate(self) -> None:
        self._active = True
        self._listening = True
        self._follow_up = False
        self._speaking = False
        self._awaiting_reply = False
        self._interrupted = False
        self._reply = None
        self._pre_roll.clear()
        self._capture_ready.clear()

    def _processes_alive(self) -> bool:
        if self._serial is not None:
            return self._serial.is_open
        for process in (self._arec, self._aplay):
            if process is None or process.returncode is not None:
                return False
        return True

    async def prime(self) -> bool:
        """Push one silent frame so the modem's USB PCM path is verified."""
        frame = b"\x00" * self._frame_bytes
        try:
            if self._serial is not None:
                await self._serial.write(frame)
            elif self._aplay is not None and self._aplay.stdin is not None:
                self._aplay.stdin.write(frame)
                await self._aplay.stdin.drain()
            else:
                return False
            return True
        except (OSError, PcmClosed) as exc:
            self._last_error = str(exc)
            log.warning("не удалось проверить PCM-маршрут: %s", exc)
            return False

    async def stop(self) -> None:
        self._active = False
        self._listening = False
        self._interrupted = True
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
        self._pre_roll.clear()
        self._reply = None
        self._playing = False
        self._speaking = False
        self._awaiting_reply = False
        self._follow_up = False
        for process in (self._arec, self._aplay):
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=2)
        self._arec = None
        self._aplay = None
        serial, self._serial = self._serial, None
        if serial is not None:
            with contextlib.suppress(Exception):
                await serial.close()

    # ── core frames ──────────────────────────────────────────────────────────

    async def handle_frame(self, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        if kind == "assistant.reply":
            self._reply = frame
            self._reply_utterance_id = str(frame.get("utterance_id") or "") or None
            self._fallback_spoken_for = None
            # Do not start a new utterance while TTS is being synthesized or
            # downloaded; playback/follow-up re-enables listening later.
            self._listening = False
            self._pre_roll.clear()
            self._reset_reply_wait()
            if self._reply_task is not None:
                self._reply_task.cancel()
            self._reply_task = asyncio.create_task(self._reply_fallback(), name="call-reply")
            return
        if kind == "audio.url":
            if self._reply_task is not None:
                self._reply_task.cancel()
                self._reply_task = None
            self._reset_reply_wait()
            await self._play_url(frame)
            return
        if kind == "assistant.error":
            log.warning("голосовой цикл: %s — %s", frame.get("code"), frame.get("message"))
            await self._end_dialogue("error")
            return
        if kind == "conversation.end":
            self._listening = False
            if self._timeout_task is not None:
                self._timeout_task.cancel()
                self._timeout_task = None
            return
        if kind in ("asr.partial", "asr.final"):
            log.debug("ASR: %s", frame.get("text"))

    def _reset_reply_wait(self) -> None:
        """A reply arrived: the microphone may listen again after playback."""
        self._awaiting_reply = False
        self._speech_frames = 0
        self._silence_frames = 0
        if self._reply_wait_task is not None:
            self._reply_wait_task.cancel()
            self._reply_wait_task = None

    async def _reply_fallback(self) -> None:
        # audio.url normally follows assistant.reply immediately. If TTS was
        # unavailable, try the device TTS endpoint and then close the turn.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(6.0)
        if not self._active or self._playing or self._reply_audio_pending:
            return
        # An audio.url may arrive after this task was cancelled but before the
        # event loop notices; never speak the text a second time.
        text = str((self._reply or {}).get("text") or "").strip()
        if text and await self._play_tts(text):
            self._fallback_spoken_for = self._reply_utterance_id
        await self._after_playback()

    async def _play_url(self, frame: dict[str, Any]) -> None:
        utterance_id = str(frame.get("utterance_id") or "")
        if utterance_id and utterance_id == self._fallback_spoken_for:
            log.info("звонок: audio.url %s уже озвучен локальным TTS — пропущен", utterance_id)
            return
        if utterance_id and utterance_id in self._played_utterances:
            log.info("звонок: повторный audio.url %s пропущен", utterance_id)
            return
        if self._playing:
            log.warning("звонок: audio.url пришёл во время воспроизведения — пропущен")
            return
        if utterance_id:
            self._played_utterances.append(utterance_id)
        self._reply_audio_pending = True
        try:
            await self._play_url_inner(frame)
        finally:
            self._reply_audio_pending = False

    async def _play_url_inner(self, frame: dict[str, Any]) -> None:
        url = str(frame.get("url") or "").strip()
        text = str(frame.get("text") or (self._reply or {}).get("text") or "")
        if url:
            try:
                data = await asyncio.to_thread(self._download, url)
            except Exception as exc:
                self._last_error = str(exc)
                log.warning("не удалось скачать audio.url: %s", exc)
                await self._play_tts(text)
            else:
                await self._play_encoded(data, _safe_float(frame.get("duration")))
        elif text:
            await self._play_tts(text)
        await self._after_playback()

    async def _after_playback(self) -> None:
        if not self._active:
            return
        self._awaiting_reply = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._pre_roll.clear()
        self._reset_reply_wait()
        reply = self._reply or {}
        listen = bool(reply.get("listen"))
        timeout = max(3, _safe_int(reply.get("follow_up_timeout_seconds"), 8) or 8)
        if listen and not reply.get("end", False):
            self._listening = True
            self._follow_up = True
            self._schedule_followup_timeout(timeout)
            log.info("звонок: жду продолжения диалога")
        else:
            self._listening = False
            self._follow_up = False
            await self._end_dialogue("ended")

    async def _end_dialogue(self, reason: str) -> None:
        self._listening = False
        self._speaking = False
        self._awaiting_reply = False
        self._follow_up = False
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            self._timeout_task = None
        self._reset_reply_wait()
        await self._emit({"type": "conversation.end", "reason": reason})

    def _schedule_followup_timeout(self, seconds: int) -> None:
        if self._timeout_task is not None:
            self._timeout_task.cancel()

        async def wait_and_end() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(seconds)
            if (
                self._active
                and not self._speaking
                and not self._playing
                and not self._awaiting_reply
            ):
                log.info("звонок: продолжения не было за %s c", seconds)
                await self._end_dialogue("timeout")

        self._timeout_task = asyncio.create_task(wait_and_end(), name="call-followup-timeout")

    # ── capture + VAD ────────────────────────────────────────────────────────

    async def _capture_loop(self) -> None:
        if self._serial is not None:
            await self._capture_loop_serial()
            return
        assert self._arec is not None and self._arec.stdout is not None
        stream = self._arec.stdout
        try:
            while self._active:
                try:
                    frame = await stream.readexactly(self._frame_bytes)
                except asyncio.IncompleteReadError:
                    break
                self._capture_ready.set()
                await self._process_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - hardware/ALSA errors
            self._last_error = str(exc)
            log.warning("захват речи в звонке остановился: %s", exc)
        finally:
            if self._active:
                self._active = False
                self._listening = False
                log.warning("arecord в звонке завершился")
                self._notify_stopped("arecord завершился")

    async def _capture_loop_serial(self) -> None:
        assert self._serial is not None
        try:
            while self._active:
                try:
                    frame = await self._serial.read_frame(self._frame_bytes)
                except PcmClosed:
                    break
                except asyncio.CancelledError:
                    raise
                self._capture_ready.set()
                await self._process_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - USB/serial errors
            self._last_error = str(exc)
            log.warning("захват речи из PCM-порта остановился: %s", exc)
        finally:
            if self._active:
                self._active = False
                self._listening = False
                log.warning("PCM-порт завершил захват речи")
                self._notify_stopped("PCM-порт завершился")

    async def _process_frame(self, frame: bytes) -> None:
        if self._playing:
            self._pre_roll.clear()
            return
        level = rms(frame)
        if not self._speaking:
            if not self._listening or self._awaiting_reply:
                self._pre_roll.clear()
                return
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
            return

        await self._send_frame(frame)
        self._utterance_frames += 1
        threshold = max(MIN_THRESHOLD, self._noise * 2.4)
        if level < threshold * 0.8:
            self._silence_frames += 1
        else:
            self._silence_frames = 0
        elapsed = time.monotonic() - self._utterance_started
        if (
            self._silence_frames >= self._silence_limit
            or self._utterance_frames >= self._max_utterance_frames
            or elapsed > MAX_UTTERANCE_SECONDS
        ):
            await self._end_utterance()

    async def _begin_utterance(self) -> None:
        self._speaking = True
        self._awaiting_reply = False
        self._silence_frames = 0
        self._utterance_frames = 0
        self._utterance_started = time.monotonic()
        self._utterance_id = f"gsm-{uuid.uuid4().hex[:12]}"
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            self._timeout_task = None
        await self._emit(
            {
                "type": "audio.start",
                "utterance_id": self._utterance_id,
                "sample_rate": self.config.sample_rate,
                "format": "pcm_s16le",
                "follow_up": self._follow_up,
            }
        )
        log.info(
            "звонок: речь абонента → audio.start%s",
            " (follow_up)" if self._follow_up else "",
        )

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
        await self._play_guarded(pcm, rate)
        return True

    async def _play_encoded(self, data: bytes, duration: float | None) -> None:
        async with self._play_lock:
            await self._playback_begin()
            reason = "interrupted"
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
                    str(self.config.sample_rate),
                    "-ac",
                    "1",
                    "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                pcm, _ = await process.communicate(data)
                if not pcm:
                    reason = "error"
                else:
                    reason = await self._play_pcm(pcm, self.config.sample_rate, duration)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - ffmpeg/ALSA
                reason = "error"
                self._last_error = str(exc)
                log.warning("воспроизведение ответа в линию не удалось: %s", exc)
            finally:
                await self._playback_end(reason)

    async def _play_guarded(self, pcm: bytes, rate: int) -> None:
        async with self._play_lock:
            await self._playback_begin()
            reason = "interrupted"
            try:
                reason = await self._play_pcm(pcm, rate)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - ALSA
                reason = "error"
                self._last_error = str(exc)
                log.warning("воспроизведение TTS в линию не удалось: %s", exc)
            finally:
                await self._playback_end(reason)

    async def _playback_begin(self) -> None:
        self._playing = True
        self._listening = False
        self._speaking = False
        self._awaiting_reply = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._pre_roll.clear()
        self._reset_reply_wait()
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            self._timeout_task = None

    async def _playback_end(self, reason: str) -> None:
        self._playing = False
        if self._interrupted or reason not in PLAYBACK_REASONS:
            reason = "interrupted" if self._interrupted else "error"
        await self._emit({"type": "playback.stopped", "reason": reason})

    async def _play_pcm(self, pcm: bytes, rate: int, duration: float | None = None) -> str:
        """Write PCM to the PCM port/aplay at realtime pace; returns a reason."""
        if not pcm:
            return "error"
        if self._serial is None and (self._aplay is None or self._aplay.stdin is None):
            return "error"
        target = self.config.sample_rate
        if rate and rate != target:
            pcm = await asyncio.to_thread(self._resample, pcm, rate, target)
        total = len(pcm) / 2 / target
        if duration:
            total = min(total, max(0.0, duration))
            pcm = pcm[: max(0, int(total * target)) * 2]
        if not pcm:
            return "error"
        volume = int(getattr(self.config, "playback_volume", 100) or 100)
        if volume != 100:
            pcm = await asyncio.to_thread(self._scale_pcm, pcm, volume / 100)

        # Deadline scheduling (like the working reference): do not add write
        # time on top of the 20 ms frame, otherwise playback drifts and the
        # modem underruns. Pad the last frame so the codec always gets 640 B.
        interval = self._frame_seconds
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        offset = 0
        while offset < len(pcm):
            if not self._active:
                return "interrupted"
            chunk = pcm[offset : offset + self._frame_bytes]
            offset += len(chunk)
            if len(chunk) < self._frame_bytes:
                chunk = chunk + b"\x00" * (self._frame_bytes - len(chunk))
            now = loop.time()
            if now - deadline >= interval:
                deadline = now
            if deadline > now:
                await asyncio.sleep(deadline - now)
            try:
                if self._serial is not None:
                    await self._serial.write(chunk)
                else:
                    assert self._aplay is not None and self._aplay.stdin is not None
                    self._aplay.stdin.write(chunk)
                    await self._aplay.stdin.drain()
            except (ConnectionResetError, BrokenPipeError, PcmClosed):
                return "error"
            self._frames_out += 1
            deadline += interval
        await asyncio.sleep(max(0.0, deadline - loop.time()))
        log.info("звонок: ответ проигран в линию (%.1f c)", total)
        return "finished"

    @staticmethod
    def _scale_pcm(pcm: bytes, gain: float) -> bytes:
        """Scale little-endian s16 PCM with clipping."""
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        samples = array(
            "h",
            [max(-32768, min(32767, int(sample * gain))) for sample in samples],
        )
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes()

    def _download(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("audio.url must be http(s)")
        if self._http_host and parsed.hostname and parsed.hostname != self._http_host:
            raise ValueError("audio.url host is not the Athena core")
        request = urllib.request.Request(url, headers={"User-Agent": "sim7600-gateway/1.1"})
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
            rate = _safe_int(response.headers.get("X-Sample-Rate"), self.config.sample_rate)
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError("TTS response is too large")
        return data, rate or self.config.sample_rate

    @staticmethod
    def _resample(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
        if source_rate == target_rate or not pcm:
            return pcm
        try:
            import audioop  # noqa: PLC0415 - stdlib, removed in Python 3.13
        except ImportError:
            return _linear_resample(pcm, source_rate, target_rate)
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

    def _notify_stopped(self, reason: str) -> None:
        if self._on_stopped is None:
            return
        try:
            result = self._on_stopped(reason)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:  # pragma: no cover - defensive
            log.exception("обработчик остановки голосового цикла упал")

    async def _emit(self, frame: dict[str, Any]) -> None:
        try:
            await self._send_json(frame)
        except Exception as exc:
            log.debug("кадр %s не отправлен: %s", frame.get("type"), exc)


def _linear_resample(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """Small fallback for Python 3.13, where stdlib ``audioop`` was removed."""
    import array  # noqa: PLC0415

    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return b""
    count = max(1, round(len(samples) * target_rate / source_rate))
    step = source_rate / target_rate
    out = array.array("h")
    last = len(samples) - 1
    for index in range(count):
        position = index * step
        left = min(last, int(position))
        right = min(last, left + 1)
        fraction = position - left
        value = samples[left] + (samples[right] - samples[left]) * fraction
        out.append(max(-32768, min(32767, int(value))))
    return out.tobytes()


def rms(frame: bytes) -> float:
    if len(frame) < 2:
        return 0.0
    samples = struct.unpack("<" + "h" * (len(frame) // 2), frame[: len(frame) // 2 * 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))
