"""``sandbox.status`` - report how child processes are contained, honestly.

Read-only. It answers the question the model (and the owner) should be able to
ask at any time: *is anything I run actually sandboxed, and what is not?* The
answer comes straight from :class:`jarvis.sandbox.SandboxService`; nothing here
is inferred or rounded up.
"""

from __future__ import annotations

from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.sandbox")

__all__ = ["SandboxStatusTool", "health"]


def health() -> dict[str, Any]:
    return {"available": True, "reason": "reads the sandbox service; no external dependency"}


@register(["sandbox"])
class SandboxStatusTool(BaseTool):
    spec = ToolSpec(
        name="sandbox.status",
        summary=("Report how child processes are contained: the active tier, resource "
                 "limits, environment scrubbing, and what is NOT isolated (filesystem, network)."),
        version="1.0",
        category="system",
        side_effect=SideEffect.NONE,
        returns=("object: {active_tier, contained, env_scrubbed, limits, tiers, isolation, notes}"),
        cheap=True,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            svc = ctx.service("sandbox") if ctx is not None else None
            if svc is None:
                from ...sandbox import SandboxService
                svc = SandboxService(config=getattr(ctx, "config", None))
            return ToolResult(ok=True, data=svc.status(),
                              evidence={"method": "service-introspection"})
        except Exception as exc:  # never raise out of run()
            log.exception("sandbox.status failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")
