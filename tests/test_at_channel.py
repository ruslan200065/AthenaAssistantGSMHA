"""AT channel tests with a scripted fake transport."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.modem.at_channel import AtChannel, Urc  # noqa: E402
from sim7600_gateway.modem.pdu import build_deliver  # noqa: E402
from sim7600_gateway.modem.transport import FakeTransport  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def test_command_ok_and_lines() -> None:
    async def scenario() -> None:
        def handler(data: bytes):
            if data.startswith(b"AT+CSQ"):
                return [b"\r\n+CSQ: 20,99\r\n", b"\r\nOK\r\n"]
            return [b"\r\nOK\r\n"]

        channel = AtChannel(FakeTransport(handler))
        await channel.open()
        result = await channel.command("AT+CSQ")
        assert result.ok
        assert result.first("+CSQ:") == "+CSQ: 20,99"
        await channel.close()

    _run(scenario())


def test_error_result() -> None:
    async def scenario() -> None:
        channel = AtChannel(FakeTransport(lambda data: [b"\r\n+CME ERROR: 10\r\n"]))
        await channel.open()
        result = await channel.command("AT+CPIN?")
        assert not result.ok
        assert result.final == "+CME ERROR: 10"
        await channel.close()

    _run(scenario())


def test_urc_routing_during_command() -> None:
    async def scenario() -> None:
        urcs: list[Urc] = []
        channel = AtChannel(
            FakeTransport(
                lambda data: [b"\r\nRING\r\n", b'\r\n+CLIP: "+79991234567",145\r\n', b"\r\nOK\r\n"]
            ),
            on_urc=urcs.append,
        )
        await channel.open()
        result = await channel.command("AT")
        assert result.ok
        await asyncio.sleep(0.05)
        names = [urc.name for urc in urcs]
        assert "RING" in names
        assert "+CLIP" in names
        await channel.close()

    _run(scenario())


def test_cmt_body_assembly() -> None:
    async def scenario() -> None:
        urcs: list[Urc] = []
        pdu = build_deliver("+79991234567", "Привет")
        channel = AtChannel(FakeTransport(), on_urc=urcs.append)
        await channel.open()
        channel._dispatch(f"+CMT: ,{len(pdu) // 2}")
        channel._dispatch(pdu)
        await asyncio.sleep(0.05)
        assert len(urcs) == 1
        assert urcs[0].name == "+CMT"
        assert urcs[0].body == pdu
        await channel.close()

    _run(scenario())


def test_sms_prompt_flow() -> None:
    async def scenario() -> None:
        seen: list[bytes] = []

        def handler(data: bytes):
            seen.append(data)
            if data.startswith(b"AT+CMGS="):
                return [b"\r\n> "]
            if data.endswith(b"\x1a"):
                return [b"\r\n+CMGS: 7\r\n", b"\r\nOK\r\n"]
            return [b"\r\nOK\r\n"]

        channel = AtChannel(FakeTransport(handler))
        await channel.open()
        result = await channel.send_sms_pdu("0001000B919799214365F7", 15, send_timeout=2)
        assert result.ok
        assert result.prompt
        assert any(line.startswith("+CMGS:") for line in result.lines)
        assert seen[-1].endswith(b"\x1a")
        await channel.close()

    _run(scenario())


def test_timeout_counts() -> None:
    async def scenario() -> None:
        channel = AtChannel(FakeTransport(lambda data: None))
        await channel.open()
        result = await channel.command("AT+CSQ", timeout=0.05)
        assert result.timed_out
        assert channel.consecutive_timeouts == 1
        await channel.close()

    _run(scenario())
