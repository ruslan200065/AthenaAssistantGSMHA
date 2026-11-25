"""Console entry point: ``python -m sim7600_gateway`` / ``sim7600-gateway``."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .app import create_app
from .config import load_config, validate
from .log import get_logger, setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sim7600-gateway",
        description="SIM7600 GSM + USB camera gateway for Athena Core and Home Assistant",
    )
    parser.add_argument("--config", "-c", default=None, help="path to config.toml")
    parser.add_argument("--host", default=None, help="override HTTP listen host")
    parser.add_argument("--port", type=int, default=None, help="override HTTP listen port")
    parser.add_argument("--log-level", default=None, help="DEBUG | INFO | WARNING | ERROR")
    parser.add_argument("--mock", action="store_true", help="simulate modem and camera (demo)")
    parser.add_argument("--no-athena", action="store_true", help="do not connect to Athena Core")
    parser.add_argument("--modem-port", default=None, help="override serial port, e.g. COM5")
    parser.add_argument("--camera-device", default=None, help="override camera device")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)

    if args.mock:
        config.modem.mock = True
        config.camera.mock = True
        config.camera.enabled = True
        if not config.athena.token.startswith("dev_") or "REPLACE" in config.athena.token:
            config.athena.enabled = False
    if args.no_athena:
        config.athena.enabled = False
    if args.host:
        config.bridge.host = args.host
    if args.port:
        config.bridge.port = args.port
    if args.log_level:
        config.bridge.log_level = args.log_level
    if args.modem_port:
        config.modem.port = args.modem_port
    if args.camera_device:
        config.camera.device = args.camera_device

    setup_logging(config.bridge.log_level)
    log = get_logger("main")
    config.ensure_dirs()

    warnings = validate(config)
    for warning in warnings:
        log.warning("конфигурация: %s", warning)

    log.info("Athena SIM7600 Gateway v%s", __version__)
    log.info("HTTP: http://%s:%s", config.bridge.host, config.bridge.port)
    log.info(
        "модем: %s%s, камера: %s%s",
        config.modem.port,
        " (симулятор)" if config.modem.mock else "",
        config.camera.device,
        " (симулятор)" if config.camera.mock else "",
    )
    if config.athena.enabled:
        log.info("Athena Core: %s", config.athena.url)
    else:
        log.info("Athena Core: отключено (локальный режим)")

    try:
        import uvicorn  # noqa: PLC0415
    except ImportError:
        log.error("uvicorn не установлен: pip install -r requirements.txt")
        return 2

    app = create_app(config)
    try:
        uvicorn.run(
            app,
            host=config.bridge.host,
            port=config.bridge.port,
            log_level="warning",
            access_log=False,
        )
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
