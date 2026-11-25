"""FastAPI application: local REST API, SSE events and the dashboard."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Config
from .dashboard import DASHBOARD_HTML
from .log import get_logger
from .modem.errors import ModemError

log = get_logger("api")



class SmsRequest(BaseModel):
    number: str = Field(min_length=3, max_length=32)
    text: str = Field(min_length=1, max_length=1000)


class UssdRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=r"^[0-9*#+]+$")


class DialRequest(BaseModel):
    number: str = Field(min_length=3, max_length=32)


class DtmfRequest(BaseModel):
    digits: str = Field(min_length=1, max_length=32, pattern=r"^[0-9*#ABCD]+$")



class AtRequest(BaseModel):
    command: str = Field(min_length=2, max_length=64)


class BridgeRequest(BaseModel):
    room: str | None = Field(default=None, max_length=64)
    endpoint_id: str | None = Field(default=None, max_length=64)


def create_app(config: Config) -> FastAPI:
    from .bridge import GatewayBridge

    bridge = GatewayBridge(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await bridge.start()
        try:
            yield
        finally:
            await bridge.stop()

    app = FastAPI(
        title="Athena SIM7600 Gateway",
        version=__version__,
        description="GSM client for Athena Core and Home Assistant",
        lifespan=lifespan,
    )
    app.state.bridge = bridge
    app.state.config = config

    @app.exception_handler(ModemError)
    async def modem_error_handler(request: Request, exc: ModemError) -> Response:
        return Response(
            content=json.dumps({"detail": str(exc)}, ensure_ascii=False),
            status_code=503,
            media_type="application/json",
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _supplied_token(request: Request, token: str | None) -> str | None:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        api_token = request.headers.get("x-api-token")
        if api_token:
            return api_token
        # Query tokens leak into access logs/history: allow them only for
        # streaming endpoints (<img>, EventSource) that cannot set headers.
        path = request.url.path
        if path.startswith("/api/events"):
            return token
        return None

    async def auth(request: Request, token: str | None = Query(default=None)) -> None:
        expected = config.bridge.api_token
        if not expected:
            return
        supplied = _supplied_token(request, token)
        if supplied != expected:
            raise HTTPException(status_code=401, detail="invalid API token")


    # ── pages and health ─────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> str:
        return DASHBOARD_HTML

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "uptime_sec": int(time.time() - bridge.started_at),
            "mock": config.modem.mock,
        }

    @app.get("/api/status", dependencies=[Depends(auth)])
    async def status() -> dict:
        return bridge.status()

    @app.get("/api/diagnostics", dependencies=[Depends(auth)])
    async def diagnostics() -> dict:
        return bridge.diagnostics()

    # ── SMS / USSD / calls ───────────────────────────────────────────────────

    @app.post("/api/sms", dependencies=[Depends(auth)])
    async def send_sms(body: SmsRequest) -> dict:
        try:
            parts = await bridge.send_sms(body.number, body.text)
        except ModemError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "parts": parts}

    @app.get("/api/sms/recent", dependencies=[Depends(auth)])
    async def sms_recent(limit: int = Query(default=50, ge=1, le=100)) -> list[dict]:
        return bridge.recent_sms(limit)

    @app.post("/api/ussd", dependencies=[Depends(auth)])
    async def ussd(body: UssdRequest) -> dict:
        result = await bridge.send_ussd(body.code)
        if not result.get("ok"):
            raise HTTPException(status_code=503, detail=result.get("error") or "USSD failed")
        return result

    @app.get("/api/ussd/recent", dependencies=[Depends(auth)])
    async def ussd_recent(limit: int = Query(default=20, ge=1, le=50)) -> list[dict]:
        return bridge.recent_ussd(limit)

    @app.post("/api/dial", dependencies=[Depends(auth)])
    async def dial(body: DialRequest) -> dict:
        result = await bridge.dial(body.number)
        if not result.ok:
            raise HTTPException(status_code=503, detail=result.error or "dial failed")
        return {"ok": True, "number": body.number}

    @app.post("/api/calls/answer", dependencies=[Depends(auth)])
    async def answer() -> dict:
        result = await bridge.answer()
        if not result.ok:
            raise HTTPException(status_code=503, detail=result.error or "answer failed")
        return {"ok": True}

    @app.post("/api/calls/hangup", dependencies=[Depends(auth)])
    async def hangup() -> dict:
        result = await bridge.hangup()
        if not result.ok:
            raise HTTPException(status_code=503, detail=result.error or "hangup failed")
        return {"ok": True}

    @app.post("/api/calls/dtmf", dependencies=[Depends(auth)])
    async def dtmf(body: DtmfRequest) -> dict:
        result = await bridge.send_dtmf(body.digits)
        if result is not None and not result.ok:
            raise HTTPException(status_code=503, detail=result.error or "DTMF failed")
        return {"ok": True}

    @app.get("/api/calls/recent", dependencies=[Depends(auth)])
    async def calls_recent(limit: int = Query(default=50, ge=1, le=100)) -> list[dict]:
        return bridge.recent_calls(limit)

    @app.post("/api/calls/bridge", dependencies=[Depends(auth)])
    async def bridge_call(body: BridgeRequest) -> dict:
        result = await bridge.start_bridge(body.room, body.endpoint_id)
        if not result.get("ok"):
            raise HTTPException(status_code=503, detail=result.get("error") or "bridge failed")
        return result

    @app.post("/api/modem/reset", dependencies=[Depends(auth)])
    async def reset_modem() -> dict:
        bridge.schedule_recover("local API request")
        return {"ok": True, "scheduled": True}

    @app.post("/api/modem/at", dependencies=[Depends(auth)])
    async def modem_at(body: AtRequest) -> dict:
        allowed = (
            "AT",
            "ATI",
            "AT+CSQ",
            "AT+CREG?",
            "AT+CGREG?",
            "AT+COPS?",
            "AT+CPIN?",
            "AT+CGSN",
            "AT+CCID",
            "AT+CGMR",
            "AT+GMM",
            "AT+CPSI?",
            "AT+CLCC",
        )
        command = body.command.strip()
        if command.upper() not in [item.upper() for item in allowed]:
            raise HTTPException(
                status_code=403, detail="command is not in the diagnostic allowlist"
            )
        channel = bridge.modem.channel
        if channel is None:
            raise HTTPException(status_code=503, detail="modem is not connected")
        result = await channel.command(command, timeout=10)
        return {
            "ok": result.ok,
            "final": result.final,
            "lines": result.lines[:20],
        }

    # ── events ───────────────────────────────────────────────────────────────

    @app.get("/api/events")
    async def events(
        request: Request, token: str | None = Query(default=None)
    ) -> StreamingResponse:
        await auth(request, token)
        queue = bridge.bus.open_subscription()

        async def generate() -> AsyncIterator[bytes]:
            try:
                yield b"retry: 3000\n\n"
                while True:
                    try:
                        record = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    payload = json.dumps(record.to_dict(), ensure_ascii=False)
                    yield f"data: {payload}\n\n".encode()
            finally:
                bridge.bus.close_subscription(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/events/recent", dependencies=[Depends(auth)])
    async def events_recent(
        limit: int = Query(default=50, ge=1, le=200),
        type: str | None = Query(default=None),
    ) -> list[dict]:
        return bridge.bus.recent(limit, type)

    # ── development simulator (only in mock mode) ────────────────────────────

    if config.modem.mock:

        @app.post("/api/dev/call", dependencies=[Depends(auth)])
        async def dev_call(body: DialRequest) -> dict:
            if bridge.simulator is None:
                raise HTTPException(status_code=503, detail="simulator is not available")
            bridge.simulator.inject_call(body.number)
            return {"ok": True, "injected": "call"}

        @app.post("/api/dev/sms", dependencies=[Depends(auth)])
        async def dev_sms(body: SmsRequest) -> dict:
            if bridge.simulator is None:
                raise HTTPException(status_code=503, detail="simulator is not available")
            bridge.simulator.inject_sms(body.number, body.text)
            return {"ok": True, "injected": "sms"}

        @app.post("/api/dev/dtmf", dependencies=[Depends(auth)])
        async def dev_dtmf(body: DtmfRequest) -> dict:
            if bridge.simulator is None:
                raise HTTPException(status_code=503, detail="simulator is not available")
            bridge.simulator.inject_dtmf(body.digits[0])
            return {"ok": True, "injected": "dtmf"}

        @app.post("/api/dev/hangup", dependencies=[Depends(auth)])
        async def dev_hangup() -> dict:
            if bridge.simulator is None:
                raise HTTPException(status_code=503, detail="simulator is not available")
            bridge.simulator.remote_hangup()
            return {"ok": True, "injected": "hangup"}

    return app
