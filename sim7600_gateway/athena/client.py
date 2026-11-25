"""Athena Core WebSocket client (device-protocol/1).

Owns the session: hello/welcome, heartbeat, command acks, event delivery and
reconnect with exponential backoff. GSM-specific work lives in the bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import socket
import ssl
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import AthenaConfig
from ..log import get_logger
from ..util import now_iso
from . import frames

log = get_logger("athena.client")

CommandHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
StatusProvider = Callable[[], dict[str, Any]]

BACKOFF = (1, 2, 4, 8, 16, 30, 60)


class AthenaAuthError(Exception):
    pass


class AthenaReplaced(Exception):
    pass


class AthenaRevoked(Exception):
    pass


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def http_base_url(ws_url: str) -> str:
    """Derive ``http(s)://host[:port]`` from the core WebSocket URL."""
    value = (ws_url or "").strip()
    if value.startswith("wss://"):
        value = "https://" + value[6:]
    elif value.startswith("ws://"):
        value = "http://" + value[5:]
    parsed = urlparse(value)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


class AthenaClient:
    def __init__(
        self,
        config: AthenaConfig,
        state_dir: Path,
        on_command: CommandHandler,
        status_provider: StatusProvider | None = None,
        on_frame: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        on_audio: Callable[[bytes], Awaitable[None] | None] | None = None,
    ):
        self.config = config
        self._state_dir = state_dir
        self._on_command = on_command
        self._status_provider = status_provider
        self._on_frame = on_frame
        self._on_audio = on_audio
        self._state_path = state_dir / "athena_state.json"

        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._connected = False
        self._fatal: str | None = None
        self._welcome: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._last_welcome_at: float | None = None
        self._attempt = 0
        self._last_command_id = self._load_last_command_id()
        self._processed: dict[int, dict[str, Any]] = {}
        self._started_at = time.time()

    # ── public ───────────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def welcome(self) -> dict[str, Any] | None:
        return self._welcome

    def status(self) -> dict[str, Any]:
        welcome = self._welcome or {}
        return {
            "enabled": self.config.enabled,
            "connected": self._connected,
            "fatal": self._fatal,
            "last_error": self._last_error,
            "last_welcome_at": self._last_welcome_at,
            "url": self.config.url,
            "home": (welcome.get("home") or {}).get("name"),
            "device": (welcome.get("device") or {}).get("name"),
            "room": (welcome.get("device") or {}).get("room_name"),
            "features": welcome.get("features") or {},
            "wake_words": welcome.get("wake_words") or [],
            "reconnect_attempt": self._attempt,
            "last_command_id": self._last_command_id,
        }

    async def start(self) -> None:
        if not self.config.enabled:
            log.info("интеграция с Athena выключена в конфиге")
            return
        if not self.config.url:
            log.error("athena.url не задан — связь с ядром невозможна")
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="athena-client")

    async def stop(self) -> None:
        self._stopping = True
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._connected = False

    async def send_event(self, frame: dict[str, Any]) -> bool:
        """Send an event frame. No retry across reconnects (protocol rule)."""
        if not self._connected:
            log.warning("событие %s не отправлено: нет связи с ядром", frame.get("type"))
            return False
        try:
            await asyncio.wait_for(self._send(frame), timeout=5)
            return True
        except Exception as exc:
            log.warning("не удалось отправить событие %s: %s", frame.get("type"), exc)
            return False

    async def send_bridge(self, target: dict[str, Any]) -> bool:
        frame = frames.call_bridge(
            room=str(target.get("room") or ""),
            target_endpoint_id=target.get("target_endpoint_id"),
            endpoint=target.get("endpoint"),
            call_number=target.get("call_number"),
        )
        return await self.send_event(frame)

    async def send_frame(self, frame: dict[str, Any]) -> bool:
        if not self._connected:
            return False
        try:
            await asyncio.wait_for(self._send(frame), timeout=5)
            return True
        except Exception as exc:
            log.warning("не удалось отправить кадр %s: %s", frame.get("type"), exc)
            return False

    async def send_binary(self, data: bytes) -> None:
        ws = self._ws
        if ws is None or not self._connected:
            return
        async with self._send_lock:
            await ws.send(data)

    async def send_diag(self, snapshot: dict[str, Any]) -> bool:
        return await self.send_event(frames.diag_modem(snapshot))

    # ── session loop ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._session()
            except AthenaAuthError as exc:
                self._fatal = str(exc)
                self._last_error = str(exc)
                log.error("авторизация в Athena не удалась: %s", exc)
                return
            except AthenaReplaced:
                self._fatal = "replaced by a newer connection"
                self._last_error = "заменён новым подключением этого же endpoint"
                log.error("сессия заменена новым подключением — переподключение остановлено")
                return
            except AthenaRevoked:
                self._last_error = "token revoked"
                log.error("токен отозван, повтор через 10 минут")
                self._connected = False
                await asyncio.sleep(600)
                continue
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc)
                log.warning("соединение с Athena потеряно: %s", exc)
            if self._stopping:
                return
            delay = BACKOFF[min(self._attempt, len(BACKOFF) - 1)]
            self._attempt += 1
            delay *= 1 + random.random() * 0.2
            log.info("переподключение к Athena через %.1f c", delay)
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        import websockets  # noqa: PLC0415 - keep import cost at startup

        ssl_context: ssl.SSLContext | None = None
        if self.config.url.startswith("wss://") and not self.config.verify_tls:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        log.info("подключение к Athena: %s", self.config.url)
        async with websockets.connect(
            self.config.url,
            open_timeout=10,
            close_timeout=5,
            max_size=2 * 1024 * 1024,
            ping_interval=None,
            ssl=ssl_context,
        ) as ws:
            self._ws = ws
            hello = frames.hello(
                self.config.token,
                hardware=self._hardware_name(),
                device_id=self.config.device_id,
                room_hint=self.config.room_hint,
                ip=local_ip(),
            )
            await ws.send(json.dumps(hello, ensure_ascii=False))
            first = await asyncio.wait_for(ws.recv(), timeout=10)
            message = self._decode(first)
            if message is None:
                raise ConnectionError("ядро прислало не-JSON кадр вместо welcome")
            if message.get("type") == "error":
                code = message.get("code")
                text = message.get("message") or code or "unknown"
                if code in ("auth", "not_found"):
                    raise AthenaAuthError(text)
                if code == "revoked":
                    raise AthenaRevoked(text)
                raise ConnectionError(f"core error: {text}")
            if message.get("type") != "welcome":
                raise ConnectionError(f"ожидался welcome, пришёл {message.get('type')!r}")

            self._welcome = message
            self._connected = True
            self._fatal = None
            self._last_error = None
            self._last_welcome_at = time.time()
            self._attempt = 0
            home = (message.get("home") or {}).get("name")
            device = (message.get("device") or {}).get("name")
            log.info("подключено к дому «%s», устройство «%s»", home, device)
            await self._send_heartbeat()

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="athena-heartbeat")
            try:
                await self._read_loop(ws)
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                self._connected = False
                self._ws = None

    async def _read_loop(self, ws: Any) -> None:
        while True:
            try:
                raw = await ws.recv()
            except Exception as exc:
                code = getattr(ws, "close_code", None)
                if code == 4000:
                    raise AthenaReplaced() from exc
                if code == 1008:
                    raise AthenaRevoked("session closed with 1008") from exc
                raise
            message = self._decode(raw)
            if message is None:
                if isinstance(raw, bytes) and self._on_audio is not None:
                    await self._on_audio(raw)
                continue
            try:
                await self._handle_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad frame must not tear down the whole WS session.
                log.exception("кадр %s не обработан", message.get("type"))

    async def _handle_message(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "command":
            await self._handle_command(message)
        elif kind == "heartbeat.ack":
            return
        elif kind == "error":
            log.warning("ошибка ядра: %s — %s", message.get("code"), message.get("message"))
        elif kind == "welcome":
            self._welcome = message
        elif self._on_frame is not None:
            # audio.url / assistant.reply / asr.* / intercom.* are consumed by
            # the bridge, not by the command dispatcher.
            result = self._on_frame(message)
            if asyncio.iscoroutine(result):
                await result
        else:
            log.debug("кадр от ядра пропущен: %s", kind)

    async def _handle_command(self, message: dict[str, Any]) -> None:
        command_id = message.get("id")
        if not isinstance(command_id, int):
            await self._send(frames.command_ack(0, "failed", "invalid command id"))
            return
        if command_id <= self._last_command_id:
            cached = self._processed.get(command_id)
            log.info("команда %s уже обработана, повторный ack", command_id)
            await self._send(
                frames.command_ack(
                    command_id,
                    (cached or {}).get("status", "done"),
                    (cached or {}).get("error"),
                )
            )
            return
        try:
            result = await self._on_command(message)
        except Exception as exc:  # command must never kill the session
            log.exception("команда %s упала", command_id)
            result = {"status": "failed", "error": str(exc)}
        result = result or {"status": "done"}
        status = result.get("status", "done")
        if status not in ("delivered", "done", "failed"):
            status = "failed"
        error = result.get("error")
        self._processed[command_id] = {"status": status, "error": error}
        if len(self._processed) > 200:
            oldest = sorted(self._processed)[:-100]
            for key in oldest:
                self._processed.pop(key, None)
        self._last_command_id = command_id
        self._save_last_command_id()
        await self._send(frames.command_ack(command_id, status, error))
        log.info(
            "команда %s (%s) → %s%s",
            command_id,
            message.get("kind"),
            status,
            f" ({error})" if error else "",
        )

    async def _heartbeat_loop(self) -> None:
        interval = max(10, min(30, self.config.heartbeat_seconds))
        while True:
            await asyncio.sleep(interval)
            try:
                await self._send_heartbeat()
            except Exception as exc:
                log.warning("heartbeat не отправлен: %s", exc)
                ws = self._ws
                if ws is not None:
                    with contextlib.suppress(Exception):
                        await ws.close()
                return

    async def _send_heartbeat(self) -> None:
        snapshot = self._status_provider() if self._status_provider else {}
        degraded = not snapshot.get("registered", False) or snapshot.get("state") in ("off", "init")
        frame = frames.heartbeat(
            "degraded" if degraded else "online",
            snapshot.get("signal_dbm"),
            int(time.time() - self._started_at),
            ip=local_ip(),
        )
        await self._send(frame)

    # ── transport helpers ────────────────────────────────────────────────────

    async def _send(self, frame: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("websocket is not connected")
        async with self._send_lock:
            await ws.send(json.dumps(frame, ensure_ascii=False))

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _hardware_name(self) -> str:
        if self._status_provider is not None:
            snapshot = self._status_provider()
            if snapshot.get("model"):
                return str(snapshot["model"])
        return "SIM7600"

    # ── persistence ──────────────────────────────────────────────────────────

    def _load_last_command_id(self) -> int:
        try:
            data = json.loads(self._state_path.read_text("utf-8"))
            return int(data.get("last_command_id", 0))
        except (OSError, ValueError, TypeError):
            return 0

    def _save_last_command_id(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"last_command_id": self._last_command_id, "at": now_iso()},
                ensure_ascii=False,
            )
            temp = self._state_path.with_suffix(".tmp")
            temp.write_text(payload, "utf-8")
            temp.replace(self._state_path)
        except OSError as exc:
            log.warning("не удалось сохранить состояние Athena: %s", exc)
