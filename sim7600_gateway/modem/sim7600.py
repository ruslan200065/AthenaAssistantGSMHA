"""High-level SIM7600 driver: calls, SMS, USSD, status, watchdog."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import ModemConfig
from ..log import get_logger
from ..util import mask_numbers, normalize_number, now_iso, percent_from_dbm
from .at_channel import AtChannel, AtResult, Urc
from .errors import ModemNotConnected, ModemTimeout, SmsError
from .pdu import DeliverSms, SmsEncodingError, build_submit, parse_deliver
from .sms_assembler import SmsAssembler
from .state import ACT_CODES, REGISTERED_STATES, REGISTRATION_STATES, ModemSnapshot, ModemState
from .transport import BaseTransport

log = get_logger("modem.sim7600")

EventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]

_HEX = re.compile(r"^[0-9A-Fa-f]{10,}$")
_DTMF = re.compile(r"\+R?X?DTMF:\s*([0-9*#ABCD])")
_CLCC = re.compile(r'\+CLCC:\s*\d+,\d+,\d+,\d+,\d+,"([^"]*)"')


def _quoted(line: str) -> list[str]:
    return re.findall(r'"([^"]*)"', line)


def decode_ussd(text: str | None, dcs: int | None = None) -> str:
    """Decode a +CUSD payload (GSM-7 text or hex-encoded UCS-2).

    UCS-2 is decoded only when the network explicitly says so (DCS 72) or
    when the 8-bit alphabet is requested (DCS 68). Guessing from a hex-looking
    string produced false positives for plain USSD menus.
    """
    if not text:
        return ""
    if dcs in (72, 0x48):
        try:
            return bytes.fromhex(text).decode("utf-16-be", errors="replace")
        except ValueError:
            return text
    if dcs in (68, 0x44) and len(text) % 2 == 0:
        try:
            return bytes.fromhex(text).decode("latin-1")
        except ValueError:
            return text
    return text


class Sim7600:
    """Owns the AT channel and translates it into Athena-level events."""

    def __init__(
        self,
        config: ModemConfig,
        transport: BaseTransport | None = None,
        on_event: EventSink | None = None,
    ):
        self.config = config
        self._on_event = on_event
        self._transport = transport
        self._channel: AtChannel | None = None
        self._status = ModemSnapshot()
        self._started_at = time.time()
        self._stopped = False
        self._urgent: asyncio.Queue[Urc] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._ring_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._recover_lock = asyncio.Lock()

        self._ringing = False
        self._active = False
        self._incoming = False
        self._incoming_emitted = False
        self._active_emitted = False
        self._call_number: str | None = None
        self._call_started_at: float | None = None
        self._answered_at: float | None = None
        self._ring_started_at: float | None = None
        self._call_present = False

        self._ussd_future: asyncio.Future | None = None
        self._ussd_code: str | None = None
        self._operator_checked_at: float = 0.0
        self._sms_assembler = SmsAssembler()
        self._allowed = {normalize_number(item) or item for item in config.allowed_numbers}
        self._rejected = {normalize_number(item) or item for item in config.reject_numbers}

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def snapshot(self) -> ModemSnapshot:
        self._status.uptime_sec = int(time.time() - self._started_at)
        if self._channel is not None:
            self._status.consecutive_timeouts = self._channel.consecutive_timeouts
        self._status.updated_at = time.time()
        return self._status

    @property
    def at_log(self) -> list[dict[str, str]]:
        if self._channel is None:
            return []
        return [{"direction": d, "text": t} for d, t in self._channel.at_log]

    @property
    def channel(self) -> AtChannel | None:
        return self._channel

    async def start(self) -> None:
        if self._channel is not None and self._channel.connected:
            return
        if self._channel is not None:
            with contextlib.suppress(Exception):
                await self._channel.close()
            self._channel = None
        self._stopped = False
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._urc_worker(), name="modem-urc"),
                asyncio.create_task(self._status_loop(), name="modem-status"),
            ]
        await self._open_channel()

    async def _open_channel(self) -> None:
        transport = self._transport or self._build_transport()
        channel = AtChannel(transport, on_urc=self._urgent.put_nowait)
        self._status.state = ModemState.INIT
        self._status.last_error = None
        try:
            await channel.open()
            self._channel = channel
            await self._initialize()
        except Exception as exc:
            if self._channel is channel:
                self._channel = None
            with contextlib.suppress(Exception):
                await channel.close()
            self._status.state = ModemState.OFF
            self._status.last_error = str(exc)
            raise

    def _build_transport(self) -> BaseTransport:
        from .transport import SerialTransport

        return SerialTransport(self.config.port, self.config.baudrate)

    async def stop(self) -> None:
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for timer in (self._ring_task, self._watchdog_task):
            if timer is not None:
                timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await timer
        self._ring_task = None
        self._watchdog_task = None
        while not self._urgent.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._urgent.get_nowait()
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        self._status.state = ModemState.OFF

    async def _initialize(self) -> None:
        channel = self._require_channel()
        failures = 0
        storage = (self.config.sms_storage or "ME").upper()
        for command in self.config.init_sequence:
            if not command.strip():
                continue
            if command.upper().startswith("AT+CPMS="):
                command = f'AT+CPMS="{storage}","{storage}","{storage}"'
            result = await channel.command(command, timeout=5)
            if not result.ok:
                failures += 1
                log.warning("init %s → %s", command, result.error)
                if command.upper() == "ATE0" and result.timed_out:
                    raise ModemTimeout("модем не отвечает на AT")
        await self.refresh_identity()
        await self.refresh()
        self._status.state = ModemState.IDLE if self._status.registered else ModemState.SIM_READY
        if failures:
            log.warning("инициализация завершилась с %d ошибками", failures)
        log.info(
            "модем готов: %s, оператор %s, регистрация %s",
            self._status.model or "SIM7600",
            self._status.operator or "—",
            self._status.registration_text,
        )

    async def recover(self, reason: str) -> None:
        """Watchdog: restart the module and re-open the channel."""
        async with self._recover_lock:
            if self._stopped:
                return
            log.warning("перезапуск модема: %s", reason)
            self._status.last_error = reason
            await self._clear_call_state("recover")
            channel = self._channel
            if channel is not None and self.config.reset_method == "at":
                with contextlib.suppress(Exception):
                    await channel.command("AT+CFUN=1,1", timeout=3)
            if self.config.reset_method == "gpio":
                await asyncio.to_thread(self._gpio_reset)
            if channel is not None:
                with contextlib.suppress(Exception):
                    await channel.close()
            self._channel = None
            self._status.state = ModemState.INIT
            await asyncio.sleep(3)
            for attempt in range(1, 6):
                if self._stopped:
                    return
                try:
                    await self._open_channel()
                    log.info("модем восстановлен (попытка %d)", attempt)
                    return
                except Exception as exc:
                    log.warning("восстановление не удалось: %s", exc)
                    await asyncio.sleep(min(30, attempt * 5))
            self._status.state = ModemState.OFF
            self._status.last_error = "recovery failed"

    async def _clear_call_state(self, reason: str) -> None:
        """Forget call/URC state after a modem reset or channel failure."""
        if self._ring_task is not None:
            self._ring_task.cancel()
            self._ring_task = None
        if self._watchdog_task is not None and self._watchdog_task is not asyncio.current_task():
            self._watchdog_task.cancel()
            self._watchdog_task = None
        while not self._urgent.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._urgent.get_nowait()
        self._ringing = False
        self._active = False
        self._incoming = False
        self._incoming_emitted = False
        self._active_emitted = False
        self._call_present = False
        self._call_number = None
        self._call_started_at = None
        self._answered_at = None
        self._ring_started_at = None
        self._status.caller = None
        self._status.ringing = False
        self._status.call_active = False
        log.info("состояние звонка сброшено (%s)", reason)

    def _gpio_reset(self) -> None:
        pin = self.config.reset_gpio
        if pin <= 0:
            return
        try:
            from pathlib import Path

            base = Path("/sys/class/gpio")
            gpio = base / f"gpio{pin}"
            if not gpio.exists():
                (base / "export").write_text(str(pin))
            (gpio / "direction").write_text("out")
            (gpio / "value").write_text("0")
            time.sleep(0.5)
            (gpio / "value").write_text("1")
        except OSError as exc:
            log.warning("GPIO reset недоступен: %s", exc)

    # ── status ───────────────────────────────────────────────────────────────

    async def refresh_identity(self) -> None:
        channel = self._require_channel()
        for command, handler in (
            ("AT+CGMI", self._parse_manufacturer),
            ("AT+GMM", self._parse_model),
            ("AT+CGMR", self._parse_firmware),
            ("AT+CGSN", self._parse_imei),
            ("AT+CCID", self._parse_iccid),
        ):
            result = await channel.command(command)
            if result.ok and result.lines:
                handler(result.lines[0])
        result = await channel.command("AT+CNUM")
        if result.ok:
            for line in result.lines:
                if line.startswith("+CNUM:"):
                    numbers = _quoted(line)
                    if len(numbers) >= 2 and numbers[1]:
                        self._status.phone_number = normalize_number(numbers[1]) or numbers[1]

    async def refresh(self) -> None:
        channel = self._require_channel()

        cpin = await channel.command("AT+CPIN?")
        if cpin.ok:
            value = cpin.first("+CPIN:")
            sim_state = value.split(":", 1)[1].strip() if value else "unknown"
            self._status.sim_state = sim_state
            self._status.sim_ready = sim_state.upper() == "READY"
        else:
            self._status.sim_state = "absent"
            self._status.sim_ready = False

        csq = await channel.command("AT+CSQ")
        if csq.ok:
            value = csq.first("+CSQ:")
            if value:
                parts = value.split(":", 1)[1].split(",")
                try:
                    rssi = int(parts[0].strip())
                except (ValueError, IndexError):
                    rssi = 99
                if 0 <= rssi <= 31:
                    dbm = -113 + 2 * rssi
                    self._status.signal_dbm = dbm
                    self._status.signal_percent = percent_from_dbm(dbm)

        registration = await self._query_registration("AT+CREG?", "+CREG")
        if registration is None:
            registration = await self._query_registration("AT+CGREG?", "+CGREG")
        if registration is not None:
            self._apply_registration(registration)
        else:
            self._apply_registration(4, emit=False)

        if self._status.operator is None or time.time() - self._operator_checked_at > 300:
            cops = await channel.command("AT+COPS?")
            if cops.ok:
                self._operator_checked_at = time.time()
                value = cops.first("+COPS:")
                if value:
                    quoted = _quoted(value)
                    if quoted:
                        self._status.operator = quoted[0]
                    tail = value.rsplit(",", 1)
                    if len(tail) == 2 and tail[1].strip().isdigit():
                        self._status.network = ACT_CODES.get(int(tail[1].strip()))

        if self._status.network is None:
            cpsi = await channel.command("AT+CPSI?")
            if cpsi.ok:
                value = cpsi.first("+CPSI:")
                if value:
                    sysmode = value.split(":", 1)[1].split(",")[0].strip()
                    self._status.network = sysmode or None

        if (
            self._channel is not None
            and self._channel.consecutive_timeouts >= self.config.watchdog_timeouts
        ):
            await self.recover("AT watchdog: несколько таймаутов подряд")
            return

        if self._status.registered and not self._ringing and not self._active:
            self._status.state = ModemState.IDLE
        elif self._ringing:
            self._status.state = ModemState.RINGING
        elif self._active:
            self._status.state = ModemState.ACTIVE
        elif self._status.state == ModemState.IDLE:
            self._status.state = ModemState.SIM_READY if self._status.sim_ready else ModemState.INIT

    async def _query_registration(self, command: str, prefix: str) -> int | None:
        channel = self._require_channel()
        result = await channel.command(command)
        if not result.ok:
            return None
        value = result.first(prefix + ":")
        if not value:
            return None
        payload = value.split(":", 1)[1].strip()
        if payload.startswith('"'):
            return None
        parts = payload.split(",")
        try:
            status = int(parts[1].strip()) if len(parts) > 1 else int(parts[0].strip())
        except (ValueError, IndexError):
            return None
        return status

    def _apply_registration(self, status: int, emit: bool = True) -> None:
        self._status.registration_status = status
        self._status.registration_text = REGISTRATION_STATES.get(status, "unknown")
        self._status.registered = status in REGISTERED_STATES
        if emit and not self._status.registered:
            log.info("регистрация: %s", self._status.registration_text)

    async def _status_loop(self) -> None:
        while not self._stopped:
            await asyncio.sleep(30)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except ModemNotConnected as exc:
                log.warning("статус: нет связи с модемом (%s)", exc)
                if not self._stopped:
                    await self.recover("AT-канал закрыт")
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("ошибка опроса статуса: %s", exc)

    def _parse_manufacturer(self, line: str) -> None:
        self._status.manufacturer = line.strip() or None

    def _parse_model(self, line: str) -> None:
        self._status.model = line.strip() or None

    def _parse_firmware(self, line: str) -> None:
        self._status.firmware = line.strip() or None

    def _parse_imei(self, line: str) -> None:
        digits = re.sub(r"\D", "", line)
        if 14 <= len(digits) <= 17:
            self._status.imei = digits

    def _parse_iccid(self, line: str) -> None:
        digits = re.sub(r"\D", "", line)
        if len(digits) >= 18:
            self._status.iccid = digits

    # ── calls ────────────────────────────────────────────────────────────────

    async def dial(self, number: str) -> AtResult:
        channel = self._require_channel()
        normalized = normalize_number(number) or number
        if self._status.call_active or self._ringing:
            return AtResult(command="ATD", ok=False, final="BUSY")
        self._call_number = normalized
        self._incoming = False
        self._call_present = True
        self._answered_at = None
        self._status.caller = None
        self._status.state = ModemState.DIALING
        self._start_call_watchdog()
        result = await channel.command(f"ATD{normalized};", timeout=20)
        if result.ok:
            self._status.state = ModemState.DIALING
            self._call_started_at = time.time()
        elif result.final in ("BUSY", "NO ANSWER", "NO CARRIER", "ERROR") or result.timed_out:
            await self._finish_call("failed", answered=False)
        return result

    async def answer(self) -> AtResult:
        channel = self._require_channel()
        if self._active:
            return AtResult(command="ATA", ok=True, final="OK")
        result = await channel.command("ATA", timeout=15)
        if result.ok:
            await self._mark_active()
        elif result.final == "NO CARRIER":
            await self._finish_call("missed", answered=False)
        return result

    async def hangup(self) -> AtResult:
        channel = self._require_channel()
        result = await channel.command("ATH", timeout=10)
        if result.final == "NO CARRIER" and not result.ok:
            # Some firmware sends NO CARRIER instead of OK for ATH.
            result.ok = True
        if self._active or self._ringing:
            await self._finish_call("ended", answered=self._active)
        return result

    async def send_dtmf(self, digits: str) -> AtResult | None:
        channel = self._require_channel()
        last: AtResult | None = None
        for digit in digits:
            if digit not in "0123456789*#ABCD":
                continue
            last = await channel.command(f"AT+VTS={digit}", timeout=5)
            if not last.ok:
                break
        return last

    async def enable_pcm_audio(self) -> bool:
        """Route call audio to the USB interface (16 kHz) for voice/bridge.

        Sequence matches the working SIM7600 reference: reset the route,
        select 16-bit linear PCM at 16 kHz, then enable it with one retry.
        """
        channel = self._channel
        if channel is None or not channel.connected:
            return False
        with contextlib.suppress(Exception):
            await channel.command("AT+CPCMREG=0,1", timeout=5)
        with contextlib.suppress(Exception):
            result = await channel.command("AT+CPCMFRM=1", timeout=5)
            if not result.ok:
                log.debug("AT+CPCMFRM=1 → %s (продолжаю)", result.error)
        result = None
        for attempt in range(2):
            try:
                result = await channel.command("AT+CPCMREG=1", timeout=5)
            except ModemNotConnected:
                return False
            if result.ok:
                break
            if attempt == 0:
                log.debug("AT+CPCMREG=1 → %s, повтор", result.error)
                await asyncio.sleep(0.2)
        if result is None or not result.ok:
            log.warning("AT+CPCMREG=1 → %s", result.error if result else "no response")
            return False
        with contextlib.suppress(Exception):
            check = await channel.command("AT+CPCMFRM?", timeout=5)
            line = check.first("+CPCMFRM:") if check.ok else None
            if line and "1" not in line.split(":", 1)[-1]:
                log.warning("AT+CPCMFRM? → %s (ожидался 16 kHz)", line)
        return True

    async def disable_pcm_audio(self) -> bool:
        """Disable the PCM route; ``CPCMREG=0,1`` is the graceful form."""
        channel = self._channel
        if channel is None or not channel.connected:
            return False
        try:
            result = await channel.command("AT+CPCMREG=0,1", timeout=5)
            if not result.ok:
                result = await channel.command("AT+CPCMREG=0", timeout=5)
        except ModemNotConnected:
            return False
        if not result.ok:
            log.warning("AT+CPCMREG=0 → %s", result.error)
            return False
        return True

    async def hold_call(self) -> bool:
        """Experimental: hold the active call (paired with release_held_calls)."""
        channel = self._channel
        if channel is None or not channel.connected:
            return False
        with contextlib.suppress(Exception):
            result = await channel.command("AT+CHLD=2", timeout=5)
            return result.ok
        return False

    async def release_held_calls(self) -> bool:
        channel = self._channel
        if channel is None or not channel.connected:
            return False
        with contextlib.suppress(Exception):
            result = await channel.command("AT+CHLD=0", timeout=5)
            return result.ok
        return False

    async def _mark_active(self) -> None:
        if self._active:
            return
        self._active = True
        self._call_present = True
        self._ringing = False
        self._status.call_active = True
        self._status.ringing = False
        self._status.state = ModemState.ACTIVE
        self._answered_at = time.time()
        if self._call_started_at is None:
            self._call_started_at = self._answered_at
        if self._status.caller:
            self._call_number = self._status.caller
        if not self._active_emitted:
            self._active_emitted = True
            await self._emit(
                "call.state",
                {
                    "state": "active",
                    "number": self._call_number or "",
                    "direction": "in" if self._incoming else "out",
                },
            )

    async def _finish_call(self, state: str, answered: bool, duration: int | None = None) -> None:
        if not self._call_present:
            return
        self._call_present = False
        incoming = self._incoming
        if answered:
            if duration is None:
                base = self._answered_at or self._call_started_at
                duration = max(0, int(time.time() - base)) if base else 0
        else:
            duration = 0
        number = self._call_number or self._status.caller or ""
        should_notify = (not incoming) or self._incoming_emitted
        self._ringing = False
        self._active = False
        self._status.call_active = False
        self._status.ringing = False
        self._status.caller = None
        self._status.call_started_at = None
        self._status.state = ModemState.IDLE if self._status.registered else ModemState.SIM_READY
        self._call_started_at = None
        self._answered_at = None
        self._call_number = None
        self._active_emitted = False
        self._incoming_emitted = False
        self._incoming = False
        if self._ring_task is not None:
            self._ring_task.cancel()
            self._ring_task = None
        if should_notify:
            await self._emit(
                "call.state",
                {
                    "state": state,
                    "number": number,
                    "duration_sec": duration,
                    "direction": "in" if incoming else "out",
                },
            )

    # ── SMS ──────────────────────────────────────────────────────────────────

    async def send_sms(self, number: str, text: str) -> int:
        channel = self._require_channel()
        destination = normalize_number(number) or number
        try:
            parts = build_submit(destination, text, mr=int(time.time()) & 0xFF)
        except SmsEncodingError as exc:
            raise SmsError(str(exc)) from exc
        self._status.state = ModemState.SMS
        try:
            for part in parts:
                result = await channel.send_sms_pdu(part.pdu, part.tpdu_length)
                if not result.ok:
                    raise SmsError(result.error or "modem rejected SMS")
                self._status.sms_sent += 1
        finally:
            self._status.state = (
                ModemState.IDLE if self._status.registered else ModemState.SIM_READY
            )
        return len(parts)

    async def send_ussd(self, code: str) -> dict[str, Any]:
        channel = self._require_channel()
        if not re.fullmatch(r"[0-9*#+]{1,32}", code):
            return {"ok": False, "error": "invalid ussd code"}
        if self._ussd_future is not None and not self._ussd_future.done():
            return {"ok": False, "error": "another USSD request is in progress"}

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._ussd_future = future
        self._ussd_code = code
        self._status.state = ModemState.USSD
        try:
            result = await channel.command(f'AT+CUSD=1,"{code}",15', timeout=10)
        except Exception:
            self._ussd_future = None
            self._ussd_code = None
            self._status.state = ModemState.IDLE
            raise
        if not result.ok:
            self._ussd_future = None
            self._ussd_code = None
            self._status.state = ModemState.IDLE
            return {"ok": False, "error": result.error}
        try:
            status, message = await asyncio.wait_for(future, timeout=30)
        except TimeoutError:
            return {"ok": False, "error": "no USSD response", "code": code}
        finally:
            self._ussd_future = None
            self._ussd_code = None
            self._status.state = (
                ModemState.IDLE if self._status.registered else ModemState.SIM_READY
            )
        return {
            "ok": status in (0, 1, 2),
            "code": code,
            "status": status,
            "response": message,
        }

    async def read_sms(self, index: int) -> bool:
        channel = self._require_channel()
        result = await channel.command(f"AT+CMGR={index}", timeout=20)
        if not result.ok:
            log.warning("CMGR %s → %s", index, result.error)
            return False
        pdu_line = None
        header_seen = False
        for line in result.lines:
            if line.startswith("+CMGR:"):
                header_seen = True
                continue
            if header_seen and _HEX.match(line):
                pdu_line = line
                break
        if pdu_line is None:
            pdu_line = next((line for line in result.lines if _HEX.match(line)), None)
        if pdu_line is None:
            log.warning("CMGR %s: PDU не найден", index)
            return False
        try:
            message = parse_deliver(pdu_line)
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("не удалось разобрать SMS: %s", exc)
            return False
        await self._deliver_sms(message, index)
        return True

    async def _deliver_sms(self, message: DeliverSms, index: int | None = None) -> None:
        """Assemble UDH segments and emit one event when the SMS is complete."""
        assembled = self._sms_assembler.add(message, index)
        expired = self._sms_assembler.purge_expired()
        if expired:
            await self._delete_sms(expired)
        if assembled is None:
            concat = message.concat or (0, 1, 1)
            log.debug(
                "SMS: получена часть %s/%s ref=%s, жду остальные", concat[2], concat[1], concat[0]
            )
            return
        sender = normalize_number(assembled.sender) or assembled.sender
        log.info("SMS от %s (%d символов)", mask_numbers(sender), len(assembled.text))
        self._status.sms_received += 1
        await self._emit(
            "sms.incoming",
            {
                "number": sender,
                "text": assembled.text,
                "at": assembled.timestamp or now_iso(),
            },
        )
        if assembled.indices:
            await self._delete_sms(assembled.indices)

    async def _delete_sms(self, indices: list[int]) -> None:
        channel = self._channel
        if channel is None:
            return
        for index in indices:
            with contextlib.suppress(Exception):
                await channel.command(f"AT+CMGD={index}", timeout=5)

    # ── URC handling ─────────────────────────────────────────────────────────

    async def _urc_worker(self) -> None:
        while not self._stopped:
            urc = await self._urgent.get()
            try:
                await self._handle_urc(urc)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - never kill the worker
                log.exception("ошибка обработки URC %s", urc.name)

    async def _handle_urc(self, urc: Urc) -> None:
        name = urc.name.upper()
        if name in ("RING", "+CRING"):
            await self._on_ring()
        elif name == "+CLIP":
            numbers = _quoted(urc.line)
            if numbers:
                self._status.caller = normalize_number(numbers[0]) or numbers[0]
            if self._ringing:
                self._schedule_ring_settle(0.1)
        elif name in ("NO CARRIER", "+CEND", "MISSED_CALL"):
            if self._active:
                await self._finish_call("ended", answered=True)
            elif self._ringing:
                await self._finish_call("missed", answered=False)
        elif name == "VOICE CALL":
            upper = urc.line.upper()
            if "BEGIN" in upper:
                await self._mark_active()
            elif "END" in upper:
                match = re.search(r"END[:\s]+(\d+)", urc.line, re.IGNORECASE)
                urc_duration = int(match.group(1)) if match else None
                state = "ended" if self._active else "missed"
                await self._finish_call(
                    state,
                    answered=self._active,
                    duration=urc_duration if self._active else None,
                )
        elif name == "+CMTI":
            numbers = re.findall(r"(\d+)", urc.line.split(":", 1)[-1])
            if numbers:
                await self.read_sms(int(numbers[-1]))
        elif name == "+CMT":
            if urc.body:
                await self._handle_sms_pdu(urc.body)
        elif name == "+CUSD":
            await self._handle_cusd(urc.line)
        elif name in ("+DTMF", "+RXDTMF"):
            match = _DTMF.search(urc.line)
            if match:
                await self._emit("dtmf", {"digit": match.group(1)})
        elif name in ("+CREG", "+CGREG", "+CEREG"):
            payload = urc.line.split(":", 1)[-1].strip()
            parts = payload.split(",")
            try:
                status = int(parts[1]) if len(parts) > 1 else int(parts[0])
            except (ValueError, IndexError):
                return
            self._apply_registration(status)

    async def _on_ring(self) -> None:
        if self._active:
            return
        if not self._ringing:
            self._ringing = True
            self._incoming = True
            self._call_present = True
            self._incoming_emitted = False
            self._active_emitted = False
            self._ring_started_at = time.time()
            self._call_number = None
            self._call_started_at = None
            self._answered_at = None
            self._status.ringing = True
            self._status.state = ModemState.RINGING
            self._status.caller = None
            self._start_call_watchdog()
        self._schedule_ring_settle(0.6)

    def _schedule_ring_settle(self, delay: float) -> None:
        if self._ring_task is not None:
            self._ring_task.cancel()
        self._ring_task = asyncio.create_task(self._ring_settle(delay), name="ring-settle")

    def _start_call_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._call_watchdog(), name="call-watchdog")

    async def _call_watchdog(self) -> None:
        """Reconcile call state with AT+CLCC.

        Some SIM7600 firmwares drop ``NO CARRIER``/``MISSED_CALL`` URCs or never
        send ``VOICE CALL: END``. Without this the core keeps a call ringing
        forever and rejects all new calls as "gateway busy".
        """
        try:
            while self._call_present and not self._stopped:
                await asyncio.sleep(3)
                channel = self._channel
                if channel is None or not self._call_present:
                    return
                try:
                    result = await channel.command("AT+CLCC", timeout=5)
                except ModemNotConnected:
                    return
                if result.timed_out or not result.ok:
                    continue
                entries = [line for line in result.lines if line.startswith("+CLCC:")]
                if not entries:
                    if self._ringing:
                        await self._finish_call("missed", answered=False)
                    elif self._active:
                        await self._finish_call("ended", answered=True)
                    else:
                        await self._finish_call("failed", answered=False)
                    return
                if not self._active:
                    for line in entries:
                        parts = line.split(":", 1)[-1].split(",")
                        if len(parts) > 2 and parts[2].strip() == "0":
                            await self._mark_active()
                            break
        finally:
            if self._watchdog_task is asyncio.current_task():
                self._watchdog_task = None

    async def _ring_settle(self, delay: float) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(delay)
        if not self._ringing:
            return
        if not self._status.caller:
            channel = self._channel
            if channel is not None:
                result = await channel.command("AT+CLCC", timeout=5)
                for line in result.lines:
                    match = _CLCC.search(line)
                    if match and match.group(1):
                        self._status.caller = normalize_number(match.group(1)) or match.group(1)
                        break
        number = self._status.caller
        if number is None:
            log.warning("входящий звонок без определившегося номера")
            if self.config.unknown_callers == "reject":
                await self.hangup()
                return
            if not self._incoming_emitted:
                self._incoming_emitted = True
                self._call_number = None
                self._call_started_at = time.time()
                known = self.config.unknown_callers == "allow"
                await self._emit(
                    "call.incoming",
                    {"number": "", "at": now_iso(), "known": known, "unknown": True},
                )
            return
        if number in self._rejected:
            log.info("звонок отклонён локальной политикой: %s", mask_numbers(number))
            await self.hangup()
            return
        if not self._incoming_emitted:
            self._incoming_emitted = True
            self._status.caller = number
            self._call_number = number
            self._call_started_at = time.time()
            known = number in self._allowed
            await self._emit(
                "call.incoming",
                {"number": number, "at": now_iso(), "known": known},
            )

    async def _handle_sms_pdu(self, pdu: str) -> None:
        try:
            message = parse_deliver(pdu)
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("не удалось разобрать входящую SMS: %s", exc)
            return
        await self._deliver_sms(message)

    async def _handle_cusd(self, line: str) -> None:
        payload = line.split(":", 1)[-1]
        parts = _split_respecting_quotes(payload)
        try:
            status = int(parts[0].strip())
        except (ValueError, IndexError):
            return
        text = parts[1].strip().strip('"') if len(parts) > 1 else ""
        dcs = None
        if len(parts) > 2:
            with contextlib.suppress(ValueError):
                dcs = int(parts[2].strip())
        message = decode_ussd(text, dcs)
        future = self._ussd_future
        if future is not None and not future.done():
            future.set_result((status, message))
        if self._ussd_code is not None:
            await self._emit(
                "device.event",
                {
                    "event": "ussd.result",
                    "data": {"code": self._ussd_code, "response": message},
                },
            )

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            result = self._on_event(event, data)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("не удалось доставить событие %s", event)

    def _require_channel(self) -> AtChannel:
        if self._channel is None:
            raise ModemNotConnected("модем не инициализирован")
        return self._channel


def _split_respecting_quotes(payload: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for ch in payload:
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif ch == "," and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts
