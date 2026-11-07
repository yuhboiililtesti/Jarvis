"""Learning tools: explicit teaching and review of JARVIS's lessons.

Thin, policy-aware wrappers over the shared :class:`LearningService`, fetched
from the tool context via ``ctx.service("learning")``. When it is absent the
tool fails loudly rather than pretending to have learned anything.
"""

from __future__ import annotations

from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.learning")

__all__ = ["LearnTool", "LessonsTool", "ForgetLessonTool"]


def _svc(ctx: ToolContext):
    return ctx.service("learning")


def _missing() -> ToolResult:
    return ToolResult(
        ok=False,
        error="learning service is unavailable: ctx.service('learning') is not wired by the kernel",
    )


@register(["learn", "learning.learn", "teach"])
class LearnTool(BaseTool):
    spec = ToolSpec(
        name="learning.learn",
        summary="Teach JARVIS a durable lesson (a preference, rule, or correction).",
        category="learning",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("text", "string", "The lesson content. Required.", required=True),
            ToolParam("tags", "array", "Optional tags for retrieval.", default=None),
            ToolParam("confidence", "number", "Initial confidence 0-1.", default=0.9),
        ),
        returns="object with the stored lesson id",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        text = str(kwargs.get("text") or "").strip()
        if not text:
            return ToolResult(ok=False, error="text must be non-empty")
        try:
            confidence = float(kwargs.get("confidence", 0.9))
            if not 0 <= confidence <= 1:
                return ToolResult(ok=False, error="confidence must be 0-1")
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="confidence must be a number 0-1")
        tags = kwargs.get("tags")
        lesson_id = await svc.learn(text, user=ctx.user, tags=tags, confidence=confidence)
        return ToolResult(ok=True, data={"lesson_id": lesson_id})


@register(["lessons", "learning.list", "lessons.list"])
class LessonsTool(BaseTool):
    spec = ToolSpec(
        name="learning.list",
        summary="List JARVIS's recorded lessons (self-learned and owner-taught).",
        category="learning",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("limit", "integer", "Maximum lessons to return.", default=20),
        ),
        returns="array of lessons with id, text, source, confidence, tags",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        try:
            limit = int(kwargs.get("limit", 20))
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="limit must be an integer")
        lessons = svc.list_lessons(user=ctx.user, limit=max(1, min(limit, 100)))
        return ToolResult(ok=True, data={"lessons": lessons, "count": len(lessons)})


@register(["forget_lesson", "learning.forget", "unlearn"])
class ForgetLessonTool(BaseTool):
    spec = ToolSpec(
        name="learning.forget",
        summary="Forget a lesson by its id.",
        category="learning",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("lesson_id", "string", "The lesson id to forget.", required=True),
        ),
        returns="object with the id and forgotten flag",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        lesson_id = str(kwargs.get("lesson_id") or "").strip()
        if not lesson_id:
            return ToolResult(ok=False, error="lesson_id must be non-empty")
        forgotten = svc.forget_lesson(lesson_id)
        return ToolResult(ok=True, data={"lesson_id": lesson_id, "forgotten": forgotten})
