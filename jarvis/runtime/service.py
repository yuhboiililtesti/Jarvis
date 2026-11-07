"""The persistent runtime: the loop that makes JARVIS operate instead of wait.

Before this module the Core was purely reactive — it did something only while a
request was being answered. ``RuntimeService`` owns a durable tick loop that,
while the Core is up:

  * fires due scheduled jobs (once per occurrence, never replayed);
  * drains queued event reactions (through the normal policy/approval gate);
  * records a heartbeat the owner can inspect.

Safety and honesty rules:

  * **One active Core.** A lease row in ``kv`` names the owner and expires; only
    the owner reconciles, ticks and ingests mirrored bus events. A Crash's lease
    expires and another takes over. The scheduler's per-occurrence claim is the
    hard guarantee, not the lease.
  * **Bounded work.** Dispatch is capped by reactions/tasks already in flight,
    not merely by rows picked per tick, so a burst cannot create unbounded
    asyncio tasks. The scheduler likewise caps in-flight runs.
  * **The loop cannot die.** A failing tick is logged, audited and backed off;
    it never takes the process down.
  * **No hidden effects.** Every reaction is an ordinary task/attempt under the
    same policy, approval and verifier as a spoken request.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import socket
import time
import uuid
from typing import Any

from ..ids import now_iso
from ..logging_setup import get_logger
from .events import EventEngine
from .scheduler import Scheduler

log = get_logger("jarvis.runtime")

__all__ = ["RuntimeService"]

LEASE_KEY = "runtime_lease"
HEARTBEAT_KEY = "runtime_heartbeat"


class RuntimeService:
    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self.config = kernel.config
        self.rb = kernel.config.runtime
        self.db = kernel.db
        self.bus = kernel.bus
        self.scheduler = Scheduler(kernel)
        self.events = EventEngine(kernel)
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._loop_task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._fires: set[asyncio.Task] = set()
        self._sem: asyncio.Semaphore | None = None
        self._ready = False
        self._is_owner = False
        self._ticks = 0
        self._last_tick: str | None = None
        self._last_error: str | None = None
        self._started_at: str | None = None

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if not self.rb.enabled:
            log.info("runtime disabled by config")
            return
        self._sem = asyncio.Semaphore(max(1, int(self.rb.max_reaction_tasks)))
        self._stop = asyncio.Event()
        self._started_at = now_iso()
        # Take the lease *before* reconciling: a standby must not mark the live
        # owner's running claims as interrupted.
        self._is_owner = self._acquire_lease()
        self._install_taps()
        if self._is_owner:
            self.scheduler.reconcile()
            self.events.reconcile()
            goals = (self.kernel.services or {}).get("goals")
            if goals is not None:
                goals.reconcile()
            with contextlib.suppress(Exception):
                self.events.prune()
            self.bus.status("runtime", "started")
            log.info("runtime started (owner %s, tick %.1fs)", self._owner, self.rb.tick_s)
        else:
            self.bus.status("runtime", "standby")
            log.info("runtime standby: loop leased by %s",
                     self._lease().get("owner"))
        self._loop_task = asyncio.create_task(self._loop(), name="jarvis-runtime")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._ready = False
        task = self._loop_task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(Exception):
                    await task
        self.scheduler.cancel_all()
        for fire in list(self._fires):
            fire.cancel()
        if self._fires:
            await asyncio.gather(*self._fires, return_exceptions=True)
        await self.scheduler.wait_idle(5.0)
        self._is_owner = False
        self._release_lease()
        if self.rb.enabled:
            self.bus.status("runtime", "stopped")

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            delay = max(0.2, float(self.rb.tick_s))
            try:
                await self.tick()
            except Exception as exc:      # a bad tick must never kill the loop
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.exception("runtime tick failed")
                with contextlib.suppress(Exception):
                    self.db.audit("runtime.tick_failed",
                                  detail={"error": self._last_error}, severity="warn")
                delay = min(max(delay * 5.0, 5.0), 60.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    # -- one tick --------------------------------------------------------
    async def tick(self, now: Any = None, *, jobs: bool = True,
                   events: bool = True) -> dict[str, Any]:
        """Run one bounded step. Public so tests need no sleeping.

        ``jobs=False`` is used by the one-shot CLI tick: a process that is about
        to exit must not claim a scheduled occurrence it cannot finish.
        """
        if not self.rb.enabled:
            return {"enabled": False, "owner": False, "jobs": 0, "missed": 0, "events": 0}
        if not self._acquire_lease():
            return {"enabled": True, "owner": False, "jobs": 0, "missed": 0, "events": 0}

        claims = await self.scheduler.fire_due(now=now) if jobs else []
        fired = sum(1 for c in claims if not c["missed"])
        missed = len(claims) - fired

        dispatched = 0
        if events:
            # Bound *tasks in flight*, not rows returned: a pending row stays
            # pending until it is claimed, so re-dispatching it every tick would
            # create an unbounded number of asyncio tasks.
            capacity = max(0, int(self.rb.max_reaction_tasks) - len(self._fires))
            if capacity:
                pending = self.events.pending(
                    limit=min(int(self.rb.max_reactions_per_tick), capacity))
                for fire in pending:
                    task = asyncio.create_task(self._guarded_fire(fire),
                                               name=f"event:{fire.get('rule_id')}")
                    self._fires.add(task)
                    task.add_done_callback(self._fires.discard)
                dispatched = len(pending)

        goals_started = 0
        goals = (self.kernel.services or {}).get("goals")
        if goals is not None:
            goals_started = len(await goals.run_pending())

        self._ticks += 1
        self._last_tick = now_iso()
        self._last_error = None
        with contextlib.suppress(Exception):
            self.db.kv_set(HEARTBEAT_KEY, {"ts": self._last_tick, "tick": self._ticks,
                                           "owner": self._owner,
                                           "last_error": self._last_error})
        self.bus.publish("runtime.tick", tick=self._ticks,
                         jobs=fired, missed=missed, events=dispatched, goals=goals_started)
        return {"enabled": True, "owner": True, "jobs": fired,
                "missed": missed, "events": dispatched, "goals": goals_started}

    async def _guarded_fire(self, fire: dict[str, Any]) -> None:
        sem = self._sem or asyncio.Semaphore(1)
        async with sem:
            try:
                await self.events.execute_fire(fire)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                log.exception("event reaction failed")

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for reactions and scheduled runs started by a one-shot tick."""
        pending = set(self._fires)
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        await self.scheduler.wait_idle(timeout)

    # -- lease -----------------------------------------------------------
    def _lease(self) -> dict[str, Any]:
        value = self.db.kv_get(LEASE_KEY, {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _expires(lease: dict[str, Any]) -> float:
        try:
            return float(lease.get("expires_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    def holds_lease(self) -> bool:
        lease = self._lease()
        return lease.get("owner") == self._owner and self._expires(lease) > time.time()

    def _acquire_lease(self) -> bool:
        now = time.time()
        lease = self._lease()
        owner = lease.get("owner")
        if owner in (None, self._owner) or self._expires(lease) <= now:
            self.db.kv_set(LEASE_KEY, {
                "owner": self._owner,
                "expires_at": now + float(self.rb.lease_ttl_s),
                "pid": os.getpid(),
                "host": socket.gethostname(),
            })
            self._is_owner = True
            return True
        self._is_owner = False
        return False

    def _release_lease(self) -> None:
        if self._lease().get("owner") == self._owner:
            with contextlib.suppress(Exception):
                self.db.kv_set(LEASE_KEY, {"owner": self._owner, "expires_at": 0})

    # -- bus mirroring ---------------------------------------------------
    def _install_taps(self) -> None:
        topics = [str(t).strip() for t in (self.rb.event_topics or []) if str(t).strip()]
        if not topics:
            return

        def tap(event: Any) -> None:
            # A standby must not accumulate events it is not responsible for.
            if not self._ready or not self._is_owner:
                return
            for pattern in topics:
                if fnmatch.fnmatchcase(event.topic, pattern):
                    try:
                        self.events.ingest(event.topic, dict(event.data or {}), source="bus")
                    except Exception:  # pragma: no cover - observation must not break the bus
                        log.warning("event mirror failed for topic %s", event.topic)
                    return

        self.bus.tap(tap)
        self._ready = True

    # -- reporting -------------------------------------------------------
    def health(self) -> dict[str, Any]:
        if not self.rb.enabled:
            return {"available": True, "reason": "runtime disabled by config (not a fault)"}
        if not self.holds_lease():
            lease = self._lease()
            if lease and self._expires(lease) <= time.time():
                return {"available": True,
                        "reason": (f"standby: the last lease ({lease.get('owner')}) "
                                   f"expired; the next tick takes ownership")}
            return {"available": True,
                    "reason": f"standby: another Core owns the loop "
                              f"({lease.get('owner')})"}
        running = bool(self._loop_task and not self._loop_task.done())
        if not running:
            return {"available": False, "reason": "runtime loop is not running"}
        return {"available": True,
                "reason": f"running: tick {self._ticks} at {self._last_tick}, "
                          f"owner {self._owner}"}

    def status(self) -> dict[str, Any]:
        lease = self._lease()
        expires = self._expires(lease)
        lease_valid = bool(lease) and expires > time.time()
        sessions = (self.kernel.services or {}).get("sessions")
        return {
            "enabled": bool(self.rb.enabled),
            "running": bool(self._loop_task and not self._loop_task.done()),
            "owner": self.holds_lease(),
            "owner_id": self._owner,
            "lease": lease,
            # The raw lease may be hours stale after a crash. Report that
            # explicitly instead of letting "lease" imply a live owner.
            "lease_valid": lease_valid,
            "lease_expired": bool(lease) and not lease_valid,
            "started_at": self._started_at,
            "ticks": self._ticks,
            "last_tick": self._last_tick,
            "last_error": self._last_error,
            "tick_s": float(self.rb.tick_s),
            "catch_up_s": float(self.rb.catch_up_s),
            "in_flight": {"reactions": len(self._fires),
                          "scheduled": len(self.scheduler._tasks)},
            "scheduler": self.scheduler.status(),
            "events": self.events.status(),
            "sessions": sessions.count() if sessions is not None else 0,
        }
