"""Modem state and snapshot shared by the modem, API and bridge layers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

REGISTRATION_STATES = {
    0: "not_registered",
    1: "registered_home",
    2: "searching",
    3: "denied",
    4: "unknown",
    5: "registered_roaming",
    6: "registered_home_sms_only",
    7: "registered_roaming_sms_only",
    8: "emergency_only",
    9: "registered_home_sms_only",
    10: "registered_roaming_sms_only",
}

REGISTERED_STATES = {1, 5, 6, 7, 9, 10}

ACT_CODES = {
    0: "GSM",
    1: "GSM Compact",
    2: "UMTS",
    3: "EDGE",
    4: "HSDPA",
    5: "HSUPA",
    6: "HSPA",
    7: "LTE",
    8: "CDMA2000",
    9: "EVDO",
    10: "EVDO rev.A",
    11: "EVDO rev.B",
    12: "LTE CA",
}


class ModemState:
    OFF = "off"
    INIT = "init"
    SIM_READY = "sim_ready"
    REGISTERED = "registered"
    IDLE = "idle"
    DIALING = "dialing"
    ACTIVE = "active"
    RINGING = "ringing"
    SMS = "sms_tx"
    USSD = "ussd"


@dataclass
class ModemSnapshot:
    state: str = ModemState.OFF
    sim_ready: bool = False
    sim_state: str = "unknown"
    registered: bool = False
    registration_status: int | None = None
    registration_text: str = "unknown"
    signal_dbm: int | None = None
    signal_percent: int | None = None
    operator: str | None = None
    network: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    firmware: str | None = None
    imei: str | None = None
    iccid: str | None = None
    phone_number: str | None = None
    call_active: bool = False
    ringing: bool = False
    caller: str | None = None
    call_started_at: float | None = None
    last_error: str | None = None
    consecutive_timeouts: int = 0
    uptime_sec: int = 0
    sms_sent: int = 0
    sms_received: int = 0
    updated_at: float = field(default_factory=time.time)

    @property
    def online(self) -> bool:
        return self.state not in (ModemState.OFF, ModemState.INIT)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "online": self.online,
            "sim_ready": self.sim_ready,
            "sim_state": self.sim_state,
            "registered": self.registered,
            "registration_status": self.registration_status,
            "registration_text": self.registration_text,
            "signal_dbm": self.signal_dbm,
            "signal_percent": self.signal_percent,
            "operator": self.operator,
            "network": self.network,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "firmware": self.firmware,
            "imei": self.imei,
            "iccid": self.iccid,
            "phone_number": self.phone_number,
            "call_active": self.call_active,
            "ringing": self.ringing,
            "caller": self.caller,
            "call_duration_sec": (
                int(time.time() - self.call_started_at)
                if self.call_active and self.call_started_at
                else 0
            ),
            "last_error": self.last_error,
            "consecutive_timeouts": self.consecutive_timeouts,
            "uptime_sec": self.uptime_sec,
            "sms_sent": self.sms_sent,
            "sms_received": self.sms_received,
            "updated_at": self.updated_at,
        }
