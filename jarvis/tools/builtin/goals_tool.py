"""Goal tools: ask JARVIS to plan and execute a multi-step objective.

``goals.create`` stores a plan (or previews it with ``dry_run``). The runtime
advances it one step at a time, and every step goes through the normal policy,
approval and verification path.
"""

from __future__ import annotations

from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common

log = get_logger("jarvis.tools.goals")

__all__ = ["GoalCreateTool", "GoalListTool", "GoalStatusTool", "GoalCancelTool", "health"]


def health() -> dict[str, Any]:
    return {"available": True,
            "reason": "requires the goals service (built by default); no external dependency"}


def _service(ctx: ToolContext) -> Any:
    return ctx.service("goals") if ctx is not None else None


@register(["goal.create", "plan.create"])
class GoalCreateTool(BaseTool):
    spec = ToolSpec(
        name="goals.create",
        summary=("Plan a multi-step objective and run it in the background. Use for a task that "
                 "needs several actions (inspect -> decide -> act -> verify), not one tool call."),
        version="1.0",
        category="automation",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("request", "string", "The objective in the owner's words.", required=True),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: goal {id,status,steps_total,steps}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _service(ctx)
        if svc is None:
            return ToolResult(ok=False, error="goals service is not loaded")
        request = str(kwargs.get("request") or "").strip()
        if not request:
            return ToolResult(ok=False, error="request is required")
        try:
            if _common.dry_run_requested(kwargs):
                steps = await svc.plan(request)
                return _common.preview(
                    target=request, operation="goals.create",
                    effect=f"persist and execute a {len(steps)}-step plan",
                    reversible=True, rollback="goals.cancel <goal_id>",
                    steps=steps)
            steps = await svc.plan(request)
            goal = svc.create(request, user=getattr(ctx, "user", "owner"), steps=steps)
            return ToolResult(
                ok=True, data=goal,
                evidence={"method": "db", "goal_id": goal.get("id"),
                          "steps": goal.get("steps_total")})
        except Exception as exc:  # never raise out of run()
            log.exception("goals.create failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        goal_id = result.data.get("id")
        svc = _service(ctx)
        if not goal_id or svc is None:
            return None
        goal = svc.status(goal_id)
        ok = goal.get("status") != "missing" and int(goal.get("steps_total") or 0) > 0
        return {"verified": ok, "method": "db-readback", "goal_id": goal_id,
                "steps": goal.get("steps_total")}


@register(["goals.list", "goal.list"])
class GoalListTool(BaseTool):
    spec = ToolSpec(
        name="goals.list",
        summary="List recent goals and their status.",
        version="1.0", category="automation", side_effect=SideEffect.NONE,
        params=(ToolParam("limit", "integer", "Maximum goals.", default=20),),
        returns="object: {goals, count}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _service(ctx)
        if svc is None:
            return ToolResult(ok=False, error="goals service is not loaded")
        try:
            limit = max(1, min(100, int(kwargs.get("limit") or 20)))
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="limit must be an integer")
        goals = svc.list_goals(limit)
        return ToolResult(ok=True, data={"goals": goals, "count": len(goals)},
                          evidence={"method": "db"})


@register(["goals.status", "goal.status"])
class GoalStatusTool(BaseTool):
    spec = ToolSpec(
        name="goals.status",
        summary="Show one goal's plan, step statuses and result.",
        version="1.0", category="automation", side_effect=SideEffect.NONE,
        params=(ToolParam("goal_id", "string", "Goal id.", required=True),),
        returns="object: goal",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _service(ctx)
        if svc is None:
            return ToolResult(ok=False, error="goals service is not loaded")
        goal_id = str(kwargs.get("goal_id") or "").strip()
        if not goal_id:
            return ToolResult(ok=False, error="goal_id is required")
        goal = svc.status(goal_id)
        if goal.get("status") == "missing":
            return ToolResult(ok=False, error=f"no goal '{goal_id}'")
        return ToolResult(ok=True, data=goal, evidence={"method": "db"})


@register(["goals.cancel", "goal.cancel"])
class GoalCancelTool(BaseTool):
    spec = ToolSpec(
        name="goals.cancel",
        summary="Cancel a goal and its pending steps.",
        version="1.0", category="automation", side_effect=SideEffect.LOCAL_WRITE,
        params=(ToolParam("goal_id", "string", "Goal id.", required=True),),
        returns="object: {cancelled}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _service(ctx)
        if svc is None:
            return ToolResult(ok=False, error="goals service is not loaded")
        goal_id = str(kwargs.get("goal_id") or "").strip()
        if not goal_id:
            return ToolResult(ok=False, error="goal_id is required")
        ok = svc.cancel(goal_id)
        if not ok:
            return ToolResult(ok=False, error=f"goal '{goal_id}' is missing or already finished")
        return ToolResult(ok=True, data={"cancelled": True, "goal_id": goal_id},
                          evidence={"method": "db-readback", "goal_id": goal_id})
