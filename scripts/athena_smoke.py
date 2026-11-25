"""End-to-end smoke test of the gateway in simulator mode.

Run from the repository root:

    python sim7600-gateway/scripts/athena_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("httpx is required: pip install httpx")
        return 2

    from sim7600_gateway.app import create_app
    from sim7600_gateway.config import Config

    config = Config()
    config.bridge.api_token = "smoke-token"
    config.bridge.state_dir = tempfile.mkdtemp(prefix="sim7600-smoke-")
    config.athena.enabled = False
    config.modem.mock = True
    config.camera.enabled = False
    config.camera.mock = True

    headers = {"Authorization": "Bearer smoke-token"}
    checks: list[tuple[str, bool]] = []

    with TestClient(create_app(config)) as client:
        checks.append(("health", client.get("/api/health").status_code == 200))
        checks.append(("auth required", client.get("/api/status").status_code == 401))

        status = None
        deadline = time.time() + 5
        while time.time() < deadline:
            response = client.get("/api/status", headers=headers)
            if response.status_code == 200 and response.json()["modem"]["state"] not in ("off", "init"):
                status = response.json()
                break
            time.sleep(0.1)
        checks.append(("modem ready", status is not None))
        if status:
            print(f"  modem: {status['modem']['model']} / {status['modem']['operator']} / "
                  f"{status['modem']['registration_text']}")
            checks.append(("registered", bool(status["modem"]["registered"])))

        sms = client.post(
            "/api/sms",
            headers=headers,
            json={"number": "+79991234567", "text": "Smoke test: privet"},
        )
        checks.append(("sms send", sms.status_code == 200 and sms.json().get("parts") == 1))

        ussd = client.post("/api/ussd", headers=headers, json={"code": "*100#"})
        checks.append(("ussd", ussd.status_code == 200 and bool(ussd.json().get("response"))))

        call = client.post("/api/dev/call", headers=headers, json={"number": "+79995550011"})
        checks.append(("inject call", call.status_code == 200))
        time.sleep(0.9)
        events = client.get("/api/events/recent", headers=headers).json()
        checks.append(("call event", any(item["type"] == "call.incoming" for item in events)))

        answer = client.post("/api/calls/answer", headers=headers)
        checks.append(("answer", answer.status_code == 200))
        hangup = client.post("/api/calls/hangup", headers=headers)
        checks.append(("hangup", hangup.status_code == 200))

        call_json = client.get("/api/status", headers=headers).json()
        checks.append(("call ended", not call_json["modem"]["call_active"]))

        bad_at = client.post("/api/modem/at", headers=headers, json={"command": "AT+CFUN=0"})
        checks.append(("at allowlist", bad_at.status_code == 403))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"[{'OK' if ok else 'FAIL'}] {name}")
    if failed:
        print(f"\n{len(failed)} checks failed")
        return 1
    print(f"\nAll {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
