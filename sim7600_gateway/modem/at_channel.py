"""AT channel: line framing, command/URC demultiplexing, SMS prompt handling."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..log import get_logger
from ..util import mask_numbers
from .errors import ModemNotConnected
from .transport import BaseTransport, TransportClosed

log = get_logger("modem.at")

_LINE_BREAK = re.compile(rb"\r\n|\r|\n")
_NUMBER = re.compile(r"\+?\d[\d\s\-()]{5,}\d")

# Lines that may only be unsolicited results, never command answers.
_DEFINITE_URC = (
    "RING",
    "+CRING",
    "+CLIP",
    "+CMTI",
    "+CMT",
    "+CDSI",
    "+CDS",
    "+CUSD",
    "NO CARRIER",
    "MISSED_CALL",
    "VOICE CALL",
    "SMS DONE",
    "PB DONE",
    "RDY",
    "*ATREADY",
    "+CGEV",
    "+CTZV",
    "+CTZE",
    "+DTMF",
    "+RXDTMF",
)

# Lines that are answers to queries but may also arrive unsolicited.
_AMBIGUOUS_URC = (
    "+CPIN",
    "+CREG",
    "+CGREG",
    "+CEREG",
    "+CLCC",
    "+CEND",
)


@dataclass(slots=True)
class Urc:
    name: str
    line: str
    body: str | None = None


@dataclass(slots=True)
class AtResult:
    command: str
    ok: bool = False
    final: str | None = None
    lines: list[str] = field(default_factory=list)
    prompt: bool = False
    timed_out: bool = False

    @property
    def error(self) -> str | None:
        if self.ok:
            return None
        if self.timed_out:
            return "timeout"
        return self.final or "unknown error"

    def first(self, prefix: str) -> str | None:
        for line in self.lines:
            if line.startswith(prefix):
                return line
        return None


@dataclass
class _Pending:
    command: str
    expect_prompt: bool = False
    lines: list[str] = field(default_factory=list)
    final: str | None = None
    prompt_seen: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    prompt: asyncio.Event = field(default_factory=asyncio.Event)


class AtChannel:
    def __init__(
        self,
        transport: BaseTransport,
        on_urc: Callable[[Urc], None] | None = None,
    ):
        self._transport = transport
        self._on_urc = on_urc
        self._pending: _Pending | None = None
        self._urc_header: tuple[str, str] | None = None
        self._lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._connected = False
        self._closed = False
        self._timeouts = 0
        self.urc_log: deque[Urc] = deque(maxlen=50)
        self.at_log: deque[tuple[str, str]] = deque(maxlen=50)

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def consecutive_timeouts(self) -> int:
        return self._timeouts

    async def open(self) -> None:
        await self._transport.open()
        self._closed = False
        self._connected = True
        self._reader_task = asyncio.create_task(self._reader_loop(), name="at-reader")
        log.info("AT-канал открыт")

    async def close(self) -> None:
        self._closed = True
        self._connected = False
        task = self._reader_task
        self._reader_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, TransportClosed):
                pass
        await self._transport.close()
        log.info("AT-канал закрыт")

    # ── commands ─────────────────────────────────────────────────────────────

    async def command(self, line: str, timeout: float = 5.0) -> AtResult:
        async with self._lock:
            return await self._command_locked(line, timeout)

    async def _command_locked(self, line: str, timeout: float) -> AtResult:
        if not self._connected:
            raise ModemNotConnected("AT channel is not connected")
        pending = _Pending(command=line)
        self._pending = pending
        self._trace(">>", line)
        try:
            await self._transport.write(line.encode("ascii", "replace") + b"\r")
        except (TransportClosed, OSError) as exc:
            self._pending = None
            self._fail_link(exc)
            raise ModemNotConnected(str(exc)) from exc
        try:
            await asyncio.wait_for(pending.done.wait(), timeout)
            self._timeouts = 0
        except TimeoutError:
            self._timeouts += 1
            return AtResult(command=line, ok=False, timed_out=True, lines=pending.lines)
        finally:
            if self._pending is pending:
                self._pending = None
        return AtResult(
            command=line,
            ok=pending.final == "OK",
            final=pending.final,
            lines=pending.lines,
        )

    async def send_sms_pdu(
        self,
        pdu: str,
        tpdu_length: int,
        prompt_timeout: float = 10.0,
        send_timeout: float = 60.0,
    ) -> AtResult:
        """AT+CMGS → wait for '>' → PDU + Ctrl-Z → wait for +CMGS/OK."""
        async with self._lock:
            if not self._connected:
                raise ModemNotConnected("AT channel is not connected")

            command = f"AT+CMGS={tpdu_length}"
            pending = _Pending(command=command, expect_prompt=True)
            self._pending = pending
            self._trace(">>", command)
            prompt_task = asyncio.create_task(pending.prompt.wait())
            done_task = asyncio.create_task(pending.done.wait())
            try:
                await self._transport.write(command.encode("ascii") + b"\r")
                await asyncio.wait(
                    {prompt_task, done_task},
                    timeout=prompt_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except (TransportClosed, OSError) as exc:
                if self._pending is pending:
                    self._pending = None
                self._fail_link(exc)
                raise ModemNotConnected(str(exc)) from exc
            finally:
                for task in (prompt_task, done_task):
                    task.cancel()
                for task in (prompt_task, done_task):
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            if not pending.prompt_seen:
                if self._pending is pending:
                    self._pending = None
                if pending.done.is_set():
                    # The modem rejected CMGS before the prompt (ERROR/CMS ERROR).
                    return AtResult(
                        command=command,
                        ok=False,
                        final=pending.final,
                        lines=pending.lines,
                    )
                self._timeouts += 1
                return AtResult(command=command, ok=False, timed_out=True, lines=pending.lines)

            payload = pdu.encode("ascii") + b"\x1a"
            self._trace(">>", f"<PDU {tpdu_length} bytes>")
            try:
                await self._transport.write(payload)
            except (TransportClosed, OSError) as exc:
                self._fail_link(exc)
                raise ModemNotConnected(str(exc)) from exc

            pending.expect_prompt = False
            self._pending = pending
            try:
                await asyncio.wait_for(pending.done.wait(), send_timeout)
                self._timeouts = 0
            except TimeoutError:
                self._timeouts += 1
                return AtResult(
                    command=command,
                    ok=False,
                    timed_out=True,
                    prompt=True,
                    lines=pending.lines,
                )
            finally:
                if self._pending is pending:
                    self._pending = None
            return AtResult(
                command=command,
                ok=pending.final == "OK",
                final=pending.final,
                lines=pending.lines,
                prompt=True,
            )

    # ── reader ───────────────────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        buffer = b""
        while not self._closed:
            try:
                chunk = await self._transport.read()
            except TransportClosed:
                self._connected = False
                log.warning("порт закрылся, AT-канал остановлен")
                self._abort_pending("NO CARRIER")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("ошибка чтения порта: %s", exc)
                self._connected = False
                self._abort_pending("ERROR")
                return
            if not chunk:
                continue
            buffer += chunk
            buffer = self._process_buffer(buffer)

    def _process_buffer(self, buffer: bytes) -> bytes:
        while True:
            match = _LINE_BREAK.search(buffer)
            if match is None:
                break
            # A trailing bare CR may be the first half of CRLF.
            if match.group() == b"\r" and match.end() == len(buffer):
                break
            raw = buffer[: match.start()]
            buffer = buffer[match.end() :]
            self._dispatch(raw.decode("utf-8", "replace").strip())

        pending = self._pending
        if pending is not None and pending.expect_prompt and not pending.prompt_seen:
            position = buffer.find(b">")
            if position >= 0:
                buffer = buffer[position + 1 :]
                pending.prompt_seen = True
                pending.prompt.set()
        elif pending is None:
            # A late prompt after a timeout must not be mistaken for the next
            # CMGS prompt (that would send the PDU into a previous session).
            stripped = buffer.lstrip(b" \t\r\n")
            if stripped.startswith(b">"):
                buffer = stripped[1:]
        return buffer

    def _dispatch(self, line: str) -> None:
        if not line:
            return

        if self._urc_header is not None:
            name, header = self._urc_header
            self._urc_header = None
            self._emit_urc(Urc(name=name, line=header, body=line))
            return

        if line.startswith("+CMT:") or line.startswith("+CDS:"):
            self._urc_header = (line.split(":", 1)[0], line)
            return

        pending = self._pending
        if pending is not None:
            if line == pending.command:
                return  # echo before ATE0 takes effect
            if self._is_final(line, pending.command):
                pending.final = line
                pending.done.set()
                return
            if self._is_definite_urc(line):
                self._emit_urc(Urc(name=line.split(":", 1)[0].strip(), line=line))
                return
            if line.startswith("+CMGS:") or line.startswith("+CMGW:"):
                pending.lines.append(line)
                return
            pending.lines.append(line)
            return

        # No command in flight: everything interesting is a URC.
        if self._is_urc(line):
            self._emit_urc(Urc(name=line.split(":", 1)[0].strip(), line=line))

    @staticmethod
    def _is_final(line: str, command: str = "") -> bool:
        if line in ("OK", "ERROR"):
            return True
        if line.startswith("+CME ERROR") or line.startswith("+CMS ERROR"):
            return True
        if line in ("BUSY", "NO ANSWER", "NO DIALTONE"):
            return True
        if line == "NO CARRIER":
            upper = command.upper()
            return (
                upper.startswith("ATH")
                or upper.startswith("AT+CHUP")
                or upper.startswith("ATD")
                or upper.startswith("ATA")
            )
        return False

    @staticmethod
    def _is_definite_urc(line: str) -> bool:
        return any(line == prefix or line.startswith(prefix + ":") for prefix in _DEFINITE_URC)

    @staticmethod
    def _is_urc(line: str) -> bool:
        if AtChannel._is_definite_urc(line):
            return True
        return any(line == prefix or line.startswith(prefix + ":") for prefix in _AMBIGUOUS_URC)

    def _emit_urc(self, urc: Urc) -> None:
        self.urc_log.append(urc)
        self._trace("<<", urc.line if urc.body is None else f"{urc.line} <body>")
        if self._on_urc is None:
            return
        try:
            self._on_urc(urc)
        except Exception:  # pragma: no cover - handler must never kill the reader
            log.exception("обработчик URC упал на %s", urc.name)

    def _abort_pending(self, final: str) -> None:
        pending = self._pending
        if pending is not None:
            pending.final = final
            pending.done.set()

    def _fail_link(self, exc: Exception) -> None:
        self._connected = False
        log.warning("AT-транспорт недоступен: %s", exc)

    def _trace(self, direction: str, text: str) -> None:
        # Both directions: incoming +CLIP/+CUSD may contain a phone number.
        safe = mask_numbers(text)
        if len(safe) > 200:
            safe = safe[:200] + "…"
        self.at_log.append((direction, safe))
        log.debug("%s %s", direction, safe)
