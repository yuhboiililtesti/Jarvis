"""Independent verifier (observer).

The verifier is deliberately separate from the tool that performed an effect.
A tool's own success response can never settle a task; only a read-back can.
Where a tool supplies a `verify` hook we use it; otherwise the outcome is
recorded as `unverified`, which is an honest, non-success state.
"""

from __future__ import annotations

from typing import Any

from ..logging_setup import get_logger
from ..tools.base import ToolContext, ToolResult

log = get_logger("jarvis.verifier")

__all__ = ["Verifier"]


class Verifier:
    def __init__(self, registry: Any, bus: Any | None = None) -> None:
        self.registry = registry
        self.bus = bus

    async def verify(self, tool_name: str, ctx: ToolContext,
                     result: ToolResult) -> dict[str, Any]:
        tool = self.registry.get(tool_name)
        if tool is None:
            return {"verified": False, "method": "none", "note": "tool missing"}
        if not result.ok:
            return {"verified": False, "method": "skipped", "note": "tool failed"}

        hook = getattr(tool, "verify", None)
        if hook is None:
            return {"verified": False, "method": "none",
                    "note": "no read-back defined; effect unconfirmed"}

        try:
            out = hook(ctx, result)
            if hasattr(out, "__await__"):
                out = await out
        except Exception as exc:
            log.warning("verification of %s raised: %s", tool_name, exc)
            return {"verified": False, "method": "error", "note": str(exc)}

        if out is None:
            # No independent read-back available.
            if tool.spec.side_effect.value == "none":
                return {"verified": True, "method": "read-only"}
            return {"verified": False, "method": "none",
                    "note": "effect not independently confirmed"}

        out.setdefault("verified", True)
        if self.bus:
            self.bus.publish("verify", tool=tool_name,
                             verified=bool(out.get("verified")), method=out.get("method"))
        return out
