"""Example plugin tool. Same contract as a builtin: see jarvis/tools/base.py.

Rules that keep a plugin trustworthy:
  * declare the true side-effect class (policy gates on it);
  * never raise out of run(); return ToolResult(ok=False, error=...) instead;
  * state-changing tools accept ``dry_run`` and implement ``verify`` with a real
    read-back (see jarvis/tools/builtin/_common.py for the shared helpers);
  * expose a module-level ``health()`` so `jarvis tools --health` can report it.
"""

from __future__ import annotations

from typing import Any

from jarvis.tools import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec, register


def health() -> dict[str, Any]:
    return {"available": True, "reason": "no external dependency"}


@register()
class WordCountTool(BaseTool):
    spec = ToolSpec(
        name="template.word_count",
        summary="Count words and characters in a piece of text.",
        version="0.1.0",
        category="text",
        side_effect=SideEffect.NONE,
        params=(ToolParam("text", "string", "Text to measure.", required=True),),
        returns="object: {words, characters}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        text = str(kwargs.get("text") or "")
        return ToolResult(ok=True, data={"words": len(text.split()), "characters": len(text)},
                          evidence={"method": "computed"})
