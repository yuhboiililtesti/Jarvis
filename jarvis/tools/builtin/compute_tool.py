"""``compute.solve`` / ``compute.list`` - thin adapters over the compute service.

The tools deliberately contain no math engine: they forward to the ``compute``
service registered on the kernel, whose contract is::

    async def solve(capability: str, **kwargs) -> dict
    def/async def list_capabilities() -> list

If that service is not present, the tools fail with a clear message rather than
inventing an answer - a model must never be told a symbolic/physical result was
computed when it was not.
"""

from __future__ import annotations

import inspect
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.compute")

__all__ = ["ComputeSolveTool", "ComputeListTool"]

_SERVICE = "compute"
_MISSING = ("compute service is unavailable: no 'compute' service is registered "
            "on the kernel")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _service(ctx: ToolContext) -> Any:
    getter = getattr(ctx, "service", None)
    return getter(_SERVICE) if callable(getter) else None


@register(["compute.solve", "solve"])
class ComputeSolveTool(BaseTool):
    spec = ToolSpec(
        name="compute.solve",
        summary="Ask the compute service to solve a capability (math, units, physics...).",
        version="1.0",
        category="compute",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("capability", "string",
                      "Capability name to invoke (see compute.list).", required=True),
            ToolParam("params", "object",
                      "Keyword arguments passed to the capability."),
        ),
        returns="object: {capability, result, service}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            svc = _service(ctx)
            if svc is None or not hasattr(svc, "solve"):
                return ToolResult(ok=False, error=_MISSING)

            capability = str(kwargs.get("capability") or "").strip()
            if not capability:
                return ToolResult(ok=False, error="capability is required")

            params = kwargs.get("params") or {}
            if not isinstance(params, dict):
                return ToolResult(ok=False, error="params must be an object")

            try:
                result = await _maybe_await(svc.solve(capability, **params))
            except TypeError as exc:
                return ToolResult(ok=False, error=f"invalid parameters for '{capability}': {exc}")
            except Exception as exc:
                return ToolResult(ok=False, error=f"compute.solve failed: {type(exc).__name__}: {exc}",
                                  certainty="unverified")

            return ToolResult(
                ok=True,
                data={"capability": capability, "result": result, "service": _SERVICE},
                evidence={"method": "compute-service", "capability": capability},
            )
        except Exception as exc:  # never raise out of run()
            log.exception("compute.solve failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["compute.list", "compute.capabilities"])
class ComputeListTool(BaseTool):
    spec = ToolSpec(
        name="compute.list",
        summary="List capabilities offered by the compute service.",
        version="1.0",
        category="compute",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object: {capabilities, count}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            svc = _service(ctx)
            if svc is None or not hasattr(svc, "list_capabilities"):
                return ToolResult(ok=False, error=_MISSING)
            try:
                capabilities = await _maybe_await(svc.list_capabilities())
            except Exception as exc:
                return ToolResult(ok=False,
                                  error=f"compute.list failed: {type(exc).__name__}: {exc}")
            if isinstance(capabilities, dict):
                capabilities = list(capabilities)
            capabilities = capabilities or []
            return ToolResult(
                ok=True,
                data={"capabilities": capabilities, "count": len(capabilities)},
                evidence={"method": "compute-service", "service": _SERVICE},
            )
        except Exception as exc:  # never raise out of run()
            log.exception("compute.list failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
