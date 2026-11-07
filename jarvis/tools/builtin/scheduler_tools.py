"""Schedule storage: ``scheduler.add`` / ``scheduler.list`` / ``scheduler.remove``.

These tools are *storage only*. They record a job (id, when, prompt, enabled) in
the ``scheduled_jobs`` kv list; a separate scheduler service is responsible for
firing due jobs. Keeping the tools inert means a model can propose a schedule
without also being the thing that triggers it, which keeps the effect surface
auditable.

``when`` accepts either an ISO-8601 timestamp or a daily ``HH:MM`` /
``HH:MM:SS`` wall-clock time.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...ids import new_id, now_iso
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.scheduler")

__all__ = ["SchedulerAddTool", "SchedulerListTool", "SchedulerRemoveTool", "JOBS_KEY"]

JOBS_KEY = "scheduled_jobs"

_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")


def _valid_when(when: str) -> bool:
    if _HHMM.match(when):
        return True
    try:
        datetime.fromisoformat(when)
        return True
    except ValueError:
        return False


def _load_jobs(ctx: ToolContext) -> list[dict[str, Any]]:
    db = getattr(ctx, "db", None)
    if db is None:
        return []
    try:
        raw = db.kv_get(JOBS_KEY, [])
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("scheduled_jobs lookup failed: %s", exc)
        return []
    if not isinstance(raw, list):
        log.warning("scheduled_jobs is not a list; ignoring")
        return []
    return [j for j in raw if isinstance(j, dict)]


def _save_jobs(ctx: ToolContext, jobs: list[dict[str, Any]]) -> None:
    ctx.db.kv_set(JOBS_KEY, jobs)


@register(["scheduler.add", "schedule.add"])
class SchedulerAddTool(BaseTool):
    spec = ToolSpec(
        name="scheduler.add",
        summary="Register a scheduled job (stored only; a service fires it).",
        version="1.0",
        category="automation",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("when", "string",
                      "ISO-8601 timestamp or a daily 'HH:MM' / 'HH:MM:SS' time.",
                      required=True),
            ToolParam("prompt", "string", "Request to run when the job fires.",
                      required=True),
            ToolParam("enabled", "boolean", "Whether the job is active.", default=True),
        ),
        returns="object: {job, count}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            when = str(kwargs.get("when") or "").strip()
            if not when:
                return ToolResult(ok=False, error="when is required")
            if not _valid_when(when):
                return ToolResult(
                    ok=False,
                    error=("when must be an ISO-8601 timestamp or a daily "
                           "'HH:MM' / 'HH:MM:SS' time"),
                )
            prompt = str(kwargs.get("prompt") or "").strip()
            if not prompt:
                return ToolResult(ok=False, error="prompt is required")
            enabled = kwargs.get("enabled")
            enabled = True if enabled is None else bool(enabled)

            job = {
                "id": new_id("job"),
                "when": when,
                "prompt": prompt,
                "enabled": enabled,
                "created_at": now_iso(),
            }
            jobs = _load_jobs(ctx)
            jobs.append(job)
            _save_jobs(ctx, jobs)
            log.info("scheduled job %s at %s", job["id"], when)
            return ToolResult(
                ok=True,
                data={"job": job, "count": len(jobs)},
                evidence={"method": "kv-write", "key": JOBS_KEY, "id": job["id"]},
                certainty="settled",
            )
        except Exception as exc:  # never raise out of run()
            log.exception("scheduler.add failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")

    def verify(self, ctx: ToolContext, result: ToolResult):
        """Confirm the job id is durably present in the kv list."""
        if not result.ok or not isinstance(result.data, dict):
            return None
        job = result.data.get("job") or {}
        job_id = job.get("id")
        if not job_id:
            return None
        present = any(j.get("id") == job_id for j in _load_jobs(ctx))
        return {"verified": present, "method": "kv-read-back", "id": job_id}


@register(["scheduler.list", "schedule.list"])
class SchedulerListTool(BaseTool):
    spec = ToolSpec(
        name="scheduler.list",
        summary="List registered scheduled jobs.",
        version="1.0",
        category="automation",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object: {jobs, count}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            jobs = _load_jobs(ctx)
            return ToolResult(
                ok=True,
                data={"jobs": jobs, "count": len(jobs)},
                evidence={"method": "kv", "key": JOBS_KEY},
            )
        except Exception as exc:  # never raise out of run()
            log.exception("scheduler.list failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["scheduler.remove", "schedule.remove"])
class SchedulerRemoveTool(BaseTool):
    spec = ToolSpec(
        name="scheduler.remove",
        summary="Delete a scheduled job by id.",
        version="1.0",
        category="automation",
        side_effect=SideEffect.DESTRUCTIVE,
        params=(
            ToolParam("id", "string", "Id of the job to remove.", required=True),
        ),
        returns="object: {removed, count}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            job_id = str(kwargs.get("id") or "").strip()
            if not job_id:
                return ToolResult(ok=False, error="id is required")
            jobs = _load_jobs(ctx)
            kept = [j for j in jobs if j.get("id") != job_id]
            if len(kept) == len(jobs):
                return ToolResult(ok=False, error=f"no scheduled job with id '{job_id}'")
            removed = next((j for j in jobs if j.get("id") == job_id), None)
            _save_jobs(ctx, kept)
            log.info("removed scheduled job %s", job_id)
            return ToolResult(
                ok=True,
                data={"removed": removed, "count": len(kept)},
                evidence={"method": "kv-write", "key": JOBS_KEY, "id": job_id},
                certainty="settled",
            )
        except Exception as exc:  # never raise out of run()
            log.exception("scheduler.remove failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")

    def verify(self, ctx: ToolContext, result: ToolResult):
        """Confirm the job id is gone from the kv list."""
        if not result.ok or not isinstance(result.data, dict):
            return None
        removed = result.data.get("removed") or {}
        job_id = removed.get("id")
        if not job_id:
            return None
        absent = all(j.get("id") != job_id for j in _load_jobs(ctx))
        return {"verified": absent, "method": "kv-read-back", "id": job_id}
