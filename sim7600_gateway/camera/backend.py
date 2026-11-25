"""USB camera backend: V4L2 MJPEG via ffmpeg, mock source for demo mode."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import CameraConfig
from ..log import get_logger

log = get_logger("camera.backend")

_FORMAT_RE = re.compile(r"\[\d+\]:\s+'([^']+)'")
_SIZE_RE = re.compile(r"Size:\s+Discrete\s+(\d+)x(\d+)")
_FPS_RE = re.compile(r"\(([\d.]+)\s+fps\)")
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"


@dataclass
class CameraInfo:
    device: str
    mock: bool
    width: int
    height: int
    fps: int
    pixel_format: str = "MJPG"
    formats: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    available: bool = False
    mode: str = "v4l2"

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "mock": self.mock,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "pixel_format": self.pixel_format,
            "available": self.available,
            "mode": self.mode,
            "formats": self.formats,
        }


def probe_v4l2(device: str, v4l2ctl: str = "v4l2-ctl") -> dict[str, list[dict[str, Any]]]:
    """Parse ``v4l2-ctl --list-formats-ext`` into {format: [{size, fps}]}."""
    import subprocess  # noqa: PLC0415 - only used on Linux hosts

    if not shutil.which(v4l2ctl):
        return {}
    try:
        result = subprocess.run(
            [v4l2ctl, "--device", device, "--list-formats-ext"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    formats: dict[str, list[dict[str, Any]]] = {}
    current: str | None = None
    size: tuple[int, int] | None = None
    for line in result.stdout.splitlines():
        fmt = _FORMAT_RE.search(line)
        if fmt:
            current = fmt.group(1).upper()
            formats.setdefault(current, [])
            size = None
            continue
        if current is None:
            continue
        match_size = _SIZE_RE.search(line)
        if match_size:
            size = (int(match_size.group(1)), int(match_size.group(2)))
            formats[current].append({"width": size[0], "height": size[1], "fps": []})
            continue
        match_fps = _FPS_RE.search(line)
        if match_fps and size and formats[current]:
            fps = float(match_fps.group(1))
            entry = formats[current][-1]
            entry["fps"].append(round(fps, 2))
    return formats


async def _run_probe(argv: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except TimeoutError:
        process.kill()
        raise RuntimeError("v4l2-ctl timed out") from None
    return stdout.decode("utf-8", "replace")


class FfmpegCamera:
    """Owns an ffmpeg process producing an MJPEG byte stream."""

    def __init__(self, config: CameraConfig, on_frame):
        self.config = config
        self.info = self._build_info()
        self._on_frame = on_frame
        self._process: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []
        self._stopped = False
        self._started_at: float | None = None
        self._frames = 0
        self._last_frame_at: float | None = None
        self._last_error: str | None = None
        self._restart_delay = 2.0

    def _build_info(self, formats: dict[str, list[dict[str, Any]]] | None = None) -> CameraInfo:
        if self.config.mock:
            return CameraInfo(
                device="lavfi:testsrc2",
                mock=True,
                width=self.config.width,
                height=self.config.height,
                fps=self.config.fps,
                pixel_format="MJPEG",
                mode="mock",
                available=True,
            )
        formats = formats or {}
        chosen = self.config.input_format
        if chosen == "auto":
            preferred = [name for name in ("MJPG", "H264", "YUYV", "NV12") if name in formats]
            chosen = preferred[0] if preferred else "MJPG"
        width, height, fps = self.config.width, self.config.height, self.config.fps
        entries = formats.get(chosen.upper()) or []
        if entries:
            fitting = [
                entry
                for entry in entries
                if entry["width"] <= width and entry["height"] <= height
            ]
            best = max(fitting, key=lambda e: e["width"] * e["height"]) if fitting else min(
                entries, key=lambda e: e["width"] * e["height"]
            )
            width, height = best["width"], best["height"]
            if best.get("fps"):
                fps = max(1, min(fps, int(max(best["fps"]))))
        return CameraInfo(
            device=self.config.device,
            mock=False,
            width=width,
            height=height,
            fps=fps,
            pixel_format=chosen.upper(),
            formats=formats,
            available=bool(formats),
            mode="v4l2",
        )

    @property
    def frames(self) -> int:
        return self._frames

    @property
    def last_frame_at(self) -> float | None:
        return self._last_frame_at

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _command(self) -> list[str]:
        cfg = self.config
        if cfg.mock:
            lavfi = f"testsrc2=size={cfg.width}x{cfg.height}:rate={cfg.fps}"
            return [
                cfg.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                lavfi,
                "-c:v",
                "mjpeg",
                "-q:v",
                "4",
                "-f",
                "mjpeg",
                "-",
            ]
        args = [cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "v4l2"]
        pixel = self.info.pixel_format.lower()
        if pixel in ("mjpg", "h264"):
            args += ["-input_format", pixel]
        args += [
            "-video_size",
            f"{self.info.width}x{self.info.height}",
            "-framerate",
            str(self.info.fps),
            "-i",
            cfg.device,
            "-an",
            "-c:v",
            "mjpeg",
            "-q:v",
            "4",
            "-f",
            "mjpeg",
            "-",
        ]
        return args

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopped = False
        self._started_at = time.time()
        if not self.config.mock:
            formats = await asyncio.to_thread(
                probe_v4l2, self.config.device, self.config.v4l2ctl
            )
            self.info = self._build_info(formats)
        self._tasks = [
            asyncio.create_task(self._run(), name="camera-ffmpeg"),
        ]
        await asyncio.sleep(0)
        if not shutil.which(self.config.ffmpeg):
            self._last_error = "ffmpeg not found"
            log.error("ffmpeg не найден: камера недоступна (укажите camera.ffmpeg)")
            self.info.available = False

    async def _run(self) -> None:
        cfg = self.config
        while not self._stopped:
            if not shutil.which(cfg.ffmpeg):
                self.info.available = False
                await asyncio.sleep(10)
                continue
            command = self._command()
            log.info("запуск камеры: %s", " ".join(command[-6:]))
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                self._last_error = str(exc)
                log.error("не удалось запустить ffmpeg: %s", exc)
                await asyncio.sleep(self._restart_delay)
                self._restart_delay = min(10, self._restart_delay * 1.5)
                continue
            self._process = process
            self.info.available = True
            self._restart_delay = 2.0
            stderr_task = asyncio.create_task(self._drain_stderr(process), name="camera-stderr")
            try:
                assert process.stdout is not None
                await self._read_frames(process.stdout)
            except asyncio.CancelledError:
                raise
            finally:
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(process.wait(), timeout=3)
                self._process = None
            if self._stopped:
                return
            log.warning("поток камеры остановился, перезапуск через %.0f c", self._restart_delay)
            await asyncio.sleep(self._restart_delay)
            self._restart_delay = min(15, self._restart_delay * 1.5)

    async def _read_frames(self, stream: asyncio.StreamReader) -> None:
        buffer = b""
        while not self._stopped:
            chunk = await stream.read(65536)
            if not chunk:
                return
            buffer += chunk
            while True:
                start = buffer.find(_SOI)
                if start < 0:
                    buffer = b""
                    break
                if start > 0:
                    buffer = buffer[start:]
                end = buffer.find(_EOI)
                if end < 0:
                    if len(buffer) > 8 * 1024 * 1024:
                        buffer = b""
                    break
                frame = buffer[: end + 2]
                buffer = buffer[end + 2 :]
                self._frames += 1
                self._last_frame_at = time.time()
                try:
                    self._on_frame(frame)
                except Exception:
                    log.exception("ошибка обработчика кадра")

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                self._last_error = text
                log.debug("ffmpeg: %s", text)

    async def stop(self) -> None:
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=3)

    async def record(self, seconds: float, path: Path) -> Path:
        """Record a clip with an independent ffmpeg process."""
        seconds = max(1.0, min(120.0, seconds))
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.config.mock:
            source = ["-f", "lavfi", "-i", f"testsrc2=size={self.info.width}x{self.info.height}:rate={self.info.fps}"]
        else:
            source = [
                "-f",
                "v4l2",
                "-video_size",
                f"{self.info.width}x{self.info.height}",
                "-framerate",
                str(self.info.fps),
                "-i",
                self.config.device,
            ]
        command = [
            self.config.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            *source,
            "-t",
            f"{seconds:.1f}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(path),
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=seconds + 30)
        except TimeoutError:
            process.kill()
            raise RuntimeError("ffmpeg record timed out") from None
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", "replace")[-500:] or "ffmpeg failed")
        return path
