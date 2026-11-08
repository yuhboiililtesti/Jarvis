"""EVOLVE — the JARVIS service layer.

Exposes the engine to JARVIS as a callable, bounded, audited subsystem. Runs are
CPU-bound and executed off the event loop via ``asyncio.to_thread``; results are
persisted (``kv``) and audited, and the last run is published on the bus so the
HUD/other agents can observe it.

This is the *untrusted-sandbox* side of EVOLVE: the engine may evolve anything,
but the service never changes JARVIS's own policy, tools, or verification layer.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..logging_setup import get_logger
from .engine import EvolutionEngine, RunConfig, RunResult
from .genome import expression_spec, numeric_spec
from .meta import self_tune
from .objectives import rastrigin, sphere, symbolic_regression

log = get_logger("jarvis.evolve")

__all__ = ["EvolveService"]

_BENCHMARKS = {"sphere": sphere, "rastrigin": rastrigin}

_LAST_KEY = "evolve.last"


class EvolveService:
    """Evolve subsystem: runs and records evolutionary experiments."""

    def __init__(self, jarvis: Any = None) -> None:
        self.j = jarvis
        self.db = getattr(jarvis, "db", None)
        self.bus = getattr(jarvis, "bus", None)

    # -- helpers ----------------------------------------------------------
    def _record(self, result: RunResult, kind: str) -> dict[str, Any]:
        summary = {"kind": kind, "result": result.to_dict()}
        if self.db is not None:
            try:
                self.db.kv_set(_LAST_KEY, summary)
                self.db.audit("evolve.run", actor="evolve", target=kind,
                              detail={"best_fitness": result.best.fitness,
                                      "generations": result.generations_run})
            except Exception as exc:  # pragma: no cover - recording must not break the run
                log.warning("evolve record failed: %s", exc)
        if self.bus is not None:
            try:
                self.bus.publish("evolve.completed", kind=kind,
                                 best_fitness=result.best.fitness,
                                 generations=result.generations_run)
            except Exception as exc:  # pragma: no cover
                log.debug("evolve bus publish failed: %s", exc)
        return summary

    # -- public operations ------------------------------------------------
    async def symbolic_regression(self, xs: list[float], ys: list[float], *,
                                  generations: int = 50, population: int = 100,
                                  seed: int = 0, max_seconds: float = 30.0) -> dict[str, Any]:
        """Evolve an expression ``f(x)`` matching the given sample points."""
        if len(xs) != len(ys) or not xs:
            return {"ok": False, "error": "xs and ys must be matching, non-empty lists"}
        if generations > 500 or population > 2000:
            return {"ok": False, "error": "budget too large (max 500 gens / 2000 pop)"}

        def _run() -> RunResult:
            fitness = symbolic_regression(xs, ys)
            cfg = RunConfig(population_size=population, generations=generations,
                            mutation_rate=0.3, crossover_rate=0.85, elitism=2,
                            tournament_size=3, selection="tournament", seed=seed,
                            max_seconds=max_seconds, stagnation_generations=20)
            return EvolutionEngine(expression_spec, fitness, cfg).run()

        result = await asyncio.to_thread(_run)
        return self._record(result, "symbolic_regression")

    async def minimize(self, function: str, *, dimensions: int = 4,
                       bounds: tuple[float, float] = (-5.0, 5.0),
                       generations: int = 60, population: int = 60,
                       seed: int = 0) -> dict[str, Any]:
        """Minimise a named numeric benchmark (``sphere`` or ``rastrigin``)."""
        fn = _BENCHMARKS.get(function)
        if fn is None:
            return {"ok": False, "error": f"unknown function {function!r} (sphere|rastrigin)"}
        if not 1 <= dimensions <= 50:
            return {"ok": False, "error": "dimensions must be 1..50"}
        spec = numeric_spec([bounds for _ in range(dimensions)])

        def _run() -> RunResult:
            cfg = RunConfig(population_size=population, generations=generations,
                            mutation_rate=0.3, crossover_rate=0.8, elitism=2,
                            tournament_size=3, seed=seed, max_seconds=60.0,
                            stagnation_generations=20)
            return EvolutionEngine(spec, fn, cfg).run()

        result = await asyncio.to_thread(_run)
        return self._record(result, f"minimize:{function}")

    async def evolve_itself(self, *, task: str = "sphere", dimensions: int = 3,
                            meta_generations: int = 10, max_seconds: float = 180.0
                            ) -> dict[str, Any]:
        """Evolve the engine's own configuration on a fixed benchmark task."""
        fn = _BENCHMARKS.get(task)
        if fn is None:
            return {"ok": False, "error": f"unknown task {task!r}"}
        spec = numeric_spec([(-5.0, 5.0) for _ in range(max(1, dimensions))])

        def _run() -> RunResult:
            return self_tune(spec, fn, meta_generations=meta_generations,
                             task_generations=20, task_population=30,
                             eval_seeds=(0, 1), max_seconds=max_seconds)

        result = await asyncio.to_thread(_run)
        return self._record(result, "self_tune")

    def status(self) -> dict[str, Any]:
        """The last run's summary, or an empty report if none has run."""
        if self.db is None:
            return {"available": False, "reason": "no database"}
        try:
            data = self.db.kv_get(_LAST_KEY, None)
        except Exception:  # pragma: no cover
            return {"available": False, "reason": "unreadable"}
        if not data:
            return {"available": True, "last": None}
        return {"available": True, "last": json.loads(json.dumps(data, default=str))}
