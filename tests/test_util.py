"""Number normalization, redaction and masking."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.util import (  # noqa: E402
    mask_numbers,
    normalize_number,
    percent_from_dbm,
    redact_number,
)


def test_normalize_russian_numbers() -> None:
    assert normalize_number("+7 999 123-45-67") == "+79991234567"
    assert normalize_number("8 (999) 123-45-67") == "+79991234567"
    assert normalize_number("9991234567") == "+79991234567"
    assert normalize_number("9991234567", "+44") == "+449991234567"


def test_normalize_international() -> None:
    assert normalize_number("+44 20 7946 0958") == "+442079460958"
    assert normalize_number("442079460958") == "442079460958"


def test_normalize_invalid() -> None:
    assert normalize_number("") is None
    assert normalize_number("123") is None
    assert normalize_number("позвони маме") is None


def test_redaction_keeps_tail() -> None:
    assert redact_number("+79991234567") == "+7***4567"
    assert redact_number(None) == "—"
    assert redact_number("123") == "***"


def test_mask_numbers_in_log_lines() -> None:
    line = 'ATD+79991234567; +CLIP: "+79995554433",145'
    masked = mask_numbers(line)
    assert "9991234567" not in masked
    assert "9995554433" not in masked
    assert "ATD" in masked


def test_signal_percent() -> None:
    assert percent_from_dbm(-113) == 0
    assert percent_from_dbm(-51) == 100
    assert percent_from_dbm(-69) == 71
