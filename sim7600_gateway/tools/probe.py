"""Hardware probe: verify the modem port and the camera before running the gateway."""

from __future__ import annotations

import argparse
import asyncio
import sys

from ..camera.backend import probe_v4l2
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


def probe_camera(device: str, control: str) -> int:
    formats = probe_v4l2(device, control)
    if not formats:
        print(f"✗ не удалось получить форматы {device} (нужен v4l2-ctl и Linux)")
        return 1
    print(f"✓ {device}")
    for name, sizes in formats.items():
        for entry in sizes[:6]:
            fps = max(entry["fps"]) if entry["fps"] else "?"
            print(f"    {name}: {entry['width']}x{entry['height']} @ {fps} fps")
    return 0


def main(argv: list[str] | None = None) -> int:
    config = load_config()
    parser = argparse.ArgumentParser(description="Probe SIM7600 and the USB camera")
    parser.add_argument("--port", default=config.modem.port)
    parser.add_argument("--baud", type=int, default=config.modem.baudrate)
    parser.add_argument("--camera", default=config.camera.device)
    parser.add_argument("--v4l2ctl", default=config.camera.v4l2ctl)
    parser.add_argument("--modem-only", action="store_true")
    parser.add_argument("--camera-only", action="store_true")
    args = parser.parse_args(argv)

    code = 0
    if not args.camera_only:
        code |= asyncio.run(probe_modem(args.port, args.baud))
    if not args.modem_only:
        code |= probe_camera(args.camera, args.v4l2ctl)
    return code


if __name__ == "__main__":
    sys.exit(main())
