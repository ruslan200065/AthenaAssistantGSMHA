"""PDU codec tests (vectors verified against the reference implementation)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sim7600_gateway.modem.pdu import (  # noqa: E402
    build_deliver,
    build_submit,
    gsm7_septets,
    parse_deliver,
)

NUMBER = "+79991234567"


def test_gsm7_single_part_vector() -> None:
    parts = build_submit(NUMBER, "hellohello", reference=0)
    assert len(parts) == 1
    assert parts[0].pdu == "0001000B919799214365F700000AE8329BFD4697D9EC37"
    assert parts[0].tpdu_length == 22


def test_ucs2_single_part_vector() -> None:
    text = "Привет, это Афина!"
    parts = build_submit(NUMBER, text, reference=0)
    assert len(parts) == 1
    assert parts[0].pdu == (
        "0001000B919799214365F7000824041F04400438043204350442002C0020044D0442"
        "043E0020041004440438043D04300021"
    )
    assert parts[0].tpdu_length == 49


def test_gsm7_extension_costs_two_septets() -> None:
    septets = gsm7_septets("€")
    assert septets == [0x1B, 0x65]
    parts = build_submit(NUMBER, "€" * 80)
    assert len(parts) == 1
    parts = build_submit(NUMBER, "€" * 81)
    assert len(parts) == 2


def test_long_gsm7_split_and_udh() -> None:
    parts = build_submit(NUMBER, "x" * 161, reference=7)
    assert len(parts) == 2
    for part in parts:
        assert part.pdu[2:4] == "41"  # UDHI bit set
        assert "0500030702" in part.pdu
    assert "050003070201" in parts[0].pdu
    assert "050003070202" in parts[1].pdu


def test_long_ucs2_split() -> None:
    parts = build_submit(NUMBER, "Привет " * 20)
    assert len(parts) >= 2
    for part in parts:
        assert part.pdu[2:4] == "41"


def test_deliver_roundtrip_ucs2() -> None:
    stamp = datetime(2026, 11, 25, 10, 30, 0, tzinfo=timezone(timedelta(hours=3)))
    pdu = build_deliver(NUMBER, "Привет!", timestamp=stamp)
    sms = parse_deliver(pdu)
    assert sms.sender == NUMBER
    assert sms.text == "Привет!"
    assert sms.timestamp == "2026-11-25T10:30:00+03:00"
    assert sms.dcs == 0x08
    assert sms.concat is None


def test_deliver_roundtrip_gsm7_and_known_vector() -> None:
    sms = parse_deliver("07919761989900F0040B919761123456F60000921032519440400BE8329BFD4697D9EC37")
    assert sms.sender == "+79162143656"
    assert sms.text == "hellohello"
    assert sms.timestamp == "2029-01-23T15:49:04+01:00"


def test_deliver_concatenation() -> None:
    first = build_deliver(NUMBER, "Привет, ", concat=(42, 2, 1))
    second = build_deliver(NUMBER, "мир!", concat=(42, 2, 2))
    one = parse_deliver(first)
    two = parse_deliver(second)
    assert one.concat == (42, 2, 1)
    assert two.concat == (42, 2, 2)
    assert one.text + two.text == "Привет, мир!"


def test_deliver_gsm7_concat_with_padding() -> None:
    pdu = build_deliver(NUMBER, "abcdefghij", concat=(9, 2, 1))
    sms = parse_deliver(pdu)
    assert sms.concat == (9, 2, 1)
    assert sms.text == "abcdefghij"


def test_alphanumeric_sender() -> None:
    pdu = "000406D0413ABAEC0E0300006211510103002105E8329BFD06"
    sms = parse_deliver(pdu)
    assert sms.sender == "Athena"
    assert sms.text == "hello"
