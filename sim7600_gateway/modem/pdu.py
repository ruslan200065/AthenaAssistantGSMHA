"""GSM 03.38 / 03.40 SMS PDU codec.

Supports SMS-SUBMIT (GSM-7 and UCS-2), concatenation via UDH, and
SMS-DELIVER decoding (including alphanumeric senders and timestamps).
No external dependencies.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

# GSM 03.38 default alphabet, positions 0x00..0x7F.
_GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

_GSM7_EXTENSION = {
    0x0A: "\f",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "€",
}

_GSM7_DECODE: dict[int, str] = {i: ch for i, ch in enumerate(_GSM7_BASIC)}
_GSM7_ENCODE: dict[str, int] = {}
for _i, _ch in enumerate(_GSM7_BASIC):
    if _ch != "\x1b":
        _GSM7_ENCODE.setdefault(_ch, _i)
_GSM7_EXT_ENCODE: dict[str, int] = {v: k for k, v in _GSM7_EXTENSION.items()}

GSM7_SINGLE_LIMIT = 160
GSM7_PART_LIMIT = 153
UCS2_SINGLE_LIMIT = 70
UCS2_PART_LIMIT = 67

MAX_PARTS = 8


class SmsEncodingError(ValueError):
    pass


@dataclass(slots=True)
class SubmitPart:
    pdu: str
    tpdu_length: int


@dataclass(slots=True)
class DeliverSms:
    sender: str
    text: str
    timestamp: str | None
    dcs: int
    concat: tuple[int, int, int] | None
    raw: str


# ── encoding ──────────────────────────────────────────────────────────────────


def gsm7_septets(text: str) -> list[int] | None:
    """Return septets or ``None`` if the text needs UCS-2."""
    septets: list[int] = []
    for ch in text:
        code = _GSM7_ENCODE.get(ch)
        if code is not None:
            septets.append(code)
            continue
        ext = _GSM7_EXT_ENCODE.get(ch)
        if ext is not None:
            septets.extend((0x1B, ext))
            continue
        return None
    return septets


def _pack_7bit(septets: list[int]) -> bytes:
    out = bytearray()
    buffer = 0
    bits = 0
    for septet in septets:
        buffer |= (septet & 0x7F) << bits
        bits += 7
        while bits >= 8:
            out.append(buffer & 0xFF)
            buffer >>= 8
            bits -= 8
    if bits:
        out.append(buffer & 0xFF)
    return bytes(out)


def _unpack_7bit(data: bytes) -> list[int]:
    out: list[int] = []
    buffer = 0
    bits = 0
    for byte in data:
        buffer |= byte << bits
        bits += 8
        while bits >= 7:
            out.append(buffer & 0x7F)
            buffer >>= 7
            bits -= 7
    return out


def _pack_7bit_with_udh(udh: bytes, septets: list[int]) -> tuple[bytes, int]:
    """Pack UDH + text into one septet stream. Returns (data, UDL in septets)."""
    out = bytearray()
    buffer = 0
    bits = 0
    for byte in udh:
        buffer |= byte << bits
        bits += 8
    fill = (-bits) % 7
    bits += fill
    header_septets = bits // 7
    for septet in septets:
        buffer |= (septet & 0x7F) << bits
        bits += 7
    while bits >= 8:
        out.append(buffer & 0xFF)
        buffer >>= 8
        bits -= 8
    if bits:
        out.append(buffer & 0xFF)
    return bytes(out), header_septets + len(septets)


def _concat_udh(ref: int, total: int, seq: int) -> bytes:
    return bytes((0x05, 0x00, 0x03, ref & 0xFF, total, seq))


def _encode_address(number: str) -> str:
    """Return the TP-DA field: length octet, TOA, semi-octet digits."""
    value = number.strip()
    if value.startswith("+"):
        toa = "91"
        digits = value[1:]
    else:
        toa = "81"
        digits = value
    clean = "".join(ch for ch in digits if ch.isdigit())
    if not clean:
        raise SmsEncodingError("number has no digits")
    digit_count = len(clean)
    if len(clean) % 2:
        clean += "F"
    swapped = "".join(clean[i + 1] + clean[i] for i in range(0, len(clean), 2))
    return f"{digit_count:02X}{toa}{swapped}"


def _submit_pdu(
    number: str,
    *,
    mr: int,
    dcs: int,
    user_data: bytes,
    udl: int,
    has_udh: bool = False,
    smsc: str = "",
) -> SubmitPart:
    address = _encode_address(number)
    first_octet = 0x01 | (0x40 if has_udh else 0x00)
    tpdu = (
        f"{first_octet:02X}"
        f"{mr & 0xFF:02X}"
        f"{address}"
        "00"  # TP-PID
        f"{dcs:02X}"
        f"{udl:02X}"
        f"{user_data.hex().upper()}"
    )
    if smsc:
        clean = "".join(ch for ch in smsc if ch.isdigit())
        if len(clean) % 2:
            clean += "F"
        swapped = "".join(clean[i + 1] + clean[i] for i in range(0, len(clean), 2))
        smsc_field = f"{len(clean) // 2:02X}91{swapped}"
    else:
        smsc_field = "00"
    return SubmitPart(pdu=(smsc_field + tpdu).upper(), tpdu_length=len(tpdu) // 2)


def _split_septets(septets: list[int]) -> list[list[int]]:
    """Split into UDH-sized chunks without cutting a 0x1B escape pair.

    ``€ { } [ ] ~ | ^ \\`` occupy two septets (``0x1B`` + code). A naive
    slice at 153 can put the escape marker at the end of one part and the
    code in the next, which loses/corrupts the character on the receiving
    side.
    """
    chunks: list[list[int]] = []
    start = 0
    while start < len(septets):
        end = min(start + GSM7_PART_LIMIT, len(septets))
        if end < len(septets) and septets[end - 1] == 0x1B:
            end -= 1
        if end <= start:  # pragma: no cover - only for malformed input
            end = start + 1
        chunks.append(septets[start:end])
        start = end
    return chunks


def _split_ucs2(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for ch in text:
        cost = len(ch.encode("utf-16-be"))
        if size + cost > UCS2_PART_LIMIT * 2:
            parts.append("".join(current))
            current = []
            size = 0
        current.append(ch)
        size += cost
    if current:
        parts.append("".join(current))
    return parts


def build_submit(
    number: str,
    text: str,
    mr: int = 0,
    smsc: str = "",
    reference: int | None = None,
) -> list[SubmitPart]:
    """Encode an SMS into one or more SMS-SUBMIT PDUs."""
    if not number:
        raise SmsEncodingError("empty destination")
    if not text:
        raise SmsEncodingError("empty text")

    septets = gsm7_septets(text)

    if septets is not None and len(septets) <= GSM7_SINGLE_LIMIT:
        return [
            _submit_pdu(
                number,
                mr=mr,
                dcs=0x00,
                user_data=_pack_7bit(septets),
                udl=len(septets),
                smsc=smsc,
            )
        ]

    if septets is None and len(text.encode("utf-16-be")) <= UCS2_SINGLE_LIMIT * 2:
        data = text.encode("utf-16-be")
        return [
            _submit_pdu(
                number,
                mr=mr,
                dcs=0x08,
                user_data=data,
                udl=len(data),
                smsc=smsc,
            )
        ]

    ref = secrets.randbelow(256) if reference is None else reference & 0xFF
    parts: list[SubmitPart] = []

    if septets is not None:
        chunks = _split_septets(septets)
        if len(chunks) > MAX_PARTS:
            raise SmsEncodingError("text is too long for a concatenated SMS")
        total = len(chunks)
        for seq, chunk in enumerate(chunks, start=1):
            udh = _concat_udh(ref, total, seq)
            data, udl = _pack_7bit_with_udh(udh, chunk)
            parts.append(
                _submit_pdu(
                    number,
                    mr=mr,
                    dcs=0x00,
                    user_data=data,
                    udl=udl,
                    has_udh=True,
                    smsc=smsc,
                )
            )
        return parts

    chunks_text = _split_ucs2(text)
    if len(chunks_text) > MAX_PARTS:
        raise SmsEncodingError("text is too long for a concatenated SMS")
    total = len(chunks_text)
    for seq, chunk in enumerate(chunks_text, start=1):
        udh = _concat_udh(ref, total, seq)
        data = udh + chunk.encode("utf-16-be")
        parts.append(
            _submit_pdu(
                number,
                mr=mr,
                dcs=0x08,
                user_data=data,
                udl=len(data),
                has_udh=True,
                smsc=smsc,
            )
        )
    return parts


def build_deliver(
    number: str,
    text: str,
    *,
    timestamp: datetime | None = None,
    concat: tuple[int, int, int] | None = None,
    smsc: str = "",
) -> str:
    """Build an SMS-DELIVER PDU (used by the simulator and tests)."""
    address = _encode_address(number)
    septets = gsm7_septets(text)
    udh = b""
    if concat is not None:
        udh = bytes((0x05, 0x00, 0x03, concat[0] & 0xFF, concat[1], concat[2]))
    if septets is not None:
        dcs = 0x00
        if udh:
            user_data, udl = _pack_7bit_with_udh(udh, septets)
        else:
            user_data, udl = _pack_7bit(septets), len(septets)
    else:
        dcs = 0x08
        user_data = udh + text.encode("utf-16-be")
        udl = len(user_data)

    stamp = timestamp or datetime.now().astimezone()
    digits = (
        f"{stamp.year % 100:02d}{stamp.month:02d}{stamp.day:02d}"
        f"{stamp.hour:02d}{stamp.minute:02d}{stamp.second:02d}"
    )
    swapped = "".join(digits[i + 1] + digits[i] for i in range(0, len(digits), 2))
    offset = stamp.utcoffset()
    quarters = int((offset.total_seconds() if offset else 0) / 900)
    negative = quarters < 0
    quarters = abs(quarters)
    tens, units = divmod(quarters, 10)
    tz_octet = ((units & 0x0F) << 4) | (tens & 0x07) | (0x08 if negative else 0)
    scts = swapped + f"{tz_octet:02X}"

    first_octet = 0x04 | (0x40 if udh else 0x00)
    tpdu = (
        f"{first_octet:02X}"
        f"{address}"
        "00"  # TP-PID
        f"{dcs:02X}"
        f"{scts}"
        f"{udl:02X}"
        f"{user_data.hex().upper()}"
    )
    if smsc:
        clean = "".join(ch for ch in smsc if ch.isdigit())
        if len(clean) % 2:
            clean += "F"
        swapped_smsc = "".join(clean[i + 1] + clean[i] for i in range(0, len(clean), 2))
        smsc_field = f"{len(clean) // 2:02X}91{swapped_smsc}"
    else:
        smsc_field = "00"
    return (smsc_field + tpdu).upper()


# ── decoding ──────────────────────────────────────────────────────────────────


def _decode_numeric(toa: int, data: bytes, digits_len: int) -> str:
    nibbles: list[str] = []
    for byte in data:
        nibbles.append(f"{byte & 0x0F:X}")
        nibbles.append(f"{byte >> 4:X}")
    joined = "".join(nibbles)[:digits_len]
    if "F" in joined:
        joined = joined[: joined.index("F")]
    prefix = "+" if (toa & 0x70) == 0x10 else ""
    return prefix + joined


def _decode_septets(septets: list[int]) -> str:
    out: list[str] = []
    escape = False
    for septet in septets:
        if escape:
            out.append(_GSM7_EXTENSION.get(septet, " "))
            escape = False
            continue
        if septet == 0x1B:
            escape = True
            continue
        out.append(_GSM7_DECODE.get(septet, " "))
    return "".join(out)


def _decode_7bit(data: bytes, septet_count: int | None = None) -> str:
    septets = _unpack_7bit(data)
    if septet_count is not None:
        septets = septets[:septet_count]
    return _decode_septets(septets)


def _decode_scts(octets: bytes) -> str | None:
    if len(octets) < 7:
        return None
    try:
        digits = "".join(f"{b & 0x0F}{b >> 4}" for b in octets[:6])
        year = 2000 + int(digits[0:2])
        month = int(digits[2:4])
        day = int(digits[4:6])
        hour = int(digits[6:8])
        minute = int(digits[8:10])
        second = int(digits[10:12])
        tz = octets[6]
        negative = bool(tz & 0x08)
        quarters = (tz & 0x07) * 10 + (tz >> 4)
        total_minutes = quarters * 15 * (-1 if negative else 1)
        sign = "-" if total_minutes < 0 else "+"
        total_minutes = abs(total_minutes)
        return (
            f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}"
            f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"
        )
    except (ValueError, IndexError):
        return None


def _parse_udh(data: bytes) -> tuple[int, tuple[int, int, int] | None]:
    """Return (header length in octets, concat tuple)."""
    if len(data) < 2:
        return 0, None
    header_len = data[0] + 1
    if header_len > len(data):
        return 0, None
    concat: tuple[int, int, int] | None = None
    pos = 1
    while pos + 1 < header_len:
        iei = data[pos]
        iedl = data[pos + 1]
        value = data[pos + 2 : pos + 2 + iedl]
        if iei == 0x00 and len(value) == 3:
            concat = (value[0], value[1], value[2])
        elif iei == 0x08 and len(value) == 4:
            concat = (int.from_bytes(value[0:2], "big"), value[2], value[3])
        pos += 2 + iedl
    return header_len, concat


def _sms_alphabet(dcs: int) -> str:
    """Classify an SMS DCS as gsm7 / ucs2 / binary (TS 23.038 ch. 4)."""
    group = dcs >> 4
    if group <= 0x07:
        alphabet = (dcs >> 2) & 0x03
        if alphabet == 0:
            return "gsm7"
        if alphabet == 1:
            return "binary"
        if alphabet == 2:
            return "ucs2"
        return "gsm7"
    if group in (0x0C, 0x0D):
        return "gsm7"
    if group == 0x0E:
        return "ucs2"
    if group == 0x0F:
        return "binary" if dcs & 0x04 else "gsm7"
    return "gsm7"


_MOJIBAKE_ZONES = (
    (0x00A0, 0x024F),  # Latin-1 letters and extended Latin
    (0x0370, 0x03FF),  # Greek
)


def _looks_garbled(text: str) -> bool:
    """True when a decoded SMS has letters but not one Cyrillic letter.

    Russian messages that came out of the wrong alphabet read like «Ωòè!»:
    letters are there, the word is not.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 2:
        return False
    if any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in letters):
        return False
    in_zone = sum(
        1 for ch in letters if any(low <= ord(ch) <= high for low, high in _MOJIBAKE_ZONES)
    )
    return in_zone >= max(2, len(letters) // 2)


def _try_ucs2(ud: bytes, header_len: int, udhi: bool) -> str | None:
    """Re-read the body as UTF-16BE when the GSM-7 reading made no sense."""
    body = ud[header_len:] if udhi else ud
    if len(body) < 4 or len(body) % 2:
        body = body[: len(body) - (len(body) % 2)]
    if not body:
        return None
    try:
        candidate = body.decode("utf-16-be")
    except UnicodeDecodeError:
        return None
    letters = [ch for ch in candidate if ch.isalpha()]
    if not letters:
        return None
    if any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in letters):
        return candidate
    return None


def parse_deliver(pdu_hex: str) -> DeliverSms:
    """Decode an SMS-DELIVER PDU (hex string, with SMSC field)."""
    raw = pdu_hex.strip().replace(" ", "").upper()
    data = bytes.fromhex(raw)
    if len(data) < 2:
        raise SmsEncodingError("PDU is too short")

    smsc_len = data[0]
    pos = 1 + smsc_len
    if pos >= len(data):
        raise SmsEncodingError("PDU has no TPDU")

    first_octet = data[pos]
    pos += 1
    udhi = bool(first_octet & 0x40)

    addr_len = data[pos]
    pos += 1
    toa = data[pos]
    pos += 1

    if (toa & 0x70) == 0x50 or (toa & 0xF0) == 0xD0:  # alphanumeric
        addr_bytes = (addr_len * 7 + 7) // 8
        sender = _decode_7bit(data[pos : pos + addr_bytes], addr_len)
        pos += addr_bytes
    else:
        addr_bytes = (addr_len + 1) // 2
        sender = _decode_numeric(toa, data[pos : pos + addr_bytes], addr_len)
        pos += addr_bytes

    pos += 1  # TP-PID
    dcs = data[pos]
    pos += 1
    scts = data[pos : pos + 7]
    pos += 7
    timestamp = _decode_scts(scts)

    udl = data[pos]
    pos += 1
    ud = data[pos:]

    header_len, concat = _parse_udh(ud) if udhi else (0, None)

    alphabet = _sms_alphabet(dcs)
    if alphabet == "ucs2":
        body = ud[header_len:] if udhi else ud
        text = body.decode("utf-16-be", errors="replace")
    elif alphabet == "binary":
        body = ud[header_len:] if udhi else ud
        text = body.decode("utf-8", errors="replace")
    else:  # GSM-7
        septets = _unpack_7bit(ud)
        if udhi:
            skip = (header_len * 8 + 6) // 7
            septets = septets[skip : skip + max(0, udl - skip)]
        else:
            septets = septets[:udl]
        text = _decode_septets(septets)
        # Some operators send UCS2 text under a GSM-7 data coding scheme. The
        # result reads as Greek/Latin gibberish («Ωòè!» instead of «Это!»), so
        # when that happens try the bytes as UTF-16 once more.
        if _looks_garbled(text):
            fixed = _try_ucs2(ud, header_len, udhi)
            if fixed:
                text = fixed

    return DeliverSms(
        sender=sender,
        text=text,
        timestamp=timestamp,
        dcs=dcs,
        concat=concat,
        raw=raw,
    )
