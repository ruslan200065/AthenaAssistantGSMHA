"""Small shared helpers: number normalization, redaction, time."""

from __future__ import annotations

import re
from datetime import UTC, datetime

DEFAULT_COUNTRY_CODE = "+7"

_NON_DIGIT = re.compile(r"\D")


def normalize_number(raw: str | None, default_cc: str = DEFAULT_COUNTRY_CODE) -> str | None:
    """Normalize a phone number the same way Athena Core does.

    ``+7XXXXXXXXXX`` stays as is, ``8XXXXXXXXXX`` becomes ``+7XXXXXXXXXX``,
    10 digits get the default country code. Returns ``None`` for anything that
    does not look like a phone number.
    """
    if not raw:
        return None
    value = raw.strip().replace("(", "").replace(")", "").replace(" ", "")
    value = value.replace("-", "").replace(".", "")
    digits = _NON_DIGIT.sub("", value)
    if not digits:
        return None
    if value.startswith("+"):
        result = "+" + digits
    elif len(digits) == 11 and digits[0] in ("7", "8"):
        result = "+7" + digits[1:]
    elif len(digits) == 10:
        result = default_cc + digits
    else:
        result = digits
    bare = result.lstrip("+")
    if not 5 <= len(bare) <= 15:
        return None
    return result


def redact_number(number: str | None) -> str:
    """Keep the shape of a number visible in logs without leaking it."""
    if not number:
        return "—"
    digits = _NON_DIGIT.sub("", number)
    if len(digits) <= 6:
        return "***"
    return f"{number[:2]}***{digits[-4:]}"


def now_iso() -> str:
    """UTC timestamp in the format Athena Core expects."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def percent_from_dbm(dbm: int) -> int:
    """Map CSQ dBm (-113..-51) to a 0..100 percentage."""
    if dbm <= -113:
        return 0
    if dbm >= -51:
        return 100
    return round((dbm + 113) * 100 / 62)


_MASKABLE_NUMBER = re.compile(r"\+?\d[\d\s\-()]{4,}\d")


def mask_numbers(text: str) -> str:
    """Replace phone-like digit sequences in a log line."""
    return _MASKABLE_NUMBER.sub("***", text)

