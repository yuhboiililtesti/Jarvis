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
        from ..evolve import expression_str
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


@register(["evolve.tune", "evolve.tune_ollama"])
class EvolveTuneTool(BaseTool):
    spec = ToolSpec(
        name="evolve.tune",
        summary="Tune real server settings by measuring them (Ollama inference latency).",
        category="evolve",
        side_effect=SideEffect.NONE,   # measurement only — nothing is written
        params=(
            ToolParam("target", "string", "Target to tune.", default="ollama",
                      enum=("ollama",)),
            ToolParam("generations", "integer", "Generations (small — each is a real call).", default=8),
            ToolParam("population", "integer", "Population size.", default=10),
        ),
        returns="object with the best settings and their measured latency",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        target = str(kwargs.get("target", "ollama"))
        if target != "ollama":
            return ToolResult(ok=False, error=f"unsupported target {target!r} (only 'ollama')")
        res = await svc.tune_ollama(
            generations=int(kwargs.get("generations", 8)),
            population=int(kwargs.get("population", 10)))
        if "ok" in res and res["ok"] is False:
            return ToolResult(ok=False, error=res["error"])
        best = res["result"]["best"]
        return ToolResult(ok=True, data={
            "target": target,
            "best_settings": best["genome"],
            "measured_seconds": best["fitness"],
            "generations": res["result"]["generations_run"],
        })


@register(["evolve.tune_command", "evolve.benchmark_tune"])
class EvolveTuneCommandTool(BaseTool):
    spec = ToolSpec(
        name="evolve.tune_command",
        summary="Evolve one integer parameter ({level}) of a shell command, measuring wall time.",
        category="evolve",
        side_effect=SideEffect.LOCAL_EXEC,  # runs the command repeatedly — gated
        params=(
            ToolParam("command", "string", "Shell command with a {level} placeholder.", required=True),
            ToolParam("low", "integer", "Parameter lower bound.", required=True),
            ToolParam("high", "integer", "Parameter upper bound.", required=True),
            ToolParam("runs", "integer", "Measurements per candidate.", default=2),
            ToolParam("generations", "integer", "Generations.", default=15),
        ),
        returns="object with the best parameter value and its mean wall time",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        res = await svc.tune_command(
            str(kwargs.get("command")),
            low=int(kwargs.get("low")),
            high=int(kwargs.get("high")),
            runs=int(kwargs.get("runs", 2)),
            generations=int(kwargs.get("generations", 15)))
        if "ok" in res and res["ok"] is False:
            return ToolResult(ok=False, error=res["error"])
        best = res["result"]["best"]
        return ToolResult(ok=True, data={
            "best_parameter": best["genome"].get("level"),
            "mean_seconds": best["fitness"],
            "generations": res["result"]["generations_run"],
        })


@register(["evolve.auto"])
class EvolveAutoTool(BaseTool):
    spec = ToolSpec(
        name="evolve.auto",
        summary="Run the bounded automatic EVOLVE experiment queue and return its report.",
        category="evolve",
        side_effect=SideEffect.NONE,   # pure computation, bounded by a time budget
        params=(
            ToolParam("minutes", "integer", "Time budget in minutes.", default=5),
        ),
        returns="object with the experiment report",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        report = await svc.auto_run(minutes=float(kwargs.get("minutes", 5)))
        return ToolResult(ok=True, data=report)


@register(["evolve.self", "evolve.evolve_agent"])
class EvolveSelfTool(BaseTool):
    spec = ToolSpec(
        name="evolve.self",
        summary="Evolve the agent's own settings (persona + LLM knobs) against its held-out eval score.",
        category="evolve",
        side_effect=SideEffect.NONE,   # measurement; original settings are restored after
        params=(
            ToolParam("generations", "integer", "Evolution generations.", default=4),
            ToolParam("population", "integer", "Population size.", default=6),
        ),
        returns="object with the best settings, their eval score, and the tasks used",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        svc = _svc(ctx)
        if svc is None:
            return _missing()
        report = await svc.evolve_agent(
            generations=int(kwargs.get("generations", 4)),
            population=int(kwargs.get("population", 6)))
        if "ok" in report and report["ok"] is False:
            return ToolResult(ok=False, error=report["error"])
        return ToolResult(ok=True, data={
            "best_settings": report["best_settings"],
            "best_eval_score": report["best_score"],
            "tasks": report["tasks"],
            "generations": report["generations"],
            "settings_restored": report["settings_restored"],
        })
