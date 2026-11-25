"""In-process event bus for SSE and Home Assistant."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EventRecord:
    seq: int
    type: str
    data: dict[str, Any]
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "data": self.data, "at": self.at}


class EventBus:
    def __init__(self, history: int = 200):
        self._history: deque[EventRecord] = deque(maxlen=history)
        self._subscribers: set[asyncio.Queue[EventRecord]] = set()
        self._seq = 0

    async def publish(self, event_type: str, data: dict[str, Any] | None = None) -> EventRecord:
        self._seq += 1
        record = EventRecord(seq=self._seq, type=event_type, data=data or {})
        self._history.append(record)
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(record)
        return record

    def recent(self, limit: int = 50, event_type: str | None = None) -> list[dict[str, Any]]:
        records = list(self._history)
        if event_type:
            records = [record for record in records if record.type == event_type]
        return [record.to_dict() for record in records[-limit:]]

    def open_subscription(self) -> asyncio.Queue[EventRecord]:
        queue: asyncio.Queue[EventRecord] = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def close_subscription(self, queue: asyncio.Queue[EventRecord]) -> None:
        self._subscribers.discard(queue)

    async def subscribe(self) -> AsyncIterator[EventRecord]:
        queue: asyncio.Queue[EventRecord] = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
