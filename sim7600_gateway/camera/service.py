"""Camera service: frame fan-out, snapshots, MJPEG, motion detection."""

from __future__ import annotations

import asyncio
import contextlib
import io
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import CameraConfig
from ..log import get_logger
from .backend import FfmpegCamera

log = get_logger("camera.service")

EventListener = Callable[[str, dict[str, Any]], Awaitable[None] | None]

BOUNDARY = "athena-frame"


class CameraService:
    def __init__(self, config: CameraConfig, on_event: EventListener | None = None):
        self.config = config
        self._on_event = on_event
        self._camera: FfmpegCamera | None = None
        self._latest: bytes | None = None
        self._last_frame_at: float | None = None
        self._frame_event = asyncio.Event()
        self._subscribers = 0
        self._motion_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
        self._motion_task: asyncio.Task | None = None
        self._previous_frame: bytes | None = None
        self._last_motion_at = 0.0
        self._motion_events = 0
        self._snapshots: list[dict[str, Any]] = []

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.enabled:
            log.info("камера выключена в конфиге")
            return
        self._camera = FfmpegCamera(self.config, on_frame=self._on_frame)
        await self._camera.start()
        if self.config.motion_detection:
            self._motion_task = asyncio.create_task(self._motion_loop(), name="camera-motion")

    async def stop(self) -> None:
        if self._motion_task is not None:
            self._motion_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._motion_task
            self._motion_task = None
        if self._camera is not None:
            await self._camera.stop()
            self._camera = None

    # ── frames ───────────────────────────────────────────────────────────────

    def _on_frame(self, frame: bytes) -> None:
        self._latest = frame
        self._last_frame_at = time.time()
        self._frame_event.set()
        if self.config.motion_detection and self._motion_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._motion_queue.get_nowait()
        if self.config.motion_detection:
            with contextlib.suppress(asyncio.QueueFull):
                self._motion_queue.put_nowait(frame)

    async def latest_frame(self, timeout: float = 3.0) -> bytes | None:
        self._frame_event.clear()
        if self._latest is not None and time.time() - (self._last_frame_at or 0) < 2:
            return self._latest
        try:
            await asyncio.wait_for(self._frame_event.wait(), timeout)
        except TimeoutError:
            return None
        return self._latest

    async def snapshot(self) -> tuple[bytes | None, str]:
        frame = await self.latest_frame()
        if frame is None:
            return None, "camera frame is not available"
        return frame, "image/jpeg"

    async def save_snapshot(self, reason: str = "manual") -> dict[str, Any] | None:
        frame = await self.latest_frame()
        if frame is None:
            log.warning("снимок невозможен: нет кадра")
            return None
        directory = Path(self.config.snapshot_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = directory / f"{reason}_{stamp}.jpg"
        await asyncio.to_thread(path.write_bytes, frame)
        record = {
            "path": str(path),
            "filename": path.name,
            "reason": reason,
            "at": time.time(),
            "url": "/api/camera/snapshot.jpg",
        }
        self._snapshots.append(record)
        self._snapshots = self._snapshots[-50:]
        await self._emit("camera.snapshot", record)
        return record

    async def snapshot_on_ring(self) -> dict[str, Any] | None:
        if not self.config.enabled or not self.config.snapshot_on_ring:
            return None
        return await self.save_snapshot("ring")

    async def record(self, seconds: float | None = None) -> dict[str, Any] | None:
        if self._camera is None:
            return None
        duration = seconds or self.config.clip_seconds
        directory = Path(self.config.snapshot_dir).expanduser().parent / "clips"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = directory / f"clip_{stamp}.mp4"
        await self._camera.record(duration, path)
        return {
            "path": str(path),
            "filename": path.name,
            "duration_sec": duration,
            "url": f"/api/camera/clips/{path.name}",
        }

    async def mjpeg_stream(self) -> AsyncIterator[bytes]:
        self._subscribers += 1
        try:
            yield b""
            last: bytes | None = None
            while True:
                frame = await self.latest_frame(timeout=5)
                if frame is None:
                    yield b"\r\n"
                    continue
                if frame is last:
                    continue
                last = frame
                header = (
                    f"--{BOUNDARY}\r\n"
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode()
                yield header + frame + b"\r\n"
        finally:
            self._subscribers -= 1

    # ── motion ───────────────────────────────────────────────────────────────

    async def _motion_loop(self) -> None:
        while True:
            frame = await self._motion_queue.get()
            if self._previous_frame is None:
                self._previous_frame = frame
                continue
            try:
                changed = await asyncio.to_thread(
                    frame_difference, self._previous_frame, frame
                )
            except Exception as exc:  # Pillow missing/invalid frame
                log.debug("motion detection: %s", exc)
                self._previous_frame = frame
                continue
            self._previous_frame = frame
            now = time.time()
            if changed >= self.config.motion_threshold and now - self._last_motion_at >= self.config.motion_cooldown:
                self._last_motion_at = now
                self._motion_events += 1
                await self._emit(
                    "camera.motion",
                    {
                        "changed": round(changed, 4),
                        "threshold": self.config.motion_threshold,
                        "at": now,
                    },
                )

    # ── info ─────────────────────────────────────────────────────────────────

    def info(self) -> dict[str, Any]:
        camera_info = self._camera.info.to_dict() if self._camera else {
            "device": self.config.device,
            "mock": self.config.mock,
            "width": self.config.width,
            "height": self.config.height,
            "fps": self.config.fps,
            "available": False,
        }
        return {
            **camera_info,
            "enabled": self.config.enabled,
            "frames": self._camera.frames if self._camera else 0,
            "last_frame_at": self._last_frame_at,
            "subscribers": self._subscribers,
            "motion_detection": self.config.motion_detection,
            "motion_events": self._motion_events,
            "snapshot_on_ring": self.config.snapshot_on_ring,
            "snapshots": list(reversed(self._snapshots[-10:])),
            "last_error": self._camera.last_error if self._camera else None,
        }

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            result = self._on_event(event, data)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("ошибка доставки события камеры %s", event)


def frame_difference(previous: bytes, current: bytes, size: tuple[int, int] = (64, 36)) -> float:
    """Share of pixels that changed noticeably between two JPEG frames."""
    from PIL import Image, ImageChops  # noqa: PLC0415 - optional at import time

    with Image.open(io.BytesIO(previous)) as first, Image.open(io.BytesIO(current)) as second:
        left = first.convert("L").resize(size)
        right = second.convert("L").resize(size)
        diff = ImageChops.difference(left, right)
        histogram = diff.histogram()
    total = size[0] * size[1]
    changed = sum(histogram[25:])
    return changed / total if total else 0.0


def mjpeg_headers() -> dict[str, str]:
    return {
        "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
    }
