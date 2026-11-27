"""Gateway orchestration: modem + Athena core + local event bus."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from typing import Any

from . import __version__, athena
from .athena import frames
from .audio_bridge import AudioBridge, audio_devices
from .bus import EventBus
from .config import Config
from .log import get_logger
from .modem.errors import ModemError
from .modem.mock import SimulatedModem
from .modem.sim7600 import Sim7600
from .util import mask_numbers, now_iso
from .voice_call import CallVoiceSession

log = get_logger("bridge")

BRIDGE_CONFIRM_TIMEOUT = 12.0


class GatewayBridge:
    def __init__(self, config: Config):
        self.config = config
        self.bus = EventBus()
        self.started_at = time.time()
        self._stopping = False
        self._modem_task: asyncio.Task | None = None
        self.simulator: SimulatedModem | None = None


        transport = None
        if config.modem.mock:
            transport = SimulatedModem()
            self.simulator = transport
        self.modem = Sim7600(config.modem, transport=transport, on_event=self._on_modem_event)

        self.audio = AudioBridge(
            config.audio_bridge,
            self.modem,
            on_capture=self._on_audio_capture,
            on_stopped=self._on_audio_stopped,
        )

        self.athena = athena.AthenaClient(
            config.athena,
            state_dir=config.state_dir,
            on_command=self._on_command,
            status_provider=lambda: self.modem.snapshot.to_dict(),
            on_frame=self._on_protocol_frame,
            on_audio=self._on_core_audio,
        )

        self.voice = CallVoiceSession(
            config.audio_bridge,
            send_json=self.athena.send_event,
            send_binary=self.athena.send_binary,
            token=config.athena.token,
            http_base_url=athena.http_base_url(config.athena.url),
            on_stopped=self._on_voice_stopped,
        )

        self._sms_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._calls_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._ussd_log: deque[dict[str, Any]] = deque(maxlen=50)
        self._intercom_session: int | None = None
        self._bridge_pending = False
        self._bridge_timeout_task: asyncio.Task | None = None
        self._pcm_enabled = False
        self._audio_error: str | None = None
        self._background: set[asyncio.Task] = set()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        log.info("запуск шлюза «%s» v%s", self.config.bridge.name, __version__)
        await self.bus.publish(
            "gateway.start",
            {
                "name": self.config.bridge.name,
                "version": __version__,
                "mock": self.config.modem.mock,
            },
        )
        if self.config.modem.enabled:
            self._modem_task = asyncio.create_task(
                self._modem_supervisor(), name="modem-supervisor"
            )
        else:
            log.info("модем выключен в конфиге (modem.enabled = false)")
        await self.athena.start()

    async def stop(self) -> None:
        self._stopping = True
        await self.bus.publish("gateway.stop", {})
        await self.athena.stop()
        if self._modem_task is not None:
            self._modem_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._modem_task
            self._modem_task = None
        self._cancel_bridge_timeout()
        await self.audio.stop()
        await self._stop_voice_call()
        await self.modem.stop()
        for task in list(self._background):
            task.cancel()
        for task in list(self._background):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._background.clear()
        log.info("шлюз остановлен")

    async def _modem_supervisor(self) -> None:
        while not self._stopping:
            try:
                await self.modem.start()
                channel = self.modem.channel
                if channel is None or not channel.connected:
                    raise RuntimeError("AT-канал не открылся")
                await self.bus.publish("modem.ready", self.modem.snapshot.to_dict())
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("модем не поднялся: %s — повтор через 10 c", exc)
                await self.bus.publish("modem.error", {"error": str(exc)})
                await asyncio.sleep(10)

    # ── modem → Athena / bus ────────────────────────────────────────────────

    async def _on_modem_event(self, event: str, data: dict[str, Any]) -> None:
        if event == "call.incoming":
            self._calls_log.append(
                {
                    "direction": "in",
                    "number": data.get("number"),
                    "at": now_iso(),
                    "state": "ringing",
                }
            )
            log.info("входящий звонок от %s", mask_numbers(data.get("number")) or "неизвестный")
            await self.bus.publish(event, data)
            if self.athena.connected:
                await self.athena.send_event(
                    frames.call_incoming(
                        data.get("number") or "",
                        data.get("at") or now_iso(),
                    )
                )
            await self._apply_incoming_policy(data)
            return

        if event == "call.state":
            state = data.get("state")
            if state in ("ended", "missed", "failed"):
                self._calls_log.append(
                    {
                        "direction": "in" if data.get("direction") == "in" else "out",
                        "number": data.get("number"),
                        "at": now_iso(),
                        "state": state,
                        "duration_sec": data.get("duration_sec", 0),
                    }
                )
            await self.bus.publish(event, data)
            if self.athena.connected:
                await self.athena.send_event(
                    frames.call_state(
                        state or "ended",
                        data.get("number") or "",
                        data.get("duration_sec"),
                        data.get("direction"),
                    )
                )
            if state in ("ended", "missed", "failed"):
                self._bridge_pending = False
                self._cancel_bridge_timeout()
                await self._stop_voice_call()
                await self._end_intercom("hangup")
            return

        if event == "sms.incoming":
            self._sms_log.append(
                {
                    "direction": "in",
                    "number": data.get("number"),
                    "text": data.get("text"),
                    "at": data.get("at") or now_iso(),
                }
            )
            log.info(
                "SMS от %s (%d символов)",
                mask_numbers(data.get("number")),
                len(data.get("text") or ""),
            )
            await self.bus.publish(event, data)
            if self.athena.connected:
                await self.athena.send_event(
                    frames.sms_incoming(data.get("number") or "", data.get("text") or "")
                )
            return

        if event == "dtmf":
            await self.bus.publish(event, data)
            if self.athena.connected:
                await self.athena.send_event(frames.dtmf(data.get("digit") or ""))
            return

        if event == "device.event":
            name = data.get("event")
            payload = data.get("data") or {}
            if name == "ussd.result":
                self._ussd_log.append(
                    {
                        "code": payload.get("code"),
                        "response": payload.get("response"),
                        "at": now_iso(),
                    }
                )
            await self.bus.publish(event, data)
            if self.athena.connected:
                await self.athena.send_event(frames.device_event(name or "device.event", payload))
            return

        await self.bus.publish(event, data)

    async def _apply_incoming_policy(self, data: dict[str, Any]) -> None:
        known = bool(data.get("known"))
        number = data.get("number") or ""
        modem_config = self.config.modem
        if modem_config.bridge_room and (known or not modem_config.bridge_known_only):
            if not await self._audio_available():
                log.warning("входящий звонок: аудио недоступно, не отвечаю (%s)", self._audio_error)
                return
            await asyncio.sleep(0.4)
            result = await self.modem.answer()
            if not result.ok:
                log.warning("не удалось ответить на звонок: %s", result.error)
                return
            bridged = await self.start_bridge(modem_config.bridge_room, None)
            if bridged.get("ok"):
                log.info(
                    "звонок %s соединён с комнатой «%s»",
                    mask_numbers(number),
                    modem_config.bridge_room,
                )
            else:
                log.warning("call.bridge не отправлен: %s", bridged.get("error"))
                await self._bridge_failed(str(bridged.get("error") or "call.bridge failed"))
            return
        if known and modem_config.auto_answer:
            # Never answer without a working audio path: the caller would hear
            # silence and the core would consider the call served.
            if not await self._audio_available():
                log.warning("автоответ отменён: аудио недоступно (%s)", self._audio_error)
                return
            await asyncio.sleep(max(0.0, modem_config.auto_answer_delay))
            ok, error = await self._answer_with_voice()
            if not ok:
                log.warning("автоответ не удался: %s", error)

    async def _audio_available(self) -> bool:
        if not self.config.audio_bridge.enabled:
            self._audio_error = "audio_bridge.enabled = false"
            return False
        if self.audio.active or self.voice.active:
            return True
        if self.config.modem.mock:
            # The simulator has no ALSA/PCM devices; the protocol path is testable.
            return True
        ok, _capture, _playback, error = await audio_devices(self.config.audio_bridge)
        self._audio_error = error
        return ok

    async def _set_pcm(self, enabled: bool) -> bool:
        if enabled:
            if self._pcm_enabled:
                return True
            if self.config.modem.mock and self.modem.channel is None:
                # The simulator has no ALSA route; tests use this branch only
                # when they explicitly enable audio.
                return False
            ok = await self.modem.enable_pcm_audio()
            self._pcm_enabled = ok
            return ok
        if self.modem.channel is None:
            self._pcm_enabled = False
            return False
        ok = await self.modem.disable_pcm_audio()
        self._pcm_enabled = False
        return ok

    async def _start_voice_call(self) -> bool:
        if not self.config.audio_bridge.enabled:
            log.warning("голосовой цикл выключен: audio_bridge.enabled = false")
            return False
        if self.audio.active:
            log.info("останавливаю PCM-мост перед голосовым циклом")
            await self.audio.stop()
        if not self.voice.active:
            if not await self.voice.start():
                await self._set_pcm(False)
                return False
        if not await self._set_pcm(True):
            log.warning("не удалось включить PCM-маршрут для голоса")
            await self._stop_voice_call()
            return False
        if not await self.voice.prime():
            log.warning("PCM-маршрут не подтвердил запись; продолжаю без блокировки")
        return True

    async def _open_call_audio(self, *, with_voice: bool) -> bool:
        """Enable the PCM route for an outgoing call the callee has answered."""
        if not self.config.audio_bridge.enabled:
            log.warning("звонок: audio_bridge.enabled = false, аудио не открывается")
            return False
        if not await self._audio_available():
            log.warning("звонок: аудио недоступно (%s)", self._audio_error)
            return False
        if with_voice and not await self._start_voice_call():
            log.warning("звонок: голосовой цикл не поднялся")
            return False
        if not await self._set_pcm(True):
            log.warning("звонок: не удалось включить PCM-маршрут")
            return False
        if not await self.voice.prime():
            log.warning("PCM-маршрут не подтвердил запись; продолжаю без блокировки")
        return True

    async def _bridge_after_pickup(self, room: str, endpoint_id: Any) -> None:
        """Hand an outgoing call to a room speaker once the person answers."""
        try:
            for _ in range(60):
                await asyncio.sleep(1.0)
                if getattr(self.modem.snapshot, "call_active", False):
                    opened = await self._open_call_audio(with_voice=False)
                    bridged = await self.start_bridge(room, endpoint_id)
                    if not bridged.get("ok"):
                        if opened:
                            await self._set_pcm(False)
                        log.warning(
                            "звонок: не удалось соединить с колонкой: %s", bridged.get("error")
                        )
                    return
            log.warning("звонок: не дождались ответа, соединять нечего")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("звонок: соединение с колонкой упало")

    async def _say_after_pickup(self, text: str) -> None:
        """Wait for the callee to answer an outgoing call, then speak ``text``."""
        try:
            for _ in range(60):
                await asyncio.sleep(1.0)
                snapshot = self.modem.snapshot
                if getattr(snapshot, "call_active", False):
                    if await self._start_voice_call():
                        if await self.voice.say(text):
                            log.info("звонок: сообщение передано голосом")
                        else:
                            log.warning("звонок: не удалось передать сообщение голосом")
                    else:
                        log.warning("звонок: голосовой цикл не поднялся, сообщение не передано")
                    return
            log.warning("звонок: сообщение не передано, линия не поднялась")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("звонок: не удалось передать сообщение голосом")

    async def _stop_voice_call(self) -> None:
        was_active = self.voice.active
        await self.voice.stop()
        if was_active or (self._pcm_enabled and not self.audio.active):
            await self._set_pcm(False)

    async def _answer_with_voice(self) -> tuple[bool, str | None]:
        """Answer only after the audio route is known to exist.

        The PCM route itself must be enabled after ATA: SIM7600 rejects
        ``AT+CPCMREG=1`` while the call is still ringing. If voice setup fails
        after answering, hang up so the caller does not hear silence.
        """
        if not await self._audio_available():
            return False, self._audio_error
        result = await self.modem.answer()
        if not result.ok:
            return False, result.error
        if not await self._start_voice_call():
            error = self.voice.status.get("last_error") or "voice audio setup failed"
            # The call is already answered, so the protocol ack is done; the
            # core must know the line ended rather than stay in a silent call.
            await self.modem.hangup()
            return True, error
        return True, None

    async def _bridge_failed(self, error: str) -> None:
        """A call.bridge request failed after ATA: keep the call alive if possible."""
        self._bridge_pending = False
        self._cancel_bridge_timeout()
        await self.audio.stop()
        if not self.modem.snapshot.call_active:
            return
        log.warning("call.bridge не удался (%s), пробую голосовой fallback", error)
        if await self._start_voice_call():
            return
        log.warning("голосовой fallback недоступен — завершаю звонок, чтобы не оставить тишину")
        with contextlib.suppress(Exception):
            await self.modem.hangup()

    async def _schedule_bridge_timeout(self) -> None:
        self._cancel_bridge_timeout()

        async def wait_bridge() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(BRIDGE_CONFIRM_TIMEOUT)
            if self._bridge_pending and self.modem.snapshot.call_active:
                log.warning("ядро не подтвердило call.bridge за %.0f c", BRIDGE_CONFIRM_TIMEOUT)
                await self._bridge_failed("call.bridge timeout")

        self._bridge_timeout_task = asyncio.create_task(wait_bridge(), name="bridge-confirm")

    def _cancel_bridge_timeout(self) -> None:
        if self._bridge_timeout_task is not None:
            self._bridge_timeout_task.cancel()
            self._bridge_timeout_task = None

    # ── Athena → gateway commands ───────────────────────────────────────────

    async def _on_command(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = str(message.get("kind") or "")
        payload = message.get("payload") or {}
        log.info("команда из ядра: %s", kind)
        await self.bus.publish("command.received", {"kind": kind, "payload": payload})

        if kind in ("call", "sms", "ussd", "reboot") and not self.config.modem.enabled:
            return {"status": "failed", "error": "modem is disabled in the gateway config"}

        if kind == "call":
            action = str(payload.get("action") or "dial")
            number = str(payload.get("number") or "")
            if action == "dial":
                if not number:
                    return {"status": "failed", "error": "number is required"}
                result = await self.modem.dial(number)
                if result.ok:
                    self._calls_log.append(
                        {"direction": "out", "number": number, "at": now_iso(), "state": "dialing"}
                    )
                    say = str(payload.get("say") or "").strip()
                    if say:
                        # «Позвони Руслану и скажи, что…»: the message is spoken
                        # into the line as soon as the person picks up.
                        asyncio.create_task(self._say_after_pickup(say), name="call-say")
                    wants_bridge = (
                        str(payload.get("mode") or "") == "bridge"
                        or bool(payload.get("bridge_room"))
                        or bool(payload.get("bridge_endpoint_id"))
                    )
                    if wants_bridge:
                        # «Позвони маме и соедини с кухней»: once the person
                        # answers, the line is handed to the room speaker.
                        asyncio.create_task(
                            self._bridge_after_pickup(
                                str(payload.get("bridge_room") or ""),
                                payload.get("bridge_endpoint_id"),
                            ),
                            name="call-bridge",
                        )
                    return {"status": "done"}
                return {"status": "failed", "error": result.error or "dial failed"}
            if action == "answer":
                mode = str(payload.get("mode") or "assistant")
                bridge_room = str(payload.get("bridge_room") or "")
                bridge_endpoint = payload.get("bridge_endpoint_id")
                wants_bridge = mode == "bridge" or bool(bridge_room) or bool(bridge_endpoint)
                if wants_bridge:
                    if not await self._audio_available():
                        return {
                            "status": "failed",
                            "error": f"voice audio is unavailable: {self._audio_error}",
                        }
                    result = await self.modem.answer()
                    if not result.ok:
                        return {"status": "failed", "error": result.error}
                    await self._stop_voice_call()
                    bridged = await self.start_bridge(bridge_room, bridge_endpoint)
                    if not bridged.get("ok"):
                        await self._bridge_failed(str(bridged.get("error") or "call.bridge failed"))
                        if self.modem.snapshot.call_active:
                            return {
                                "status": "done",
                                "mode": "assistant",
                                "warning": bridged.get("error"),
                            }
                        return {"status": "failed", "error": bridged.get("error")}
                    return {"status": "done", "mode": "bridge"}
                if not await self._audio_available():
                    return {
                        "status": "failed",
                        "error": f"voice audio is unavailable: {self._audio_error}",
                    }
                ok, error = await self._answer_with_voice()
                if not ok:
                    return {
                        "status": "failed",
                        "error": f"voice audio is unavailable: {error}",
                    }
                self._bridge_pending = False
                self._cancel_bridge_timeout()
                if error:
                    return {"status": "done", "mode": "assistant", "warning": error}
                return {"status": "done"}
            if action == "hangup":
                result = await self.modem.hangup()
                return (
                    {"status": "done"} if result.ok else {"status": "failed", "error": result.error}
                )
            return {"status": "failed", "error": f"unsupported call action: {action}"}

        if kind == "sms":
            number = str(payload.get("number") or "")
            text = str(payload.get("text") or "")
            if not number or not text:
                return {"status": "failed", "error": "number and text are required"}
            if len(text) > 1000:
                return {"status": "failed", "error": "text is longer than 1000 characters"}
            try:
                parts = await self.modem.send_sms(number, text)
            except ModemError as exc:
                return {"status": "failed", "error": str(exc)}
            self._sms_log.append(
                {
                    "direction": "out",
                    "number": number,
                    "text": text,
                    "at": now_iso(),
                    "parts": parts,
                }
            )
            await self.bus.publish("sms.sent", {"number": number, "parts": parts})
            return {"status": "done"}

        if kind == "ussd":
            code = str(payload.get("code") or "")
            result = await self.modem.send_ussd(code)
            if result.get("ok"):
                self._ussd_log.append(
                    {"code": code, "response": result.get("response"), "at": now_iso()}
                )
                return {"status": "done"}
            return {"status": "failed", "error": str(result.get("error") or "ussd failed")}

        if kind == "reboot":
            self.schedule_recover("core reboot command")
            return {"status": "done"}

        if kind == "config":
            log.info("ядро прислало config: %s", payload)
            return {"status": "done"}

        if kind == "intercom":
            return {
                "status": "failed",
                "error": "intercom is not a command; use call.bridge after answering the GSM call",
            }

        return {"status": "failed", "error": f"unsupported command: {kind or 'unknown'}"}

    def schedule_recover(self, reason: str) -> None:
        self._spawn(self.modem.recover(reason))

    async def _reboot_modem(self, reason: str) -> None:
        await asyncio.sleep(1)
        await self.modem.recover(reason)

    # ── intercom frames ──────────────────────────────────────────────────────

    async def _on_protocol_frame(self, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        if kind == "error":
            await self._on_core_error(frame)
            return
        if self.voice.active and not kind.startswith("intercom."):
            try:
                await self.voice.handle_frame(frame)
            except Exception:  # pragma: no cover - a bad frame must not kill the call
                log.exception("кадр %s не обработан голосовым циклом", kind)
        if kind == "intercom.ringing":
            session = frame.get("session_id")
            if isinstance(session, int):
                self._intercom_session = session
            self._bridge_pending = True
            self._cancel_bridge_timeout()
            await self.bus.publish("intercom.ringing", frame)
            return
        if kind == "intercom.active":
            session = frame.get("session_id")
            if isinstance(session, int):
                self._intercom_session = session
            self._bridge_pending = False
            self._cancel_bridge_timeout()
            await self.bus.publish("intercom.active", frame)
            if not await self.audio.start():
                log.warning(
                    "intercom.active, но аудиомост не поднялся: %s",
                    self.audio.status()["last_error"],
                )
                await self._end_intercom("error")
                with contextlib.suppress(Exception):
                    await self.modem.hangup()
            return
        if kind in ("intercom.ended", "intercom.end"):
            await self.bus.publish(kind, frame)
            await self.audio.stop()
            if kind == "intercom.ended":
                self._intercom_session = None
                self._bridge_pending = False
                self._cancel_bridge_timeout()
            return
        if kind == "intercom.ack":
            await self.bus.publish(kind, frame)
            if frame.get("status") == "active":
                self._bridge_pending = False
                self._cancel_bridge_timeout()
                if not await self.audio.start():
                    await self._end_intercom("error")
            return
        if kind == "event.ack" and frame.get("event") == "call.bridge":
            self._schedule_bridge_timeout()
        await self.bus.publish(kind or "intercom.frame", frame)

    async def _on_core_error(self, frame: dict[str, Any]) -> None:
        code = str(frame.get("code") or "")
        if code.startswith("bridge_") or self._bridge_pending:
            log.warning("ядро отклонило мост: %s — %s", code, frame.get("message"))
            await self.bus.publish("bridge.error", frame)
            await self._bridge_failed(code or "core error")
            return
        await self.bus.publish("core.error", frame)

    async def _on_voice_stopped(self, reason: str) -> None:
        if self._stopping:
            return
        log.warning("голосовой цикл звонка остановился: %s", reason)
        await self._stop_voice_call()
        if self.modem.snapshot.call_active and not self._bridge_pending:
            with contextlib.suppress(Exception):
                await self.modem.hangup()

    async def _on_audio_stopped(self, reason: str) -> None:
        if self._stopping:
            return
        log.warning("аудиомост остановился: %s", reason)
        self._bridge_pending = False
        self._cancel_bridge_timeout()
        if self._intercom_session is not None:
            session, self._intercom_session = self._intercom_session, None
            await self.athena.send_frame(
                {"type": "intercom.end", "session_id": session, "reason": "error"}
            )
        await self.audio.stop()
        if self.modem.snapshot.call_active:
            with contextlib.suppress(Exception):
                await self.modem.hangup()

    async def _end_intercom(self, reason: str) -> None:
        await self.audio.stop()
        if self._intercom_session is None:
            return
        session, self._intercom_session = self._intercom_session, None
        await self.athena.send_frame(
            {"type": "intercom.end", "session_id": session, "reason": reason}
        )

    async def _on_audio_capture(self, pcm: bytes) -> None:
        await self.athena.send_binary(pcm)

    async def _on_core_audio(self, pcm: bytes) -> None:
        await self.audio.feed(pcm)

    # ── local API helpers ────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "gateway": {
                "name": self.config.bridge.name,
                "version": __version__,
                "uptime_sec": int(time.time() - self.started_at),
                "mock": self.config.modem.mock,
                "time": now_iso(),
                "config_source": self.config.source,
            },
            "modem": self.modem.snapshot.to_dict(),
            "athena": self.athena.status(),
            "audio": self.audio.status(),
            "voice_call": self.voice.status,
            "pcm_enabled": self._pcm_enabled,
            "stats": {
                "sms_in_log": len(self._sms_log),
                "calls_in_log": len(self._calls_log),
                "intercom_session": self._intercom_session,
            },
        }

    def recent_sms(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._sms_log)[-limit:][::-1]

    def recent_calls(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._calls_log)[-limit:][::-1]

    def recent_ussd(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._ussd_log)[-limit:][::-1]

    def diagnostics(self) -> dict[str, Any]:
        return {
            "gateway": self.status()["gateway"],
            "at_log": self.modem.at_log[-50:],
            "athena": self.athena.status(),
            "config": {
                "modem": {
                    "port": self.config.modem.port,
                    "mock": self.config.modem.mock,
                    "unknown_callers": self.config.modem.unknown_callers,
                    "bridge_room": self.config.modem.bridge_room,
                    "auto_answer": self.config.modem.auto_answer,
                },
                "athena": {
                    "url": self.config.athena.url,
                    "enabled": self.config.athena.enabled,
                },
            },
        }

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background_done)

    def _background_done(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("фоновая задача завершилась ошибкой: %s", exc)

    # ── direct API actions (local UI / Home Assistant) ──────────────────────

    async def send_sms(self, number: str, text: str) -> int:
        parts = await self.modem.send_sms(number, text)
        self._sms_log.append(
            {"direction": "out", "number": number, "text": text, "at": now_iso(), "parts": parts}
        )
        await self.bus.publish("sms.sent", {"number": number, "parts": parts})
        return parts

    async def send_ussd(self, code: str) -> dict[str, Any]:
        result = await self.modem.send_ussd(code)
        if result.get("ok"):
            self._ussd_log.append(
                {"code": code, "response": result.get("response"), "at": now_iso()}
            )
        return result

    async def dial(self, number: str):
        result = await self.modem.dial(number)
        if result.ok:
            self._calls_log.append(
                {"direction": "out", "number": number, "at": now_iso(), "state": "dialing"}
            )
            await self.bus.publish("call.dialing", {"number": number})
        return result

    async def answer(self):
        return await self.modem.answer()

    async def hangup(self):
        return await self.modem.hangup()

    async def send_dtmf(self, digits: str):
        return await self.modem.send_dtmf(digits)

    async def start_bridge(
        self, room: str | None = None, endpoint_id: str | None = None
    ) -> dict[str, Any]:
        """Answer the current GSM call (if ringing) and ask the core to bridge it."""
        if not await self._audio_available():
            return {"ok": False, "error": self._audio_error or "voice audio is unavailable"}
        if self.modem.snapshot.ringing:
            result = await self.modem.answer()
            if not result.ok:
                return {"ok": False, "error": result.error or "answer failed"}
        target: dict[str, Any] = {}
        target_room = room or self.config.modem.bridge_room
        if endpoint_id:
            target["target_endpoint_id"] = endpoint_id
        elif target_room:
            target["room"] = target_room
        else:
            return {"ok": False, "error": "room or endpoint_id is required"}
        target["call_number"] = self.modem.snapshot.caller or ""
        sent = await self.athena.send_bridge(target)
        self._bridge_pending = bool(sent)
        if sent:
            await self._schedule_bridge_timeout()
        else:
            self._cancel_bridge_timeout()
        return {"ok": sent, "room": target.get("room"), "target_endpoint_id": endpoint_id}
