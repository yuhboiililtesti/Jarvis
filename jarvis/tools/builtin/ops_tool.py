"""``system.diagnose`` - JARVIS's own health report, checked in the background.

Read-only: it returns the latest snapshot from ``OpsService``. It does not probe
or repair anything itself, so calling it cannot have a side effect.
"""

from __future__ import annotations

from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.ops")

__all__ = ["SystemDiagnoseTool", "health"]


def health() -> dict[str, Any]:
    return {"available": True,
            "reason": "reads the ops service snapshot; no external dependency"}


@register(["system.diagnose", "ops.status"])
class SystemDiagnoseTool(BaseTool):
    spec = ToolSpec(
        name="system.diagnose",
        summary=("Report JARVIS's own health: model provider, runtime, goals, audit chain, "
                 "sandbox, database, disk and enrolled nodes, with a repair hint for any failure."),
        version="1.0",
        category="system",
        side_effect=SideEffect.NONE,
        returns="object: {ts, ok, checks:[{name,ok,detail,repair}], repaired}",
        cheap=True,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            svc = ctx.service("ops") if ctx is not None else None
            if svc is None:
                return ToolResult(ok=False, error="ops service is not loaded")
            snapshot = svc.status()
            return ToolResult(ok=True, data=snapshot,
                              evidence={"method": "ops-snapshot",
                                        "ok": snapshot.get("ok")})
        except Exception as exc:  # never raise out of run()
            log.exception("system.diagnose failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")
