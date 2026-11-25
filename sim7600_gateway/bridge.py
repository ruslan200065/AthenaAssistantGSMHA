"""Gateway orchestration: modem + camera + Athena core + local event bus."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from typing import Any

from . import __version__, athena
from .athena import frames
from .audio_bridge import AudioBridge
from .bus import EventBus
from .camera.service import CameraService
from .config import Config
from .log import get_logger
from .modem.errors import ModemError
from .modem.mock import SimulatedModem
from .modem.sim7600 import Sim7600
from .util import mask_numbers, now_iso
from .voice_call import CallVoiceSession

log = get_logger("bridge")


class GatewayBridge:
    def __init__(self, config: Config):
        self.config = config
        self.bus = EventBus()
        self.started_at = time.time()
        self._stopping = False
        self._modem_task: asyncio.Task | None = None
        self.simulator: SimulatedModem | None = None

        self.camera = CameraService(config.camera, on_event=self._on_camera_event)

        transport = None
        if config.modem.mock:
            transport = SimulatedModem()
            self.simulator = transport
        self.modem = Sim7600(config.modem, transport=transport, on_event=self._on_modem_event)

        self.audio = AudioBridge(
            config.audio_bridge,
            self.modem,
            on_capture=self._on_audio_capture,
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
        )

        self._sms_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._calls_log: deque[dict[str, Any]] = deque(maxlen=100)
        self._ussd_log: deque[dict[str, Any]] = deque(maxlen=50)
        self._intercom_session: int | None = None
        self._bridge_pending = False
        self._background: set[asyncio.Task] = set()

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        log.info("запуск шлюза «%s» v%s", self.config.bridge.name, __version__)
        await self.bus.publish(
            "gateway.start",
            {"name": self.config.bridge.name, "version": __version__, "mock": self.config.modem.mock},
        )
        await self.camera.start()
        self._modem_task = asyncio.create_task(self._modem_supervisor(), name="modem-supervisor")
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
        await self.audio.stop()
        await self._stop_voice_call()
        await self.modem.stop()
        await self.camera.stop()
        log.info("шлюз остановлен")

    async def _modem_supervisor(self) -> None:
        while not self._stopping:
            try:
                await self.modem.start()
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
            self._calls_log.append({"direction": "in", "number": data.get("number"), "at": now_iso(), "state": "ringing"})
            log.info("входящий звонок от %s", mask_numbers(data.get("number")))
            self._spawn(self.camera.snapshot_on_ring())
            if self.config.camera.clip_on_ring:
                self._spawn(self._record_ring_clip())
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
                    )
                )
            if state in ("ended", "missed", "failed"):
                self._bridge_pending = False
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
            log.info("SMS от %s (%d символов)", mask_numbers(data.get("number")), len(data.get("text") or ""))
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
                self._ussd_log.append({"code": payload.get("code"), "response": payload.get("response"), "at": now_iso()})
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
            await asyncio.sleep(0.4)
            result = await self.modem.answer()
            if not result.ok:
                log.warning("не удалось ответить на звонок: %s", result.error)
                return
            bridged = await self.start_bridge(modem_config.bridge_room, None)
            if bridged.get("ok"):
                log.info("звонок %s соединён с комнатой «%s»", mask_numbers(number), modem_config.bridge_room)
            else:
                log.warning("call.bridge не отправлен: %s", bridged.get("error"))
            return
        if known and modem_config.auto_answer:
            await asyncio.sleep(max(0.0, modem_config.auto_answer_delay))
            result = await self.modem.answer()
            if result.ok:
                await self._start_voice_call()
            else:
                log.warning("автоответ не удался: %s", result.error)

    async def _start_voice_call(self) -> None:
        channel = self.modem.channel
        if channel is not None:
            with contextlib.suppress(Exception):
                await channel.command("AT+CPCMREG=1", timeout=5)
        await self.voice.start()

    async def _stop_voice_call(self) -> None:
        was_active = self.voice.active
        await self.voice.stop()
        channel = self.modem.channel
        if was_active and channel is not None:
            with contextlib.suppress(Exception):
                await channel.command("AT+CPCMREG=0", timeout=5)

    async def _record_ring_clip(self) -> None:
        try:
            record = await self.camera.record()
            if record:
                log.info("записан клип звонка: %s", record.get("filename"))
        except Exception as exc:
            log.warning("не удалось записать клип: %s", exc)

    # ── camera events ────────────────────────────────────────────────────────

    async def _on_camera_event(self, event: str, data: dict[str, Any]) -> None:
        await self.bus.publish(event, data)
        if self.athena.connected and event in ("camera.motion", "camera.snapshot"):
            await self.athena.send_event(frames.device_event(event, data))

    # ── Athena → gateway commands ───────────────────────────────────────────

    async def _on_command(self, message: dict[str, Any]) -> dict[str, Any]:
        kind = str(message.get("kind") or "")
        payload = message.get("payload") or {}
        log.info("команда из ядра: %s", kind)
        await self.bus.publish("command.received", {"kind": kind, "payload": payload})

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
                    return {"status": "done"}
                return {"status": "failed", "error": result.error or "dial failed"}
            if action == "answer":
                mode = str(payload.get("mode") or "assistant")
                bridge_room = str(payload.get("bridge_room") or "")
                bridge_endpoint = payload.get("bridge_endpoint_id")
                result = await self.modem.answer()
                if not result.ok:
                    return {"status": "failed", "error": result.error}
                if mode == "bridge" or bridge_room or bridge_endpoint:
                    await self._stop_voice_call()
                    bridged = await self.start_bridge(bridge_room, bridge_endpoint)
                    if not bridged.get("ok"):
                        log.warning("call.bridge не отправлен: %s", bridged.get("error"))
                elif self._bridge_pending or self.audio.active:
                    log.info("звонок уже соединён с колонкой, голосовой цикл не запускаю")
                else:
                    await self._start_voice_call()
                return {"status": "done"}
            if action == "hangup":
                result = await self.modem.hangup()
                return {"status": "done"} if result.ok else {"status": "failed", "error": result.error}
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
                {"direction": "out", "number": number, "text": text, "at": now_iso(), "parts": parts}
            )
            await self.bus.publish("sms.sent", {"number": number, "parts": parts})
            return {"status": "done"}

        if kind == "ussd":
            code = str(payload.get("code") or "")
            result = await self.modem.send_ussd(code)
            if result.get("ok"):
                self._ussd_log.append({"code": code, "response": result.get("response"), "at": now_iso()})
                return {"status": "done"}
            return {"status": "failed", "error": str(result.get("error") or "ussd failed")}

        if kind == "reboot":
            self._spawn(self._reboot_modem("core reboot command"))
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

    async def _reboot_modem(self, reason: str) -> None:
        await asyncio.sleep(1)
        await self.modem.recover(reason)

    # ── intercom frames ──────────────────────────────────────────────────────

    async def _on_protocol_frame(self, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        if self.voice.active and not kind.startswith("intercom."):
            await self.voice.handle_frame(frame)
        if kind == "intercom.ringing":
            session = frame.get("session_id")
            if isinstance(session, int):
                self._intercom_session = session
            self._bridge_pending = True
            await self.bus.publish("intercom.ringing", frame)
            return
        if kind == "intercom.active":
            session = frame.get("session_id")
            if isinstance(session, int):
                self._intercom_session = session
            await self.bus.publish("intercom.active", frame)
            await self.audio.start()
            return
        if kind in ("intercom.ended", "intercom.end"):
            await self.bus.publish(kind, frame)
            await self.audio.stop()
            if kind == "intercom.ended":
                self._intercom_session = None
                self._bridge_pending = False
            return
        if kind == "intercom.ack":
            await self.bus.publish(kind, frame)
            if frame.get("status") == "active":
                await self.audio.start()
            return
        await self.bus.publish(kind or "intercom.frame", frame)

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
            "camera": self.camera.info(),
            "athena": self.athena.status(),
            "audio": self.audio.status(),
            "voice_call": self.voice.status,
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
            "camera": self.camera.info(),
            "athena": self.athena.status(),
            "config": {
                "modem": {
                    "port": self.config.modem.port,
                    "mock": self.config.modem.mock,
                    "unknown_callers": self.config.modem.unknown_callers,
                    "bridge_room": self.config.modem.bridge_room,
                    "auto_answer": self.config.modem.auto_answer,
                },
                "camera": {
                    "device": self.config.camera.device,
                    "mock": self.config.camera.mock,
                    "width": self.config.camera.width,
                    "height": self.config.camera.height,
                    "fps": self.config.camera.fps,
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
        task.add_done_callback(self._background.discard)

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

    async def start_bridge(self, room: str | None = None, endpoint_id: str | None = None) -> dict[str, Any]:
        """Answer the current GSM call (if ringing) and ask the core to bridge it."""
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
        return {"ok": sent, "room": target.get("room"), "target_endpoint_id": endpoint_id}
