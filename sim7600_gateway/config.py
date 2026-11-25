"""Configuration: dataclasses, TOML loading, environment overrides."""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_INIT_SEQUENCE = [
    "ATE0",
    "AT+CMEE=2",
    "AT+CMGF=0",
    "AT+CNMI=2,1,0,0,0",
    "AT+CLIP=1",
    "AT+CRC=1",
    "AT+DDET=1",
    'AT+CPMS="ME","ME","ME"',
]


def _build(cls: type, data: dict[str, Any] | None):
    """Instantiate a dataclass, ignoring unknown keys and coercing simple types."""
    data = data or {}
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if value is None:
            continue
        kwargs[f.name] = value
    return cls(**kwargs)


@dataclass
class BridgeConfig:
    name: str = "GSM-шлюз SIM7600"
    host: str = "127.0.0.1"
    port: int = 8099
    api_token: str = ""
    log_level: str = "INFO"
    state_dir: str = "./data"
    allow_anonymous_camera: bool = False


@dataclass
class AthenaConfig:
    enabled: bool = True
    url: str = ""
    token: str = ""
    device_id: str = "sim7600-gateway"
    room_hint: str = ""
    heartbeat_seconds: int = 20
    verify_tls: bool = True


@dataclass
class ModemConfig:
    enabled: bool = True
    port: str = "/dev/ttyUSB2"
    baudrate: int = 115200
    mock: bool = False
    init_sequence: list[str] = field(default_factory=lambda: list(DEFAULT_INIT_SEQUENCE))
    unknown_callers: str = "notify"
    allowed_numbers: list[str] = field(default_factory=list)
    reject_numbers: list[str] = field(default_factory=list)
    auto_answer: bool = False
    auto_answer_delay: float = 1.0
    bridge_room: str = ""
    bridge_known_only: bool = True
    watchdog_timeouts: int = 3
    reset_method: str = "at"
    reset_gpio: int = 0
    sms_storage: str = "ME"


@dataclass
class AudioBridgeConfig:
    enabled: bool = False
    capture_device: str = "auto"
    playback_device: str = "auto"
    sample_rate: int = 16000
    chunk_ms: int = 20
    # Experimental: AT+CHLD=2 holds the active call while the PCM bridge is
    # being set up. Disabled by default because some SIM7600 firmwares stop
    # routing call audio to USB while the line is held.
    hold_before_bridge: bool = False


@dataclass
class CameraConfig:
    enabled: bool = True
    device: str = "/dev/video0"
    mock: bool = False
    width: int = 1280
    height: int = 720
    fps: int = 15
    input_format: str = "auto"
    ffmpeg: str = "ffmpeg"
    v4l2ctl: str = "v4l2-ctl"
    snapshot_on_ring: bool = True
    snapshot_dir: str = "./data/snapshots"
    clip_on_ring: bool = False
    clip_seconds: int = 12
    motion_detection: bool = True
    motion_threshold: float = 0.035
    motion_cooldown: float = 30.0


@dataclass
class Config:
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    athena: AthenaConfig = field(default_factory=AthenaConfig)
    modem: ModemConfig = field(default_factory=ModemConfig)
    audio_bridge: AudioBridgeConfig = field(default_factory=AudioBridgeConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    source: str = "<defaults>"

    @property
    def state_dir(self) -> Path:
        return Path(self.bridge.state_dir).expanduser()

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        Path(self.camera.snapshot_dir).expanduser().mkdir(parents=True, exist_ok=True)


_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, current: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return current
    return raw.strip().lower() in _TRUE


def _env_int(name: str, current: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return current
    try:
        return int(raw)
    except ValueError:
        return current


def _env_float(name: str, current: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return current
    try:
        return float(raw)
    except ValueError:
        return current


def _env_str(name: str, current: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return current
    return raw


def apply_env(cfg: Config) -> Config:
    cfg.bridge.host = _env_str("BRIDGE_HOST", cfg.bridge.host)
    cfg.bridge.port = _env_int("BRIDGE_PORT", cfg.bridge.port)
    cfg.bridge.api_token = _env_str("API_TOKEN", cfg.bridge.api_token)
    cfg.bridge.log_level = _env_str("LOG_LEVEL", cfg.bridge.log_level)
    cfg.bridge.state_dir = _env_str("STATE_DIR", cfg.bridge.state_dir)
    cfg.bridge.allow_anonymous_camera = _env_bool(
        "ALLOW_ANONYMOUS_CAMERA", cfg.bridge.allow_anonymous_camera
    )

    cfg.athena.enabled = _env_bool("ATHENA_ENABLED", cfg.athena.enabled)
    cfg.athena.url = _env_str("ATHENA_URL", cfg.athena.url)
    cfg.athena.token = _env_str("ATHENA_TOKEN", cfg.athena.token)
    cfg.athena.room_hint = _env_str("ATHENA_ROOM_HINT", cfg.athena.room_hint)
    cfg.athena.device_id = _env_str("ATHENA_DEVICE_ID", cfg.athena.device_id)
    cfg.athena.heartbeat_seconds = _env_int("ATHENA_HEARTBEAT", cfg.athena.heartbeat_seconds)

    cfg.modem.enabled = _env_bool("MODEM_ENABLED", cfg.modem.enabled)
    cfg.modem.port = _env_str("MODEM_PORT", cfg.modem.port)
    cfg.modem.baudrate = _env_int("MODEM_BAUD", cfg.modem.baudrate)
    cfg.modem.mock = _env_bool("MODEM_MOCK", cfg.modem.mock)

    cfg.camera.enabled = _env_bool("CAMERA_ENABLED", cfg.camera.enabled)
    cfg.camera.device = _env_str("CAMERA_DEVICE", cfg.camera.device)
    cfg.camera.mock = _env_bool("CAMERA_MOCK", cfg.camera.mock)
    cfg.camera.width = _env_int("CAMERA_WIDTH", cfg.camera.width)
    cfg.camera.height = _env_int("CAMERA_HEIGHT", cfg.camera.height)
    cfg.camera.fps = _env_int("CAMERA_FPS", cfg.camera.fps)
    cfg.camera.motion_detection = _env_bool("CAMERA_MOTION", cfg.camera.motion_detection)
    cfg.camera.snapshot_dir = _env_str("CAMERA_SNAPSHOT_DIR", cfg.camera.snapshot_dir)

    cfg.audio_bridge.enabled = _env_bool("AUDIO_BRIDGE_ENABLED", cfg.audio_bridge.enabled)
    cfg.audio_bridge.capture_device = _env_str(
        "AUDIO_CAPTURE_DEVICE", cfg.audio_bridge.capture_device
    )
    cfg.audio_bridge.playback_device = _env_str(
        "AUDIO_PLAYBACK_DEVICE", cfg.audio_bridge.playback_device
    )
    return cfg


def load_config(path: str | Path | None = None) -> Config:
    """Load TOML (optional) and apply environment overrides."""
    candidate = path or os.environ.get("GATEWAY_CONFIG") or "config.toml"
    file_path = Path(candidate).expanduser()
    raw: dict[str, Any] = {}
    source = "<defaults>"
    if file_path.is_file():
        with file_path.open("rb") as fh:
            raw = tomllib.load(fh)
        source = str(file_path)
    cfg = Config(
        bridge=_build(BridgeConfig, raw.get("bridge")),
        athena=_build(AthenaConfig, raw.get("athena")),
        modem=_build(ModemConfig, raw.get("modem")),
        audio_bridge=_build(AudioBridgeConfig, raw.get("audio_bridge")),
        camera=_build(CameraConfig, raw.get("camera")),
        source=source,
    )
    apply_env(cfg)
    return cfg


def validate(cfg: Config) -> list[str]:
    """Return human-readable warnings (never raise for a half-configured box)."""
    warnings: list[str] = []
    if cfg.athena.enabled:
        if not cfg.athena.url:
            warnings.append("athena.url is empty — core link disabled")
        if not cfg.athena.token.startswith("dev_"):
            warnings.append("athena.token must start with 'dev_'")
        elif "REPLACE" in cfg.athena.token:
            warnings.append("athena.token still contains the example placeholder")
    if not cfg.bridge.api_token or cfg.bridge.api_token == "change-me-please":
        warnings.append("bridge.api_token is not configured — REST API is unprotected")
    if cfg.bridge.host not in ("127.0.0.1", "localhost", "::1"):
        warnings.append(
            "bridge.host is not loopback — the API must be protected by a strong api_token"
        )
    if cfg.modem.unknown_callers not in ("allow", "notify", "reject"):
        warnings.append("modem.unknown_callers must be allow|notify|reject")
    if cfg.modem.sms_storage.upper() not in ("ME", "SM", "MT"):
        warnings.append("modem.sms_storage must be ME|SM|MT")
    if cfg.audio_bridge.enabled:
        if not 8000 <= cfg.audio_bridge.sample_rate <= 48000:
            warnings.append("audio_bridge.sample_rate must be between 8000 and 48000")
        if not 5 <= cfg.audio_bridge.chunk_ms <= 100:
            warnings.append("audio_bridge.chunk_ms must be between 5 and 100")
    if cfg.camera.input_format not in ("auto", "mjpg", "yuyv", "h264"):
        warnings.append("camera.input_format must be auto|mjpg|yuyv|h264")
    return warnings
