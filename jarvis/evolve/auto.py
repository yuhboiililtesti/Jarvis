"""EVOLVE — automatic experiment runner (Night-Protocol style).

Runs a bounded queue of experiments (symbolic regression, numeric optimisation
on several benchmark functions, meta self-tuning), emits progress events, and
produces a report. Bounded by a wall-clock budget; every experiment is
deterministic under a seed and stops on its own stagnation/time limits.

CLI: ``python -m jarvis.evolve auto [--minutes 10] [--report path.json]``
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .engine import EvolutionEngine, RunConfig, RunResult
from .genome import expression_spec, expression_str, numeric_spec
from .meta import self_tune
from .objectives import (ackley, minimize, rastrigin, rosenbrock, sphere,
                         symbolic_regression)

__all__ = ["auto_run", "AutoRunner"]

Progress = Callable[[dict[str, Any]], None]


@dataclass
class AutoRunner:
    """Runs the standard EVOLVE experiment queue within a time budget."""

    minutes: float = 10.0
    on_progress: Progress | None = None
    report_path: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    def _emit(self, kind: str, **data: Any) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress({"kind": kind, **data})
            except Exception:  # pragma: no cover - reporting must not break runs
                pass

    def _remaining(self) -> float:
        return (self.started_at + self.minutes * 60.0) - time.time()

    # -- the standard experiment queue -----------------------------------
    def _queue(self) -> list[tuple[str, Callable[[float], RunResult]]]:
        def symbolic(name: str, xs: list[float], ys: list[float], gens: int,
                     pop: int) -> tuple[str, Callable[[float], RunResult]]:
            fitness = symbolic_regression(xs, ys)

            def run(budget: float) -> RunResult:
                cfg = RunConfig(population_size=pop, generations=gens,
                                mutation_rate=0.35, crossover_rate=0.9, elitism=2,
                                tournament_size=3, seed=0, max_seconds=budget,
                                stagnation_generations=30,
                                on_generation=lambda g, s: self._emit(
                                    "generation", experiment=name, generation=g,
                                    best=s["best"], mean=s["mean"]))
                return EvolutionEngine(expression_spec, fitness, cfg).run()

            return name, run

        def minimize_fn(name: str, fn: Any, dims: int, bounds: tuple[float, float],
                        gens: int, pop: int) -> tuple[str, Callable[[float], RunResult]]:
            spec = numeric_spec([bounds] * dims)

            def run(budget: float) -> RunResult:
                cfg = RunConfig(population_size=pop, generations=gens,
                                mutation_rate=0.3, crossover_rate=0.8, elitism=2,
                                tournament_size=3, seed=0, max_seconds=budget,
                                stagnation_generations=20,
                                on_generation=lambda g, s: self._emit(
                                    "generation", experiment=name, generation=g,
                                    best=s["best"], mean=s["mean"]))
                return EvolutionEngine(spec, minimize(fn), cfg).run()

            return name, run

        def tune(name: str, fn: Any, dims: int) -> tuple[str, Callable[[float], RunResult]]:
            spec = numeric_spec([(-5.0, 5.0)] * dims)

            def run(budget: float) -> RunResult:
                return self_tune(spec, fn, meta_generations=8,
                                 task_generations=15, task_population=24,
                                 eval_seeds=(0,), max_seconds=budget)

            return name, run

        return [
            symbolic("symbolic-linear", [0, 1, 2, 3, 4], [1, 3, 5, 7, 9], 80, 120),
            symbolic("symbolic-quadratic", [-3, -2, -1, 0, 1, 2, 3],
                     [x * x + 2 * x + 1 for x in (-3, -2, -1, 0, 1, 2, 3)], 100, 150),
            minimize_fn("minimize-sphere", sphere, 4, (-5.0, 5.0), 60, 60),
            minimize_fn("minimize-rastrigin", rastrigin, 4, (-5.12, 5.12), 80, 80),
            minimize_fn("minimize-ackley", ackley, 4, (-32.768, 32.768), 80, 80),
            minimize_fn("minimize-rosenbrock", rosenbrock, 3, (-5.0, 10.0), 80, 80),
            tune("self-tune-sphere", sphere, 2),
        ]

    def run(self) -> dict[str, Any]:
        self.started_at = time.time()
        self.results = []
        queue = self._queue()
        self._emit("queue_started", experiments=[name for name, _ in queue],
                   minutes=self.minutes)

        for name, fn in queue:
            if self._remaining() <= 0:
                self._emit("budget_exhausted", remaining=queue)
                break
            self._emit("experiment_started", experiment=name)
            t0 = time.time()
            result = fn(max(5.0, self._remaining()))
            seconds = round(time.time() - t0, 2)
            self.results.append({"name": name, "result": result.to_dict(),
                                 "seconds": seconds})
            self._emit("experiment_done", experiment=name,
                       best_fitness=result.best.fitness,
                       generations=result.generations_run, seconds=seconds)

        self.finished_at = time.time()
        report = self._report()
        self._emit("queue_done", report=report)
        return report

    def _report(self) -> dict[str, Any]:
        rows = []
        for r in self.results:
            best = r["result"]["best"]
            rows.append({
                "experiment": r["name"],
                "best_fitness": best["fitness"],
                "best_genome": best["genome"],
                "best_str": (expression_str(best["genome"])
                             if r["name"].startswith("symbolic") else None),
                "generations": r["result"]["generations_run"],
                "seconds": r["seconds"],
            })
        return {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at)),
            "wall_seconds": round(self.finished_at - self.started_at, 1),
            "budget_minutes": self.minutes,
            "experiments_run": len(rows),
            "rows": rows,
        }

    def save_report(self) -> str | None:
        if not self.report_path:
            return None
        import json
        with open(self.report_path, "w", encoding="utf-8") as f:
            json.dump(self._report(), f, indent=2)
        return self.report_path


def auto_run(minutes: float = 10.0, report_path: str | None = None,
             on_progress: Progress | None = None) -> dict[str, Any]:
    """Run the standard experiment queue and return the report."""
    runner = AutoRunner(minutes=minutes, on_progress=on_progress,
                        report_path=report_path)
    report = runner.run()
    if report_path:
        runner.save_report()
    return report


def _print_report(report: dict[str, Any]) -> None:
    print(f"\n=== EVOLVE automatic run ===")
    print(f"wall time: {report['wall_seconds']}s · experiments: {report['experiments_run']}")
    for r in report["rows"]:
        label = r["experiment"]
        if r["best_str"]:
            label += f"  ->  f(x) = {r['best_str']}"
        else:
            label += f"  ->  genome {r['best_genome']}"
        print(f"  {label}")
        print(f"      fitness={r['best_fitness']:.6g}  gens={r['generations']}  "
              f"time={r['seconds']}s")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(prog="python -m jarvis.evolve auto")
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--report", default=None, help="path to write the JSON report")
    args = parser.parse_args()
    _print_report(auto_run(args.minutes, args.report))
