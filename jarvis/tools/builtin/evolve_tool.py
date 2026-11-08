"""EVOLVE tools — run evolutionary experiments from JARVIS.

Thin wrappers over :class:`jarvis.evolve.EvolveService`, fetched via
``ctx.service("evolve")``. Every run is bounded (generation/population budgets),
deterministic under a seed, and recorded + audited by the service.
"""

from __future__ import annotations

from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.evolve")

__all__ = ["EvolveSymbolicTool", "EvolveMinimizeTool", "EvolveSelfTuneTool", "EvolveStatusTool"]


def _svc(ctx: ToolContext):
    return ctx.service("evolve")


def _missing() -> ToolResult:
    return ToolResult(ok=False, error="evolve service is unavailable (not wired by the kernel)")


def _numbers(v: Any, label: str) -> list[float] | None:
    if not isinstance(v, (list, tuple)) or not v:
        return None
    try:
        return [float(x) for x in v]
    except (TypeError, ValueError):
        return None


@register(["evolve.symbolic", "evolve.fit"])
class EvolveSymbolicTool(BaseTool):
    spec = ToolSpec(
        name="evolve.symbolic",
        summary="Evolve a symbolic expression f(x) matching given sample points (symbolic regression).",
        category="evolve",
        side_effect=SideEffect.NONE,   # pure computation, no external effect
        params=(
            ToolParam("xs", "array", "X sample points.", required=True),
            ToolParam("ys", "array", "Y sample points (same length as xs).", required=True),
            ToolParam("generations", "integer", "Evolution generations (max 500).", default=50),
            ToolParam("population", "integer", "Population size (max 2000).", default=100),
        ),
        returns="object with the best expression (as a string) and its mean-squared error",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        xs = _numbers(kwargs.get("xs"), "xs")
        ys = _numbers(kwargs.get("ys"), "ys")
        if xs is None or ys is None or len(xs) != len(ys):
            return ToolResult(ok=False, error="xs and ys must be matching non-empty numeric lists")
        res = await svc.symbolic_regression(
            xs, ys, generations=int(kwargs.get("generations", 50)),
            population=int(kwargs.get("population", 100)))
        if "ok" in res and res["ok"] is False:
            return ToolResult(ok=False, error=res["error"])
        best = res["result"]["best"]
        from ..evolve.genome import expression_str
        return ToolResult(ok=True, data={
            "expression": expression_str(best["genome"]),
            "mean_squared_error": best["fitness"],
            "generations": res["result"]["generations_run"],
        })


@register(["evolve.minimize", "evolve.optimize"])
class EvolveMinimizeTool(BaseTool):
    spec = ToolSpec(
        name="evolve.minimize",
        summary="Minimize a numeric benchmark function (sphere or rastrigin) via evolution.",
        category="evolve",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("function", "string", "Benchmark function.", default="sphere",
                      enum=("sphere", "rastrigin")),
            ToolParam("dimensions", "integer", "Number of dimensions (1..50).", default=4),
            ToolParam("generations", "integer", "Evolution generations.", default=60),
        ),
        returns="object with the best vector and its cost",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        res = await svc.minimize(
            str(kwargs.get("function", "sphere")),
            dimensions=int(kwargs.get("dimensions", 4)),
            generations=int(kwargs.get("generations", 60)))
        if "ok" in res and res["ok"] is False:
            return ToolResult(ok=False, error=res["error"])
        best = res["result"]["best"]
        return ToolResult(ok=True, data={
            "function": str(kwargs.get("function", "sphere")),
            "best_vector": best["genome"],
            "best_cost": best["fitness"],
            "generations": res["result"]["generations_run"],
        })


@register(["evolve.selftune", "evolve.self_tune"])
class EvolveSelfTuneTool(BaseTool):
    spec = ToolSpec(
        name="evolve.selftune",
        summary="Evolve the engine's own configuration on a benchmark task (bounded self-improvement).",
        category="evolve",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("task", "string", "Benchmark task to tune against.", default="sphere",
                      enum=("sphere", "rastrigin")),
            ToolParam("meta_generations", "integer", "Meta-evolution generations.", default=10),
        ),
        returns="object with the winning engine configuration and its cost",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        res = await svc.evolve_itself(
            task=str(kwargs.get("task", "sphere")),
            meta_generations=int(kwargs.get("meta_generations", 10)))
        if "ok" in res and res["ok"] is False:
            return ToolResult(ok=False, error=res["error"])
        best = res["result"]["best"]
        return ToolResult(ok=True, data={
            "task": str(kwargs.get("task", "sphere")),
            "best_config": best["genome"],
            "best_cost": best["fitness"],
            "meta_generations": res["result"]["generations_run"],
        })


@register(["evolve.status"])
class EvolveStatusTool(BaseTool):
    spec = ToolSpec(
        name="evolve.status",
        summary="Report the last evolutionary experiment's result.",
        category="evolve",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object with the last run summary (or null if none)",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        return ToolResult(ok=True, data=svc.status())
