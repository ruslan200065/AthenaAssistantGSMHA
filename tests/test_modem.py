"""SIM7600 driver end-to-end against the built-in simulator."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.config import ModemConfig  # noqa: E402
from sim7600_gateway.modem.mock import SimulatedModem  # noqa: E402
from sim7600_gateway.modem.sim7600 import Sim7600  # noqa: E402


def test_full_call_and_sms_cycle() -> None:
    async def scenario() -> None:
        events: list[tuple[str, dict]] = []

        async def on_event(event: str, data: dict) -> None:
            events.append((event, data))

        simulator = SimulatedModem()
        modem = Sim7600(ModemConfig(mock=True), transport=simulator, on_event=on_event)
        await modem.start()
        try:
            snapshot = modem.snapshot
            assert snapshot.registered
            assert snapshot.signal_dbm == -65
            assert snapshot.operator == "Athena Lab"

            assert await modem.send_sms("+79991234567", "Привет, это Афина!") == 1

            simulator.inject_sms("+79995554433", "Проверка входящей SMS")
            await asyncio.sleep(0.3)
            sms_events = [data for kind, data in events if kind == "sms.incoming"]
            assert sms_events and sms_events[0]["text"] == "Проверка входящей SMS"

            simulator.inject_call("+79991234567")
            await asyncio.sleep(1.0)
            assert modem.snapshot.ringing
            incoming = [data for kind, data in events if kind == "call.incoming"]
            assert incoming and incoming[0]["number"] == "+79991234567"

            assert (await modem.answer()).ok
            await asyncio.sleep(0.2)
            states = [data["state"] for kind, data in events if kind == "call.state"]
            assert "active" in states

            simulator.inject_dtmf("5")
            await asyncio.sleep(0.2)
            assert any(kind == "dtmf" and data["digit"] == "5" for kind, data in events)

            assert (await modem.hangup()).ok
            await asyncio.sleep(0.2)
            states = [data["state"] for kind, data in events if kind == "call.state"]
            assert "ended" in states
        finally:
            await modem.stop()

    asyncio.run(scenario())


def test_ussd_reply() -> None:
    async def scenario() -> None:
        events: list[tuple[str, dict]] = []

        async def on_event(event: str, data: dict) -> None:
            events.append((event, data))

        modem = Sim7600(ModemConfig(mock=True), transport=SimulatedModem(), on_event=on_event)
        await modem.start()
        try:
            result = await modem.send_ussd("*100#")
            assert result["ok"]
            assert "Баланс" in result["response"]
            assert any(
                kind == "device.event" and data["event"] == "ussd.result" for kind, data in events
            )
        finally:
            await modem.stop()

    asyncio.run(scenario())


def test_silent_hangup_is_reconciled_by_clcc() -> None:
    async def scenario() -> None:
        events: list[tuple[str, dict]] = []

        async def on_event(event: str, data: dict) -> None:
            events.append((event, data))

        simulator = SimulatedModem()
        modem = Sim7600(ModemConfig(mock=True), transport=simulator, on_event=on_event)
        await modem.start()
        try:
            simulator.inject_call("+79991234567")
            await asyncio.sleep(1.0)
            assert modem.snapshot.ringing
            # Firmware drops the call without NO CARRIER / VOICE CALL: END.
            simulator.silent_hangup()
            deadline = asyncio.get_running_loop().time() + 7
            while asyncio.get_running_loop().time() < deadline:
                if any(kind == "call.state" and data["state"] == "missed" for kind, data in events):
                    break
                await asyncio.sleep(0.2)
            assert any(kind == "call.state" and data["state"] == "missed" for kind, data in events)
            assert not modem.snapshot.ringing
        finally:
            await modem.stop()

    asyncio.run(scenario())


def test_hangup_uses_at_chup() -> None:
    async def scenario() -> None:
        from sim7600_gateway.modem.at_channel import AtChannel
        from sim7600_gateway.modem.transport import FakeTransport

        commands: list[str] = []

        def handler(data: bytes):
            line = data.decode("ascii", "replace").strip()
            commands.append(line)
            return [b"\r\nOK\r\n"]

        transport = FakeTransport(handler)
        channel = AtChannel(transport)
        await channel.open()
        modem = Sim7600(ModemConfig(mock=True), transport=transport)
        modem._channel = channel
        try:
            result = await modem.hangup()
            assert result.ok
            assert commands and commands[0] == "AT+CHUP"
        finally:
            await channel.close()

    asyncio.run(scenario())


def test_outgoing_call() -> None:
    async def scenario() -> None:
        events: list[tuple[str, dict]] = []

        async def on_event(event: str, data: dict) -> None:
            events.append((event, data))

        modem = Sim7600(ModemConfig(mock=True), transport=SimulatedModem(), on_event=on_event)
        await modem.start()
        try:
            assert (await modem.dial("+79990000000")).ok
            await asyncio.sleep(1.2)
            assert modem.snapshot.call_active
            assert (await modem.hangup()).ok
        finally:
            await modem.stop()

    asyncio.run(scenario())
