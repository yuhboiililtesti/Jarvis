"""In-process async event bus.

The bus is the seam between subsystems: the task controller publishes, the UI
websocket, monitoring and evidence views subscribe. It is intentionally simple
(bounded queues, drop-oldest) so a slow subscriber can never stall the Core.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from .ids import now_iso
from .logging_setup import get_logger

log = get_logger("jarvis.bus")

__all__ = ["Event", "EventBus"]


@dataclass
class Event:
    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=now_iso)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "data": self.data, "ts": self.ts, "seq": self.seq}


class EventBus:
    """Fan-out bus with per-subscriber bounded queues and a recent-history ring."""

    def __init__(self, history: int = 300) -> None:
        self._subs: list[asyncio.Queue[Event]] = []
        self._seq = 0
        self._history: list[Event] = []
        self._history_max = history
        self._tap_callbacks: list[Callable[[Event], None]] = []

    def tap(self, fn: Callable[[Event], None]) -> None:
        """Register a synchronous observer (used for audit + metrics)."""
        self._tap_callbacks.append(fn)

    def publish(self, topic: str, **data: Any) -> Event:
        self._seq += 1
        ev = Event(topic=topic, data=data, seq=self._seq)
        self._history.append(ev)
        if len(self._history) > self._history_max:
            self._history = self._history[-self._history_max:]
        for q in self._subs:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()      # drop oldest, keep newest
                    q.put_nowait(ev)
                except Exception:       # pragma: no cover - defensive
                    pass
        for cb in self._tap_callbacks:
            try:
                cb(ev)
            except Exception as exc:    # pragma: no cover - observer must not break bus
                log.warning("event tap failed: %s", exc)
        return ev

    def history(self, limit: int = 100) -> list[Event]:
        return self._history[-limit:]

    async def subscribe(self, maxsize: int = 500) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subs.append(q)
        try:
            # replay a short tail so a reconnecting UI is not blind
            for ev in self._history[-25:]:
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    break
            while True:
                yield await q.get()
        finally:
            if q in self._subs:
                self._subs.remove(q)

    # convenience topic helpers ------------------------------------------------
    def status(self, component: str, state: str, **extra: Any) -> None:
        self.publish("status", component=component, state=state, **extra)

    def degrade(self, component: str, reason: str, **extra: Any) -> None:
        self.publish("degraded", component=component, reason=reason, **extra)

    def error(self, component: str, message: str, **extra: Any) -> None:
        self.publish("error", component=component, message=message, **extra)
