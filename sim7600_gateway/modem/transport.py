"""Transport abstractions: real serial port, fake/scripted transport."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from typing import Protocol


class TransportClosed(ConnectionError):
    pass


class BaseTransport(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def write(self, data: bytes) -> None: ...

    async def read(self) -> bytes: ...


class SerialTransport:
    """pyserial transport with a reader thread feeding an asyncio queue."""

    def __init__(self, port: str, baudrate: int = 115200, read_timeout: float = 0.2):
        self.port = port
        self.baudrate = baudrate
        self.read_timeout = read_timeout
        self._serial = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        try:
            import serial  # noqa: PLC0415 - optional import for test environments
        except ImportError as exc:  # pragma: no cover - dependency is required on device
            raise TransportClosed("pyserial is not installed") from exc

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._stop.clear()
        try:
            self._serial = serial.Serial(
                self.port,
                self.baudrate,
                timeout=self.read_timeout,
                write_timeout=3,
            )
        except Exception as exc:  # serial.SerialException and OS errors
            raise TransportClosed(f"cannot open {self.port}: {exc}") from exc
        self._thread = threading.Thread(target=self._reader, name="serial-reader", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._serial.read(1024) if self._serial else b""
            except Exception:
                break
            if not chunk:
                continue
            loop = self._loop
            if loop is None or loop.is_closed():
                break
            loop.call_soon_threadsafe(self._queue.put_nowait, chunk)
        loop = self._loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._queue.put_nowait, b"")

    async def write(self, data: bytes) -> None:
        async with self._write_lock:
            serial_port = self._serial
            if serial_port is None:
                raise TransportClosed("serial port is not open")
            await asyncio.to_thread(serial_port.write, data)

    async def read(self) -> bytes:
        chunk = await self._queue.get()
        if not chunk:
            raise TransportClosed("serial port closed")
        return chunk

    async def close(self) -> None:
        self._stop.set()
        serial_port = self._serial
        self._serial = None
        if serial_port is not None:
            try:
                await asyncio.to_thread(serial_port.close)
            except Exception:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 1.0)


class FakeTransport:
    """Scripted transport for tests and demos.

    ``handler`` receives everything written to the port and returns chunks that
    should be pushed back to the reader. Chunks may also be pushed manually via
    :meth:`push` to emulate unsolicited results.
    """

    def __init__(self, handler: Callable[[bytes], Sequence[bytes] | None] | None = None):
        self.handler = handler
        self.writes: list[bytes] = []
        self.opened = False
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False
        self._queue.put_nowait(b"")

    async def write(self, data: bytes) -> None:
        self.writes.append(data)
        if self.handler is not None:
            chunks = self.handler(data)
            for chunk in chunks or ():
                self._queue.put_nowait(chunk)

    async def read(self) -> bytes:
        chunk = await self._queue.get()
        if not chunk:
            raise TransportClosed("fake transport closed")
        return chunk

    def push(self, chunk: str | bytes) -> None:
        if isinstance(chunk, str):
            chunk = chunk.encode("ascii")
        self._queue.put_nowait(chunk)
