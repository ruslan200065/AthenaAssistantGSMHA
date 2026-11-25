#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/snapshots

python3 - <<'PY'
import json
from pathlib import Path

options_path = Path("/data/options.json")
options = json.loads(options_path.read_text("utf-8-sig")) if options_path.exists() else {}

def q(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)

def flag(value) -> str:
    return "true" if value else "false"

numbers = [
    item.strip()
    for item in str(options.get("allowed_numbers") or "").replace(";", ",").split(",")
    if item.strip()
]

api_token = str(options.get("api_token") or "").strip()
if not api_token or api_token == "change-me-please":
    import secrets
    api_token = secrets.token_urlsafe(24)
    Path("/data/api_token.txt").write_text(api_token + "\n", encoding="utf-8")
    print("Generated API token (also saved to /data/api_token.txt):", api_token)

config = f"""
[bridge]
name = "GSM-шлюз SIM7600"
host = "0.0.0.0"
port = 8099
api_token = {q(api_token)}
log_level = {q(options.get("log_level") or "INFO")}
state_dir = "/data"
allow_anonymous_camera = false

[athena]
enabled = {flag(options.get("athena_enabled", True))}
url = {q(options.get("athena_url") or "")}
token = {q(options.get("athena_token") or "")}
room_hint = ""
heartbeat_seconds = 20
verify_tls = {flag(options.get("athena_verify_tls", True))}

[modem]
enabled = true
port = {q(options.get("modem_port") or "/dev/ttyUSB2")}
baudrate = 115200
mock = {flag(options.get("modem_mock", False))}
unknown_callers = {q(options.get("unknown_callers") or "notify")}
allowed_numbers = {json.dumps(numbers, ensure_ascii=False)}
bridge_room = {q(options.get("bridge_room") or "")}
auto_answer = false

[camera]
enabled = {flag(options.get("camera_enabled", True))}
device = {q(options.get("camera_device") or "/dev/video0")}
mock = {flag(options.get("camera_mock", False))}
width = 1280
height = 720
fps = 15
input_format = "auto"
snapshot_on_ring = true
snapshot_dir = "/data/snapshots"
motion_detection = true
motion_threshold = 0.035

[audio_bridge]
enabled = {flag(options.get("audio_enabled", True))}
pcm_port = {q(options.get("pcm_port") or "/dev/ttyUSB4")}
pcm_baudrate = 115200
audio_transport = "auto"
playback_volume = {int(options.get("playback_volume") or 50)}
capture_device = {q(options.get("audio_device") or "auto")}
playback_device = {q(options.get("audio_device") or "auto")}
sample_rate = 16000
chunk_ms = 20
hold_before_bridge = false
"""

Path("/data/config.toml").write_text(config, encoding="utf-8")
print("config.toml written from add-on options")
PY

exec python3 -m sim7600_gateway -c /data/config.toml
