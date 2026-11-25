"""A small SIM7600 simulator.

It speaks enough AT to drive the whole gateway without hardware, including
incoming calls, SMS and USSD. Used by ``--mock`` demos and integration tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Sequence

from ..log import get_logger
from .pdu import build_deliver

log = get_logger("modem.mock")


class SimulatedModem:
    def __init__(
        self,
        phone_number: str = "+79990001122",
        operator: str = "Athena Lab",
        model: str = "SIM7600E-H",
        imei: str = "860000000000001",
        iccid: str = "8970100000000000000",
    ):
        self.phone_number = phone_number
        self.operator = operator
        self.model = model
        self.imei = imei
        self.iccid = iccid

        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffer = b""
        self._sms_payload = b""
        self._awaiting_payload = False
        self._echo = True
        self._closed = False

        self._sms_index = 0
        self._sms: dict[int, str] = {}
        self._call: str | None = None  # ringing | active | dialing
        self._call_number: str | None = None
        self._call_started: float | None = None
        self._ring_timer: asyncio.TimerHandle | None = None
        self._ussd_code: str | None = None

    # ── transport API ────────────────────────────────────────────────────────

    async def open(self) -> None:
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        if self._ring_timer is not None:
            self._ring_timer.cancel()
            self._ring_timer = None
        self._queue.put_nowait(b"")

    async def write(self, data: bytes) -> None:
        self._buffer += data
        if self._awaiting_payload:
            end = self._buffer.find(b"\x1a")
            if end < 0:
                return
            payload = self._buffer[:end].strip()
            self._buffer = self._buffer[end + 1 :]
            self._awaiting_payload = False
            self._accept_sms_payload(payload)
            return
        while b"\r" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\r", 1)
            line = raw.decode("ascii", "replace").strip()
            if line:
                self._handle(line)
        # A trailing LF from a CRLF-separated write is harmless.
        self._buffer = self._buffer.lstrip(b"\n")

    async def read(self) -> bytes:
        chunk = await self._queue.get()
        if not chunk:
            raise ConnectionError("mock modem closed")
        return chunk

    # ── injection helpers ────────────────────────────────────────────────────

    def inject_call(self, number: str) -> None:
        if self._call is not None:
            return
        self._call = "ringing"
        self._call_number = number
        self._push(f'\r\n+CLIP: "{number}",145,"",0\r\n')
        self._ring()
        log.info("симулятор: входящий звонок %s", number)

    def _ring(self) -> None:
        self._push("\r\nRING\r\n")
        self._ring_timer = asyncio.get_running_loop().call_later(3.0, self._ring)

    def inject_sms(self, number: str, text: str) -> None:
        pdu = build_deliver(number, text)
        self._sms_index += 1
        self._sms[self._sms_index] = pdu
        self._push(f'\r\n+CMTI: "ME",{self._sms_index}\r\n')
        log.info("симулятор: SMS от %s (%d символов)", number, len(text))

    def inject_dtmf(self, digit: str) -> None:
        self._push(f"\r\n+DTMF: {digit}\r\n")

    def remote_hangup(self) -> None:
        if self._call is None:
            return
        duration = 5
        self._call = None
        self._call_number = None
        if self._ring_timer is not None:
            self._ring_timer.cancel()
            self._ring_timer = None
        self._push(f"\r\nVOICE CALL: END: {duration:06d}\r\n")

    def silent_hangup(self) -> None:
        """Drop the call without any URC (worst-case firmware behaviour)."""
        self._call = None
        self._call_number = None
        if self._ring_timer is not None:
            self._ring_timer.cancel()
            self._ring_timer = None

    # ── internals ────────────────────────────────────────────────────────────

    def _push(self, chunk: str | bytes) -> None:
        data = chunk.encode("ascii") if isinstance(chunk, str) else chunk
        self._queue.put_nowait(data)

    def _ok(self) -> None:
        self._push("\r\nOK\r\n")

    def _error(self, code: str = "ERROR") -> None:
        self._push(f"\r\n{code}\r\n")

    def _echo_line(self, line: str) -> None:
        if self._echo:
            self._push(line + "\r\n")

    def _handle(self, line: str) -> None:
        upper = line.upper()
        log.debug("mock << %s", upper if not upper.startswith("ATD") else "ATD***")

        if upper in ("AT", "ATZ", "AT&F", "ATE0", "ATE1"):
            self._echo_line(line)
            if upper == "ATE0":
                self._echo = False
            elif upper == "ATE1":
                self._echo = True
            self._ok()
            return

        if upper.startswith(("AT+CMEE", "AT+CMGF", "AT+CNMI", "AT+CLIP", "AT+CRC", "AT+DDET")):
            self._echo_line(line)
            self._ok()
            return
        if upper.startswith(("AT+CPMS=", "AT+CPMS?")):
            self._echo_line(line)
            self._push('\r\n+CPMS: "ME",0,20,"ME",0,20,"ME",0,20\r\n')
            self._ok()
            return
        if upper in ("AT+CPIN?",):
            self._push("\r\n+CPIN: READY\r\n")
            self._ok()
            return
        if upper == "AT+CSQ":
            self._push("\r\n+CSQ: 24,99\r\n")
            self._ok()
            return
        if upper == "AT+CREG?":
            self._push("\r\n+CREG: 0,1\r\n")
            self._ok()
            return
        if upper == "AT+CGREG?":
            self._push("\r\n+CGREG: 0,1\r\n")
            self._ok()
            return
        if upper == "AT+COPS?":
            self._push(f'\r\n+COPS: 0,0,"{self.operator}",7\r\n')
            self._ok()
            return
        if upper == "AT+CPSI?":
            self._push("\r\n+CPSI: LTE,Online,250-01,0,1850,0,0,0,0,0\r\n")
            self._ok()
            return
        if upper == "AT+CGMI":
            self._push("\r\nSIMCOM_LTD\r\n")
            self._ok()
            return
        if upper in ("AT+GMM", "AT+CGMM"):
            self._push(f"\r\n{self.model}\r\n")
            self._ok()
            return
        if upper == "AT+CGMR":
            self._push("\r\nLE20B04SIM7600G22\r\n")
            self._ok()
            return
        if upper == "AT+CGSN":
            self._push(f"\r\n{self.imei}\r\n")
            self._ok()
            return
        if upper in ("AT+CCID", "AT+ICCID"):
            self._push(f"\r\n+CCID: {self.iccid}\r\n")
            self._ok()
            return
        if upper == "AT+CNUM":
            self._push(f'\r\n+CNUM: "","{self.phone_number}",145\r\n')
            self._ok()
            return
        if upper.startswith("ATD"):
            number = line[3:].rstrip(";")
            if self._call is not None:
                self._error("BUSY")
                return
            self._call = "dialing"
            self._call_number = number
            self._ok()
            asyncio.get_running_loop().call_later(1.0, self._begin_call)
            return
        if upper == "ATA":
            if self._call != "ringing":
                self._error("ERROR")
                return
            self._ok()
            if self._ring_timer is not None:
                self._ring_timer.cancel()
                self._ring_timer = None
            asyncio.get_running_loop().call_later(0.4, self._begin_call)
            return
        if upper in ("ATH", "AT+CHUP"):
            self._ok()
            if self._call is not None:
                self.remote_hangup()
            return
        if upper == "AT+CLCC":
            if self._call in ("ringing", "active", "dialing") and self._call_number:
                status = 4 if self._call == "ringing" else 0
                self._push(f'\r\n+CLCC: 1,1,{status},0,0,"{self._call_number}",145\r\n')
                self._ok()
                return
            self._ok()
            return
        if upper.startswith("AT+VTS="):
            digit = line.split("=", 1)[1].strip('"')
            self._ok()
            if digit:
                self._push(f"\r\n+DTMF: {digit[0]}\r\n")
            return
        if upper.startswith("AT+CUSD="):
            match = re.search(r'AT\+CUSD=1,"([^"]*)"', line, re.IGNORECASE)
            self._ok()
            if match:
                self._ussd_code = match.group(1)
                asyncio.get_running_loop().call_later(0.7, self._ussd_reply)
            return
        if upper.startswith("AT+CMGS="):
            self._awaiting_payload = True
            self._push("\r\n> ")
            return
        if upper.startswith("AT+CMGR="):
            match = re.search(r"(\d+)", upper.split("=", 1)[1])
            index = int(match.group(1)) if match else -1
            pdu = self._sms.get(index)
            if pdu is None:
                self._error("+CMS ERROR: 321")
                return
            self._push(f"\r\n+CMGR: 0,,{len(pdu) // 2}\r\n{pdu}\r\n")
            self._ok()
            return
        if upper.startswith("AT+CMGD="):
            match = re.search(r"(\d+)", upper.split("=", 1)[1])
            if match:
                self._sms.pop(int(match.group(1)), None)
            self._ok()
            return
        if upper.startswith("AT+CMGL"):
            if not self._sms:
                self._ok()
                return
            for index, pdu in sorted(self._sms.items()):
                self._push(
                    f'\r\n+CMGL: {index},0,,{len(pdu) // 2}\r\n{pdu}\r\n'
                )
            self._ok()
            return
        if upper.startswith("AT+CFUN=1,1"):
            self._ok()
            asyncio.get_running_loop().call_later(1.0, self._reboot)
            return
        if upper.startswith("AT+CTZU") or upper.startswith("AT+CTZR"):
            self._ok()
            return
        if upper.startswith("AT+CLIR") or upper.startswith("AT+CSMS"):
            self._ok()
            return
        self._echo_line(line)
        self._error()

    def _accept_sms_payload(self, payload: bytes) -> None:
        text = payload.decode("ascii", "replace")
        if not re.fullmatch(r"[0-9A-Fa-f]+", text):
            self._error("+CMS ERROR: 304")
            return
        self._push("\r\n+CMGS: 42\r\n")
        self._ok()
        log.info("симулятор: отправлена SMS, %d байт PDU", len(text) // 2)

    def _begin_call(self) -> None:
        if self._call is None:
            return
        self._call = "active"
        self._call_started = asyncio.get_running_loop().time()
        self._push("\r\nVOICE CALL: BEGIN\r\n")

    def _ussd_reply(self) -> None:
        text = {
            "*100#": "Баланс: 350.00 руб",
            "*111#": "1. Тариф  2. Остатки  3. Помощь",
            "#100#": "Ваш номер: " + self.phone_number,
        }.get(self._ussd_code or "", "Ответ сети: OK")
        escaped = text.replace('"', "'")
        if escaped.isascii():
            self._push(f'\r\n+CUSD: 0,"{escaped}",15\r\n')
        else:
            encoded = escaped.encode("utf-16-be").hex().upper()
            self._push(f'\r\n+CUSD: 0,"{encoded}",72\r\n')
        self._ussd_code = None

    def _reboot(self) -> None:
        self._call = None
        self._sms.clear()
        self._push("\r\nRDY\r\n")
        self._push("\r\n*ATREADY: 1\r\n")


def simulate_sequence(script: Sequence[str]) -> list[bytes]:
    """Test helper: convert newline-separated mock output into chunks."""
    return [part.encode() for part in script if part]


def cancel_timer(timer: asyncio.TimerHandle | None) -> None:
    if timer is not None:
        with contextlib.suppress(Exception):
            timer.cancel()
