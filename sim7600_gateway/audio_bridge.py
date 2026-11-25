"""Optional PCM audio bridge between a GSM call and the Athena intercom.

Requires ALSA tools (arecord/aplay) and the SIM7600 USB audio interface.
On any other platform this module is inert.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import Awaitable, Callable

from .config import AudioBridgeConfig
from .log import get_logger
from .modem.sim7600 import Sim7600

log = get_logger("audio.bridge")

FRAME_BYTES = 640  # 20 ms, 16 kHz, mono, s16le


class AudioBridge:
    def __init__(
        self,
        config: AudioBridgeConfig,
        modem: Sim7600,
        on_capture: Callable[[bytes], Awaitable[None] | None] | None = None,
    ):
        self.config = config
        self._modem = modem
        self._on_capture = on_capture
        self._capture: asyncio.subprocess.Process | None = None
        self._playback: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()
        self._active = False
        self._frames_in = 0
        self._frames_out = 0
        self._last_error: str | None = None

    @property
    def active(self) -> bool:
        return self._active

    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "active": self._active,
            "frames_in": self._frames_in,
            "frames_out": self._frames_out,
            "capture_device": self.config.capture_device,
            "playback_device": self.config.playback_device,
            "last_error": self._last_error,
        }

    async def start(self) -> bool:
        if not self.config.enabled:
            return False
        if self._active:
            return True
        if not shutil.which("arecord") or not shutil.which("aplay"):
            self._last_error = "arecord/aplay not installed"
            log.warning("аудиомост недоступен: нет arecord/aplay")
            return False
        channel = self._modem.channel
        if channel is not None:
            with contextlib.suppress(Exception):
                result = await channel.command("AT+CPCMREG=1", timeout=5)
                if not result.ok:
                    log.warning("AT+CPCMREG=1 → %s", result.error)
            if self.config.hold_before_bridge:
                with contextlib.suppress(Exception):
                    await channel.command("AT+CHLD=2", timeout=5)
        rate = str(self.config.sample_rate)
        capture_cmd = [
            "arecord",
            "-q",
            "-D",
            self.config.capture_device,
            "-f",
            "S16_LE",
            "-r",
            rate,
            "-c",
            "1",
            "-t",
            "raw",
        ]
        playback_cmd = [
            "aplay",
            "-q",
            "-D",
            self.config.playback_device,
            "-f",
            "S16_LE",
            "-r",
            rate,
            "-c",
            "1",
            "-t",
            "raw",
        ]
        try:
            self._capture = await asyncio.create_subprocess_exec(
                *capture_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._playback = await asyncio.create_subprocess_exec(
                *playback_cmd,
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._last_error = str(exc)
            log.error("не удалось запустить аудиомост: %s", exc)
            await self.stop()
            return False
        self._active = True
        self._tasks = [
            asyncio.create_task(self._capture_loop(), name="audio-capture"),
            asyncio.create_task(self._stderr_loop(self._capture), name="audio-capture-err"),
            asyncio.create_task(self._stderr_loop(self._playback), name="audio-play-err"),
        ]
        log.info("аудиомост запущен (%s ↔ %s)", self.config.capture_device, self.config.playback_device)
        return True

    async def _capture_loop(self) -> None:
        process = self._capture
        if process is None or process.stdout is None:
            return
        while self._active:
            try:
                frame = await process.stdout.readexactly(FRAME_BYTES)
            except asyncio.IncompleteReadError:
                break
            except asyncio.CancelledError:
                raise
            self._frames_in += 1
            if self._on_capture is not None:
                try:
                    result = self._on_capture(frame)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    log.debug("отправка PCM не удалась: %s", exc)
        if self._active:
            log.warning("захват аудио остановился")

    async def _stderr_loop(self, process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                self._last_error = text
                log.debug("alsa: %s", text)

    async def feed(self, pcm: bytes) -> None:
        """PCM from Athena Core → GSM line."""
        process = self._playback
        if not self._active or process is None or process.stdin is None:
            return
        async with self._write_lock:
            try:
                process.stdin.write(pcm)
                await process.stdin.drain()
                self._frames_out += 1
            except (ConnectionResetError, BrokenPipeError) as exc:
                self._last_error = str(exc)
                log.warning("воспроизведение PCM остановилось: %s", exc)
                self._active = False

    async def stop(self) -> None:
        was_active = self._active
        self._active = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for process in (self._capture, self._playback):
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=2)
        self._capture = None
        self._playback = None
        if was_active:
            channel = self._modem.channel
            if channel is not None:
                with contextlib.suppress(Exception):
                    await channel.command("AT+CPCMREG=0", timeout=5)
            log.info("аудиомост остановлен")
