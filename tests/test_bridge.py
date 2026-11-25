"""Bridge orchestration against a fake Athena core."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.bridge import GatewayBridge  # noqa: E402
from sim7600_gateway.config import Config  # noqa: E402

WELCOME = {
    "type": "welcome",
    "home": {"id": 1, "name": "Дом", "timezone": "Europe/Moscow"},
    "device": {"id": 2, "name": "GSM", "room_id": None, "room_name": "Прихожая"},
    "wake_words": ["афина"],
    "audio": {"input": "pcm_s16le/16000/1", "output": "pcm_s16le/24000/1", "chunk_ms": 20},
    "features": {"intercom": True, "music": True, "sounds": True, "display": False},
    "server_time": "2026-11-25T10:00:00Z",
    "pending_commands": [],
}


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.02)


def test_bridge_command_and_call_bridge(tmp_path) -> None:
    async def scenario() -> None:
        import websockets

        received: list[dict] = []
        ws_future: asyncio.Future = asyncio.get_running_loop().create_future()

        async def handler(websocket) -> None:
            await websocket.recv()  # hello
            ws_future.set_result(websocket)
            await websocket.send(json.dumps(WELCOME))
            try:
                while True:
                    raw = await websocket.recv()
                    if isinstance(raw, str):
                        received.append(json.loads(raw))
            except Exception:
                return

        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        config = Config()
        config.bridge.state_dir = str(tmp_path)
        config.bridge.api_token = "test"
        config.athena.enabled = True
        config.athena.url = f"ws://127.0.0.1:{port}/api/device/ws"
        config.athena.token = "dev_test"
        config.modem.mock = True
        config.modem.bridge_room = "Кухня"
        config.modem.allowed_numbers = ["+79991234567"]
        config.camera.enabled = False
        config.camera.mock = True

        bridge = GatewayBridge(config)
        try:
            await bridge.start()
            ws = await asyncio.wait_for(ws_future, 3)
            await _wait_until(lambda: bridge.athena.connected)
            await _wait_until(lambda: bridge.modem.snapshot.registered)

            await ws.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": 10,
                        "kind": "sms",
                        "payload": {"number": "+79991234567", "text": "Привет"},
                        "created_at": "2026-11-25T10:00:00Z",
                    }
                )
            )
            await _wait_until(
                lambda: any(
                    item.get("type") == "command.ack" and item.get("id") == 10
                    for item in received
                )
            )
            ack = next(item for item in received if item.get("type") == "command.ack")
            assert ack["status"] == "done"

            bridge.simulator.inject_call("+79991234567")
            await _wait_until(
                lambda: any(item.get("type") == "call.incoming" for item in received)
            )
            await _wait_until(
                lambda: any(item.get("type") == "call.bridge" for item in received)
            )
            bridge_frame = next(item for item in received if item.get("type") == "call.bridge")
            assert bridge_frame["room"] == "Кухня"
            assert bridge.modem.snapshot.call_active

            await ws.send(json.dumps({"type": "intercom.ringing", "session_id": 77}))
            await asyncio.sleep(0.1)
            bridge.simulator.remote_hangup()
            await _wait_until(
                lambda: any(item.get("type") == "intercom.end" for item in received)
            )
            end = next(item for item in received if item.get("type") == "intercom.end")
            assert end["session_id"] == 77
        finally:
            await bridge.stop()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
