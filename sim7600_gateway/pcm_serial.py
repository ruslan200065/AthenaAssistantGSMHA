"""Raw PCM transport for the SIM7600 USB audio port.

Some SIM7600 firmware exposes call audio as a second serial interface
(usually ``/dev/ttyUSB4``) carrying 16 kHz mono s16le frames. This is the
transport used by the working reference implementation; ALSA is only a
fallback for firmwares that expose a USB audio class card instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

from .log import get_logger

log = get_logger("audio.pcm_serial")


class PcmClosed(ConnectionError):
    """The PCM serial port was closed."""


class SerialPcm:
    """Async reader/writer over a pyserial PCM port.

    The reader thread only moves bytes into an asyncio queue; framing stays in
    :meth:`read_frame`, so a 3200-byte USB read can satisfy several 640-byte
    voice frames without loss.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        *,
        read_timeout: float = 0.1,
        write_timeout: float = 0.75,
    ):
        self.port = port
        self.baudrate = baudrate
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self._serial = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffer = bytearray()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = asyncio.Lock()
        self._closed = False

    @property
    def is_open(self) -> bool:
        return self._serial is not None and not self._closed

    async def open(self) -> None:
        if self.is_open:
            return
        try:
            import serial  # noqa: PLC0415 - optional for tests
        except ImportError as exc:  # pragma: no cover
            raise PcmClosed("pyserial is not installed") from exc

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._buffer.clear()
        self._stop.clear()
        self._closed = False
        try:
            self._serial = await asyncio.to_thread(
                serial.Serial,
                self.port,
                self.baudrate,
                timeout=self.read_timeout,
                write_timeout=self.write_timeout,
            )
        except Exception as exc:
            self._serial = None
            raise PcmClosed(f"cannot open PCM port {self.port}: {exc}") from exc
        self._thread = threading.Thread(
            target=self._reader, name="pcm-serial-reader", daemon=True
        )
        self._thread.start()
        log.info("PCM-порт открыт: %s @ %d", self.port, self.baudrate)

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._serial.read(4096) if self._serial else b""
            except Exception:
                break
            if not chunk:
                continue
            loop = self._loop
            if loop is None or loop.is_closed():
                break
            loop.call_soon_threadsafe(self._queue.put_nowait, chunk)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._queue.put_nowait, b"")
        log.debug("поток чтения PCM завершился: %s", self.port)

    async def read_frame(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = await self._queue.get()
            if not chunk:
                raise PcmClosed("PCM port closed")
            self._buffer.extend(chunk)
        frame = bytes(self._buffer[:size])
        del self._buffer[:size]
        return frame

    async def write(self, data: bytes) -> None:
        serial_port = self._serial
        if serial_port is None or self._closed:
            raise PcmClosed("PCM port is not open")
        async with self._write_lock:
            await asyncio.to_thread(serial_port.write, data)

    async def close(self) -> None:
        self._closed = True
        self._stop.set()
        serial_port, self._serial = self._serial, None
        if serial_port is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(serial_port.close)
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 1.0)
        log.info("PCM-порт закрыт: %s", self.port)


def serial_route(port: str) -> str:
    """Marker stored in the capture/playback tuple for serial mode."""
    return f"serial:{port}"


def route_port(value: str | None) -> str | None:
    if value and value.startswith("serial:"):
        return value.split(":", 1)[1]
    return None
