"""Due-job scheduler: the service that actually fires ``scheduler.*`` jobs.

The tools only *record* a job (that separation is deliberate: a model can propose
a schedule without being the thing that triggers it). This is the missing half —
the part that used to be claimed but did not exist.

Correctness properties, each tested:

  * **At-most-once per occurrence.** A run row is claimed with
    ``INSERT OR IGNORE`` against ``UNIQUE(job_id, due_key)`` *before* anything
    happens, so a crash, a restart or a second Core cannot fire the same
    occurrence twice.
  * **No silent replay.** A claim left ``pending``/``running`` by a crash is
    reconciled to ``outcome-unknown`` and never re-run — the same rule the task
    journal uses for effects.
  * **Bounded lateness.** A job more than ``runtime.catch_up_s`` late is recorded
    ``missed`` instead of surprising the owner hours afterwards.

The job definition stays where the tools put it (the ``scheduled_jobs`` kv list);
this module adds the durable execution record around it.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Any

from ..ids import new_id, now_iso
from ..logging_setup import get_logger

log = get_logger("jarvis.runtime.scheduler")

__all__ = ["Scheduler", "JOBS_KEY", "parse_when", "due_occurrence", "next_occurrence"]

JOBS_KEY = "scheduled_jobs"

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")


def _naive(dt: datetime) -> datetime:
    """Local wall-clock, naive — the same frame ``datetime.now()`` uses."""
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt


def parse_when(when: Any) -> tuple[str | None, Any]:
    """``("daily", (h, m, s))``, ``("once", datetime)`` or ``(None, None)``."""
    text = str(when or "").strip()
    match = _HHMM.match(text)
    if match:
        return "daily", (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
    try:
        return "once", _naive(datetime.fromisoformat(text))
    except ValueError:
        return None, None


def due_occurrence(when: Any, now: datetime,
                   lookback_s: float | None = None) -> tuple[datetime, str] | None:
    """The occurrence that is due at ``now``, with the key that identifies it.

    For a daily time, today's occurrence is used when it has passed. If it has
    not, ``lookback_s`` also considers *yesterday's* occurrence, so a job at
    23:59 is not silently skipped when the tick (or a restart) lands at 00:00:30.
    """
    kind, value = parse_when(when)
    if kind == "daily":
        hour, minute, second = value
        occ = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if occ <= now:
            return occ, occ.strftime("%Y-%m-%dT%H:%M:%S")
        if lookback_s is not None:
            previous = occ - timedelta(days=1)
            if (now - previous).total_seconds() <= float(lookback_s):
                return previous, previous.strftime("%Y-%m-%dT%H:%M:%S")
        return None
    if kind == "once" and value <= now:
        return value, value.isoformat(timespec="seconds")
    return None


def next_occurrence(when: Any, now: datetime) -> datetime | None:
    kind, value = parse_when(when)
    if kind == "daily":
        hour, minute, second = value
        occ = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        return occ if occ > now else occ + timedelta(days=1)
    if kind == "once":
        return value if value > now else None
    return None


class Scheduler:
    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self.db = kernel.db
        self.config = kernel.config
        self._tasks: set[asyncio.Task] = set()

    # -- definitions -----------------------------------------------------
    def jobs(self) -> list[dict[str, Any]]:
        raw = self.db.kv_get(JOBS_KEY, [])
        if not isinstance(raw, list):
            return []
        return [j for j in raw if isinstance(j, dict) and j.get("id")]

    # -- firing ----------------------------------------------------------
    async def fire_due(self, limit: int | None = None,
                       now: datetime | None = None) -> list[dict[str, Any]]:
        """Claim every due occurrence (bounded); schedule the runs in the background."""
        now = now or datetime.now()
        # Bound *running* turns, not just claims per tick. Otherwise fifty jobs due
        # at 07:00 all start at once.
        in_flight = max(1, int(self.config.runtime.max_jobs_in_flight))
        if len(self._tasks) >= in_flight:
            return []
        limit = max(1, min(int(limit or self.config.runtime.max_jobs_per_tick),
                           in_flight - len(self._tasks)))
        catch_up = float(self.config.runtime.catch_up_s)
        claims: list[dict[str, Any]] = []
        for job in self.jobs():
            if not job.get("enabled", True):
                continue
            # One run per job at a time: a slow turn must not overlap its next
            # occurrence (or share its session with itself).
            busy = self.db.query_one(
                "SELECT 1 FROM scheduler_runs WHERE job_id=? AND status='running' LIMIT 1",
                (job["id"],))
            if busy is not None:
                continue
            occurrence = due_occurrence(job.get("when"), now, lookback_s=catch_up)
            if occurrence is None:
                continue
            due_at, due_key = occurrence
            late_s = (now - due_at).total_seconds()
            missed = late_s > catch_up
            claim = self._claim(job, due_key, missed, late_s)
            if claim is None:          # already handled at some point
                continue
            claims.append(claim)
            if not missed:
                self._spawn(claim)
            if len(claims) >= limit:
                break
        return claims

    def _claim(self, job: dict[str, Any], due_key: str, missed: bool,
               late_s: float) -> dict[str, Any] | None:
        run_id = new_id("run")
        status = "missed" if missed else "pending"
        detail = (f"missed by {late_s:.0f}s (catch-up limit "
                  f"{float(self.config.runtime.catch_up_s):.0f}s)") if missed else None
        cur = self.db.execute(
            "INSERT OR IGNORE INTO scheduler_runs"
            "(id,job_id,due_key,status,ended_at,error) VALUES(?,?,?,?,?,?)",
            (run_id, job["id"], due_key, status, now_iso() if missed else None, detail))
        if cur.rowcount == 0:
            return None
        self.db.audit("scheduler.claim", target=job["id"],
                      detail={"due": due_key, "missed": missed},
                      severity="warn" if missed else "info")
        return {"run_id": run_id, "job_id": job["id"], "prompt": job.get("prompt", ""),
                "due_key": due_key, "missed": missed, "error": detail}

    def _spawn(self, claim: dict[str, Any]) -> None:
        task = asyncio.create_task(self._guarded(claim),
                                   name=f"schedule:{claim['job_id']}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, claim: dict[str, Any]) -> None:
        try:
            await self.execute_claim(claim)
        except asyncio.CancelledError:
            self._finish(claim["run_id"], "outcome-unknown", "cancelled at shutdown")
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("scheduled run failed")
            self._finish(claim["run_id"], "failed", f"{type(exc).__name__}: {exc}")

    async def execute_claim(self, claim: dict[str, Any]) -> dict[str, Any]:
        """Run one claimed occurrence. Public so tests can await it deterministically."""
        run_id = claim["run_id"]
        self.db.execute("UPDATE scheduler_runs SET status='running', started_at=? WHERE id=?",
                        (now_iso(), run_id))
        agent = getattr(self.kernel, "agent", None)
        if agent is None:
            self._finish(run_id, "failed", "no agent configured")
            return {"ok": False, "run_id": run_id, "error": "no agent configured"}

        session_id = f"scheduler:{claim['job_id']}"
        session = self.kernel.session(session_id)
        task_id: str | None = None
        final = ""
        final_status = "succeeded"
        try:
            async for ev in agent.run_turn(claim["prompt"], session=session,
                                           source="schedule"):
                kind = ev.get("kind")
                if kind == "task":
                    task_id = ev.get("task_id")
                    self.db.execute("UPDATE scheduler_runs SET task_id=? WHERE id=?",
                                    (task_id, run_id))
                elif kind == "final":
                    final = ev.get("text") or ""
                    final_status = ev.get("status") or "succeeded"
        except Exception as exc:
            self._finish(run_id, "failed", f"{type(exc).__name__}: {exc}")
            return {"ok": False, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}

        if final_status != "succeeded":
            self._finish(run_id, "failed", f"the turn ended '{final_status}'; see the task journal")
            return {"ok": False, "run_id": run_id, "task_id": task_id,
                    "error": "turn failed"}
        self._finish(run_id, "succeeded", None)
        self.db.audit("scheduler.fire", target=claim["job_id"],
                      detail={"run": run_id, "task": task_id})
        return {"ok": True, "run_id": run_id, "task_id": task_id, "text": final[:400]}

    def _finish(self, run_id: str, status: str, error: str | None) -> None:
        self.db.execute("UPDATE scheduler_runs SET status=?, ended_at=?, error=? WHERE id=?",
                        (status, now_iso(), error, run_id))
        if status != "succeeded":
            self.db.audit("scheduler.run_failed", target=run_id,
                          detail={"status": status, "error": error}, severity="warn")

    # -- lifecycle -------------------------------------------------------
    async def wait_idle(self, timeout: float = 30.0) -> None:
        pending = set(self._tasks)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    def reconcile(self) -> int:
        """Settle claims interrupted by a crash; never retry them."""
        cur = self.db.execute(
            "UPDATE scheduler_runs SET status='outcome-unknown', ended_at=?, "
            "error=COALESCE(error,'interrupted; not retried because it may have run') "
            "WHERE status IN ('pending','running')", (now_iso(),))
        count = int(cur.rowcount or 0)
        if count:
            self.db.audit("scheduler.reconcile", detail={"count": count}, severity="warn")
        return count

    # -- reporting -------------------------------------------------------
    def status(self, limit: int = 10) -> dict[str, Any]:
        now = datetime.now()
        jobs = []
        for job in self.jobs():
            kind, _ = parse_when(job.get("when"))
            nxt = next_occurrence(job.get("when"), now)
            jobs.append({
                "id": job["id"], "when": job.get("when"), "kind": kind,
                "enabled": bool(job.get("enabled", True)),
                "prompt": str(job.get("prompt", ""))[:160],
                "next_run": nxt.isoformat(timespec="seconds") if nxt else None,
            })
        runs = [dict(r) for r in self.db.query(
            "SELECT id,job_id,due_key,status,task_id,started_at,ended_at,error "
            "FROM scheduler_runs ORDER BY rowid DESC LIMIT ?", (min(max(limit, 1), 100),))]
        counts: dict[str, int] = {}
        for row in self.db.query("SELECT status, COUNT(*) AS n FROM scheduler_runs GROUP BY status"):
            counts[row["status"]] = int(row["n"])
        return {"jobs": jobs, "count": len(jobs), "runs": runs, "run_counts": counts,
                "catch_up_s": float(self.config.runtime.catch_up_s)}
