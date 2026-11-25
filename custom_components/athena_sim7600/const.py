"""Constants for the Athena SIM7600 integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "athena_sim7600"
MANUFACTURER = "Athena"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.NOTIFY,
]

DEFAULT_SCAN_INTERVAL = 15
DEFAULT_TIMEOUT = 20

CONF_URL = "url"
CONF_TOKEN = "token"
CONF_VERIFY_SSL = "verify_ssl"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEFAULT_NUMBER = "default_sms_number"

EVENT_GATEWAY = "athena_sim7600_event"
EVENT_SMS = "athena_sim7600_sms_received"
EVENT_CALL = "athena_sim7600_incoming_call"
EVENT_CALL_STATE = "athena_sim7600_call_state"
EVENT_MOTION = "athena_sim7600_motion"

MOTION_HOLD_SECONDS = 60

SERVICE_SEND_SMS = "send_sms"
SERVICE_SEND_USSD = "send_ussd"
SERVICE_DIAL = "dial"
SERVICE_ANSWER = "answer_call"
SERVICE_HANGUP = "hangup_call"
SERVICE_RESET = "reset_modem"
SERVICE_SNAPSHOT = "take_snapshot"
SERVICE_RECORD = "record_clip"
SERVICE_BRIDGE = "bridge_call"

ATTR_ENTRY_ID = "entry_id"
