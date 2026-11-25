"""Concatenated SMS (UDH) assembler.

SMS-DELIVER segments arrive independently (``+CMTI``/``+CMT``). The gateway
must emit one ``sms.incoming`` event with the full text only after all parts
of a concatenated message have been received.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .pdu import DeliverSms

DEFAULT_TTL = 120.0
DEFAULT_MAX_PARTS = 8
DEFAULT_MAX_PENDING = 32


@dataclass(slots=True)
class AssembledSms:
    sender: str
    text: str
    timestamp: str | None
    indices: list[int] = field(default_factory=list)


@dataclass
class _Pending:
    parts: dict[int, str] = field(default_factory=dict)
    timestamp: str | None = None
    indices: list[int] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)


class SmsAssembler:
    """Collect UDH segments keyed by ``(sender, reference, total)``."""

    def __init__(
        self,
        ttl: float = DEFAULT_TTL,
        max_parts: int = DEFAULT_MAX_PARTS,
        max_pending: int = DEFAULT_MAX_PENDING,
    ):
        self.ttl = ttl
        self.max_parts = max_parts
        self.max_pending = max_pending
        self._pending: dict[tuple[str, int, int], _Pending] = {}
        self._orphaned: list[int] = []

    @property
    def pending(self) -> int:
        return len(self._pending)

    def add(self, message: DeliverSms, index: int | None = None) -> AssembledSms | None:
        if message.concat is None:
            return AssembledSms(
                sender=message.sender,
                text=message.text,
                timestamp=message.timestamp,
                indices=[] if index is None else [index],
            )

        reference, total, seq = message.concat
        if total < 1 or total > self.max_parts or seq < 1 or seq > total:
            # Malformed UDH: do not swallow the message completely.
            return AssembledSms(
                sender=message.sender,
                text=message.text,
                timestamp=message.timestamp,
                indices=[] if index is None else [index],
            )

        key = (message.sender, reference, total)
        pending = self._pending.get(key)
        if pending is None:
            if len(self._pending) >= self.max_pending:
                oldest_key = next(iter(self._pending))
                evicted = self._pending.pop(oldest_key)
                self._orphaned.extend(evicted.indices)
            pending = _Pending()
            self._pending[key] = pending
        pending.parts[seq] = message.text
        pending.updated_at = time.monotonic()
        if pending.timestamp is None and message.timestamp is not None:
            pending.timestamp = message.timestamp
        if index is not None and index not in pending.indices:
            pending.indices.append(index)

        if len(pending.parts) < total or any(
            part not in pending.parts for part in range(1, total + 1)
        ):
            return None

        self._pending.pop(key, None)
        text = "".join(pending.parts[part] for part in range(1, total + 1))
        return AssembledSms(
            sender=message.sender,
            text=text,
            timestamp=pending.timestamp,
            indices=list(pending.indices),
        )

    def purge_expired(self) -> list[int]:
        """Drop incomplete messages older than TTL and return their indices.

        The caller can delete those segments from modem storage so they do
        not occupy slots forever.
        """
        now = time.monotonic()
        expired: list[int] = []
        for key, pending in list(self._pending.items()):
            if now - pending.updated_at >= self.ttl:
                expired.extend(pending.indices)
                self._pending.pop(key, None)
        expired.extend(self._orphaned)
        self._orphaned.clear()
        return expired
