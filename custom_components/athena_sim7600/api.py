"""Minimal aiohttp client for the local gateway REST API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_TIMEOUT


class GatewayError(Exception):
    """Gateway request failed."""


class GatewayAuthError(GatewayError):
    """Gateway rejected the API token."""


class GatewayApi:
    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        token: str,
        verify_ssl: bool = True,
    ):
        self._session = async_get_clientsession(hass, verify_ssl=verify_ssl)
        self.base_url = url.rstrip("/")
        self.token = token

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @property
    def stream_url(self) -> str:
        return f"{self.base_url}/api/camera/stream.mjpg?token={quote(self.token)}"

    @property
    def snapshot_url(self) -> str:
        return f"{self.base_url}/api/camera/snapshot.jpg?token={quote(self.token)}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)
        try:
            async with self._session.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers,
                timeout=timeout,
                **kwargs,
            ) as response:
                if response.status in (401, 403):
                    raise GatewayAuthError("invalid gateway API token")
                if response.status >= 400:
                    text = await response.text()
                    raise GatewayError(f"HTTP {response.status}: {text[:300]}")
                if response.content_type == "application/json":
                    return await response.json()
                return await response.read()
        except aiohttp.ClientError as exc:
            raise GatewayError(str(exc)) from exc
        except TimeoutError as exc:
            raise GatewayError("gateway request timed out") from exc

    # ── state ────────────────────────────────────────────────────────────────

    async def status(self) -> dict[str, Any]:
        result = await self._request("GET", "/api/status")
        if not isinstance(result, dict):
            raise GatewayError("unexpected status payload")
        return result

    async def camera_info(self) -> dict[str, Any]:
        return await self._request("GET", "/api/camera/info")

    async def recent_sms(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._request("GET", f"/api/sms/recent?limit={limit}")

    async def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._request("GET", f"/api/events/recent?limit={limit}")

    # ── actions ──────────────────────────────────────────────────────────────

    async def send_sms(self, number: str, text: str) -> dict[str, Any]:
        return await self._request("POST", "/api/sms", json={"number": number, "text": text})

    async def send_ussd(self, code: str) -> dict[str, Any]:
        return await self._request("POST", "/api/ussd", json={"code": code})

    async def dial(self, number: str) -> dict[str, Any]:
        return await self._request("POST", "/api/dial", json={"number": number})

    async def answer(self) -> dict[str, Any]:
        return await self._request("POST", "/api/calls/answer")

    async def hangup(self) -> dict[str, Any]:
        return await self._request("POST", "/api/calls/hangup")

    async def send_dtmf(self, digits: str) -> dict[str, Any]:
        return await self._request("POST", "/api/calls/dtmf", json={"digits": digits})

    async def reset_modem(self) -> dict[str, Any]:
        return await self._request("POST", "/api/modem/reset")

    async def bridge_call(
        self, room: str | None = None, endpoint_id: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/calls/bridge",
            json={"room": room, "endpoint_id": endpoint_id},
        )

    async def snapshot(self) -> bytes:
        data = await self._request("GET", "/api/camera/snapshot.jpg")
        return data if isinstance(data, bytes) else b""

    async def save_snapshot(self) -> dict[str, Any]:
        data = await self._request("GET", "/api/camera/snapshot.jpg?save=true")
        return {"size": len(data) if isinstance(data, bytes) else 0}

    async def record(self, seconds: float = 12) -> dict[str, Any]:
        return await self._request("POST", "/api/camera/record", json={"seconds": seconds})

    # ── server-sent events ───────────────────────────────────────────────────

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
        while True:
            try:
                async with self._session.get(
                    f"{self.base_url}/api/events",
                    headers=self._headers,
                    timeout=timeout,
                ) as response:
                    if response.status in (401, 403):
                        raise GatewayAuthError("invalid gateway API token")
                    if response.status >= 400:
                        raise GatewayError(f"events HTTP {response.status}")
                    async for raw in response.content:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        try:
                            event = json.loads(payload)
                        except ValueError:
                            continue
                        if isinstance(event, dict):
                            yield event
                # Normal close (gateway restart): do not reconnect in a tight loop.
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except GatewayAuthError:
                raise
            except (aiohttp.ClientError, TimeoutError, GatewayError):
                await asyncio.sleep(5)
