"""Athena client against a local WebSocket core stub."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.athena.client import AthenaClient  # noqa: E402
from sim7600_gateway.athena.frames import sms_incoming  # noqa: E402
from sim7600_gateway.config import AthenaConfig  # noqa: E402

WELCOME = {
    "type": "welcome",
    "home": {"id": 1, "name": "Дом", "timezone": "Europe/Moscow"},
    "device": {"id": 2, "name": "GSM", "room_id": None, "room_name": None},
    "wake_words": ["афина"],
    "audio": {"input": "pcm_s16le/16000/1", "output": "pcm_s16le/24000/1", "chunk_ms": 20},
    "person_default_id": None,
    "features": {"intercom": True, "music": True, "sounds": True, "display": False},
    "server_time": "2026-11-25T10:00:00Z",
    "pending_commands": [],
}


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.02)


def test_session_commands_events_and_dedup(tmp_path) -> None:
    async def scenario() -> None:
        import websockets

        received: list[dict] = []
        hello_seen: asyncio.Future = asyncio.get_running_loop().create_future()
        command_acks: list[dict] = []
        event_seen = asyncio.Event()
        executed: list[dict] = []

        async def handler(websocket) -> None:
            raw = await websocket.recv()
            hello_seen.set_result(json.loads(raw))
            await websocket.send(json.dumps(WELCOME))
            await websocket.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": 5,
                        "kind": "sms",
                        "payload": {"number": "+79991234567", "text": "Привет"},
                        "created_at": "2026-11-25T10:00:01Z",
                    }
                )
            )
            # Repeat the same id: it must not be executed twice.
            await websocket.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": 5,
                        "kind": "sms",
                        "payload": {"number": "+79991234567", "text": "Привет"},
                        "created_at": "2026-11-25T10:00:02Z",
                    }
                )
            )
            try:
                while True:
                    raw = await websocket.recv()
                    if isinstance(raw, bytes):
                        received.append({"binary": len(raw)})
                        continue
                    message = json.loads(raw)
                    received.append(message)
                    if message.get("type") == "command.ack":
                        command_acks.append(message)
                    if message.get("type") == "sms.incoming":
                        event_seen.set()
            except Exception:
                return

        async def on_command(message: dict) -> dict:
            executed.append(message)
            return {"status": "done"}

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = AthenaClient(
            AthenaConfig(
                enabled=True,
                url=f"ws://127.0.0.1:{port}/api/device/ws",
                token="dev_test",
                device_id="test",
            ),
            state_dir=tmp_path,
            on_command=on_command,
            status_provider=lambda: {"registered": True, "signal_dbm": -65},
        )
        try:
            await client.start()
            hello = await asyncio.wait_for(hello_seen, 3)
            assert hello["type"] == "hello"
            assert hello["token"] == "dev_test"
            assert hello["kind"] == "gsm"
            await _wait_until(lambda: client.connected)
            await _wait_until(lambda: len(command_acks) >= 2)
            assert len(executed) == 1
            assert command_acks[1]["status"] == "done"

            assert await client.send_event(sms_incoming("+79995554433", "test"))
            await asyncio.wait_for(event_seen.wait(), 3)
            assert any(item.get("type") == "sms.incoming" for item in received)
            heartbeat = [item for item in received if item.get("type") == "heartbeat"]
            assert heartbeat and heartbeat[0]["status"] == "online"
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_auth_error_stops_client(tmp_path) -> None:
    async def scenario() -> None:
        import websockets

        async def handler(websocket) -> None:
            await websocket.recv()
            await websocket.send(
                json.dumps({"type": "error", "code": "auth", "message": "bad token"})
            )
            await websocket.close(code=1008)

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = AthenaClient(
            AthenaConfig(enabled=True, url=f"ws://127.0.0.1:{port}", token="dev_bad"),
            state_dir=tmp_path,
            on_command=lambda message: None,
        )
        try:
            await client.start()
            await _wait_until(lambda: client.status()["fatal"] is not None, timeout=3)
            assert "bad token" in client.status()["fatal"]
            assert client.status()["connected"] is False
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
