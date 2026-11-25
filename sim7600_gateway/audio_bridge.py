"""Optional PCM audio bridge between a GSM call and the Athena intercom.

Requires ALSA tools (arecord/aplay) and the SIM7600 USB audio interface.
On any other platform this module is inert.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable

from .config import AudioBridgeConfig
from .log import get_logger
from .modem.sim7600 import Sim7600
from .pcm_serial import PcmClosed, SerialPcm, route_port, serial_route

log = get_logger("audio.bridge")

PROCESS_READY_DELAY = 0.15
_ALSA_CACHE_TTL = 30.0
_ALSA_CACHE: dict[str, tuple[float, str | None]] = {}

_ALSA_CARD = re.compile(
    r"card\s+(\d+):\s+([^\[]*)\[([^\]]*)\].*?device\s+(\d+):\s+([^\[]*)\[([^\]]*)\]"
)


def detect_alsa_device(keyword: str = "SIM7600", *, refresh: bool = False) -> str | None:
    """Return ``plughw:<card>,<device>`` for the modem's USB audio interface.

    ``auto`` must never silently pick an unrelated card (HDMI, laptop mic):
    only a card matching the keyword (or SIMCom) is accepted, otherwise
    ``None`` is returned and the caller reports a configuration error.
    """
    if not shutil.which("arecord"):
        return None
    now = time.monotonic()
    cached = _ALSA_CACHE.get(keyword)
    if cached is not None and not refresh and now - cached[0] < _ALSA_CACHE_TTL:
        return cached[1]
    try:
        result = subprocess.run(
            ["arecord", "-l"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    found: str | None = None
    if result is not None:
        needle = keyword.lower()
        for line in result.stdout.splitlines():
            match = _ALSA_CARD.search(line)
            if not match:
                continue
            card, card_id, card_name, device, _dev_id, dev_name = match.groups()
            haystack = f"{card_id} {card_name} {dev_name}".lower()
            if needle in haystack or "simcom" in haystack:
                found = f"plughw:{card},{device}"
                break
    _ALSA_CACHE[keyword] = (now, found)
    return found


def resolve_alsa_device(value: str, keyword: str = "SIM7600") -> str | None:
    """Resolve a configured ALSA device.

    ``auto`` means "the SIM7600 card or nothing"; explicit values (including
    ``default`` and ``plughw:...``) are passed through untouched.
    """
    raw = (value or "").strip()
    if raw.lower() == "default":
        return "default"
    if raw and raw.lower() != "auto":
        return raw
    return detect_alsa_device(keyword)


async def resolve_alsa_device_async(value: str, keyword: str = "SIM7600") -> str | None:
    """Non-blocking wrapper: ``arecord -l`` must stay off the event loop."""
    return await asyncio.to_thread(resolve_alsa_device, value, keyword)


async def audio_devices(
    config: AudioBridgeConfig,
) -> tuple[bool, str | None, str | None, str | None]:
    """Validate an audio route before a call is answered.

    Returns ``(ok, capture, playback, error)``. A configured serial PCM port
    wins over ALSA and is reported as ``serial:<port>`` in both capture and
    playback. The check does not open the port; callers do that and report
    failures precisely.
    """
    if not config.enabled:
        return False, None, None, "audio_bridge.enabled = false"

    transport = (config.audio_transport or "auto").lower()
    port = (config.pcm_port or "").strip()
    if transport in ("auto", "serial"):
        if port and port.lower() not in ("", "off", "none", "false", "auto"):
            if await asyncio.to_thread(os.path.exists, port):
                route = serial_route(port)
                return True, route, route, None
            if transport == "serial":
                return (
                    False,
                    None,
                    None,
                    f"serial PCM port not found: {port} (set audio_bridge.pcm_port)",
                )

    if not shutil.which("arecord") or not shutil.which("aplay"):
        return (
            False,
            None,
            None,
            "serial PCM port not found and arecord/aplay not installed (alsa-utils)",
        )
    try:
        capture, playback = await asyncio.gather(
            resolve_alsa_device_async(config.capture_device),
            resolve_alsa_device_async(config.playback_device),
        )
    except Exception as exc:  # pragma: no cover - subprocess/OS errors
        return False, None, None, f"ALSA detection failed: {exc}"
    if not capture or not playback:
        return (
            False,
            capture,
            playback,
            "serial PCM port not found and SIM7600 ALSA card not found "
            "(set audio_bridge.pcm_port or capture_device/playback_device)",
        )
    return True, capture, playback, None


def frame_bytes(config: AudioBridgeConfig) -> int:
    """PCM frame size for the configured rate/chunk (even number of bytes)."""
    value = int(config.sample_rate * 2 * config.chunk_ms / 1000)
    return max(2, value - value % 2)


class AudioBridge:
    def __init__(
        self,
        config: AudioBridgeConfig,
        modem: Sim7600,
        on_capture: Callable[[bytes], Awaitable[None] | None] | None = None,
        on_stopped: Callable[[str], Awaitable[None] | None] | None = None,
    ):
        self.config = config
        self._modem = modem
        self._on_capture = on_capture
        self._on_stopped = on_stopped
        self._capture: asyncio.subprocess.Process | None = None
        self._playback: asyncio.subprocess.Process | None = None
        self._serial: SerialPcm | None = None
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()
        self._active = False
        self._pcm_enabled = False
        self._held = False
        self._capture_device: str | None = None
        self._playback_device: str | None = None
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
            "pcm_enabled": self._pcm_enabled,
            "frames_in": self._frames_in,
            "frames_out": self._frames_out,
            "capture_device": self._capture_device or self.config.capture_device,
            "playback_device": self._playback_device or self.config.playback_device,
            "transport": "serial" if self._serial is not None else "alsa",
            "sample_rate": self.config.sample_rate,
            "chunk_ms": self.config.chunk_ms,
            "last_error": self._last_error,
        }

    async def prepare(self) -> bool:
        ok, capture, playback, error = await audio_devices(self.config)
        if not ok:
            self._last_error = error
            log.warning("аудиомост недоступен: %s", error)
            return False
        self._capture_device = capture
        self._playback_device = playback
        return True

    async def start(self) -> bool:
        if self._active:
            return True
        if not await self.prepare():
            return False
        serial_port = route_port(self._capture_device)
        if serial_port:
            self._serial = SerialPcm(serial_port, self.config.pcm_baudrate)
            try:
                await self._serial.open()
            except Exception as exc:
                self._last_error = str(exc)
                log.error("не удалось открыть PCM-порт моста %s: %s", serial_port, exc)
                self._serial = None
                return False
            if not await self._modem.enable_pcm_audio():
                self._last_error = "AT+CPCMREG=1 failed"
                log.error("аудиомост: модем не включил PCM-маршрут")
                await self.stop()
                return False
            self._pcm_enabled = True
            if self.config.hold_before_bridge:
                self._held = await self._modem.hold_call()
            self._active = True
            self._tasks = [
                asyncio.create_task(self._capture_loop(), name="audio-capture"),
            ]
            log.info("аудиомост запущен (PCM %s, %s Гц)", serial_port, self.config.sample_rate)
            return True

        assert self._capture_device is not None and self._playback_device is not None
        rate = str(self.config.sample_rate)
        capture_cmd = [
            "arecord",
            "-q",
            "-D",
            self._capture_device,
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
            self._playback_device,
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

        # arecord/aplay report a missing/busy device asynchronously.
        await asyncio.sleep(PROCESS_READY_DELAY)
        if not self._processes_alive():
            await self.stop()
            return False

        if not await self._modem.enable_pcm_audio():
            self._last_error = "AT+CPCMREG=1 failed"
            log.error("аудиомост: модем не включил PCM-маршрут")
            await self.stop()
            return False
        self._pcm_enabled = True
        if self.config.hold_before_bridge:
            self._held = await self._modem.hold_call()
        self._active = True
        self._tasks = [
            asyncio.create_task(self._capture_loop(), name="audio-capture"),
            asyncio.create_task(
                self._stderr_loop(self._capture, "arecord"), name="audio-capture-err"
            ),
            asyncio.create_task(self._stderr_loop(self._playback, "aplay"), name="audio-play-err"),
        ]
        log.info(
            "аудиомост запущен (%s ↔ %s, %s Гц)",
            self._capture_device,
            self._playback_device,
            self.config.sample_rate,
        )
        return True

    def _processes_alive(self) -> bool:
        for process in (self._capture, self._playback):
            if process is None or process.returncode is not None:
                return False
        return True

    async def _capture_loop(self) -> None:
        size = frame_bytes(self.config)
        if self._serial is not None:
            try:
                while self._active:
                    try:
                        frame = await self._serial.read_frame(size)
                    except PcmClosed:
                        break
                    except asyncio.CancelledError:
                        raise
                    self._frames_in += 1
                    await self._deliver_capture(frame)
            finally:
                if self._active:
                    self._active = False
                    self._notify_stopped("PCM-порт завершился")
            return
        process = self._capture
        if process is None or process.stdout is None:
            return
        try:
            while self._active:
                try:
                    frame = await process.stdout.readexactly(size)
                except asyncio.IncompleteReadError:
                    break
                except asyncio.CancelledError:
                    raise
                self._frames_in += 1
                await self._deliver_capture(frame)
        finally:
            if self._active:
                self._active = False
                self._notify_stopped("arecord завершился")

    async def _deliver_capture(self, frame: bytes) -> None:
        if self._on_capture is None:
            return
        try:
            result = self._on_capture(frame)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log.debug("отправка PCM не удалась: %s", exc)

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

    async def feed(self, pcm: bytes) -> None:
        """PCM from Athena Core → GSM line."""
        if not self._active:
            return
        if self._serial is not None:
            async with self._write_lock:
                try:
                    await self._serial.write(pcm)
                    self._frames_out += 1
                except (PcmClosed, OSError) as exc:
                    self._last_error = str(exc)
                    log.warning("воспроизведение PCM остановилось: %s", exc)
                    self._active = False
                    self._notify_stopped("PCM-порт завершился")
            return
        process = self._playback
        if process is None or process.stdin is None:
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
                self._notify_stopped("aplay завершился")

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
        serial, self._serial = self._serial, None
        if serial is not None:
            with contextlib.suppress(Exception):
                await serial.close()
        if self._held:
            self._held = False
            with contextlib.suppress(Exception):
                await self._modem.release_held_calls()
        if self._pcm_enabled:
            self._pcm_enabled = False
            with contextlib.suppress(Exception):
                await self._modem.disable_pcm_audio()
        if was_active:
            log.info("аудиомост остановлен")

    def _notify_stopped(self, reason: str) -> None:
        if self._on_stopped is None:
            return
        try:
            result = self._on_stopped(reason)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:  # pragma: no cover - defensive
            log.exception("обработчик остановки аудиомоста упал")
