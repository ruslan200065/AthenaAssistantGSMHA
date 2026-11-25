"""Hardware probe: verify the SIM7600 AT port before running the gateway."""

from __future__ import annotations

import argparse
import asyncio
import sys

from ..config import load_config
from ..modem.at_channel import AtChannel
from ..modem.transport import SerialTransport

COMMANDS = [
    "ATI",
    "AT+CPIN?",
    "AT+CSQ",
    "AT+CREG?",
    "AT+COPS?",
    "AT+CGSN",
    "AT+CCID",
    "AT+GMM",
    "AT+CGMR",
]


async def probe_modem(port: str, baudrate: int) -> int:
    transport = SerialTransport(port, baudrate)
    channel = AtChannel(transport)
    print(f"→ открываю {port} @ {baudrate}")
    try:
        await channel.open()
    except Exception as exc:
        print(f"✗ не удалось открыть порт: {exc}")
        return 1
    failures = 0
    try:
        for command in COMMANDS:
            result = await channel.command(command, timeout=5)
            mark = "✓" if result.ok else "✗"
            if not result.ok:
                failures += 1
            print(f"{mark} {command}")
            for line in result.lines:
                print(f"    {line}")
            if result.final and result.final != "OK":
                print(f"    ! {result.final}")
    finally:
        await channel.close()
    if failures:
        print(f"\n{failures} команд завершились ошибкой. Проверьте AT-порт (обычно ttyUSB2).")
        return 1
    print("\nМодем отвечает корректно.")
    return 0


def main(argv: list[str] | None = None) -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Probe the SIM7600 AT port")
    parser.add_argument("--port", default=config.modem.port)
    parser.add_argument("--baud", type=int, default=config.modem.baudrate)
    args = parser.parse_args(argv)
    return asyncio.run(probe_modem(args.port, args.baud))


if __name__ == "__main__":
    sys.exit(main())
