"""EVOLVE — command-line interface.

Run evolutionary experiments without JARVIS running:

    python -m jarvis.evolve symbolic --xs 0,1,2,3 --ys 1,3,5,7
    python -m jarvis.evolve minimize --function rastrigin --dimensions 4
    python -m jarvis.evolve selftune --task sphere
    python -m jarvis.evolve demo
    python -m jarvis.evolve gui
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from .engine import EvolutionEngine, RunConfig
from .genome import expression_spec, expression_str, numeric_spec
from .meta import self_tune
from .objectives import (ackley, griewank, maximize, minimize, rastrigin,
                         rosenbrock, sphere, symbolic_regression)

__all__ = ["cli_main", "demo"]

_BENCHMARKS = {"sphere": sphere, "rastrigin": rastrigin, "ackley": ackley,
               "rosenbrock": rosenbrock, "griewank": griewank}


def _numbers(text: str) -> list[float]:
    return [float(x) for x in text.replace(",", " ").split() if x.strip()]


def _run_symbolic(xs: list[float], ys: list[float], generations: int,
                  population: int, seed: int) -> None:
    fitness = symbolic_regression(xs, ys)
    cfg = RunConfig(population_size=population, generations=generations,
                    mutation_rate=0.35, crossover_rate=0.9, elitism=2,
                    tournament_size=3, selection="tournament", seed=seed,
                    max_seconds=120.0, stagnation_generations=30)
    t0 = time.perf_counter()
    res = EvolutionEngine(expression_spec, fitness, cfg).run()
    print("\n=== symbolic regression ===")
    print(f"xs: {xs}")
    print(f"ys: {ys}")
    print(f"recovered expression: f(x) = {expression_str(res.best.genome)}")
    print(f"mean-squared error:   {res.best.fitness:.6f}")
    print(f"generations run:      {res.generations_run}")
    print(f"wall time:            {time.perf_counter() - t0:.2f}s")
    _print_history(res)


def _run_minimize(function: str, dimensions: int, generations: int,
                  population: int, seed: int) -> None:
    fn = _BENCHMARKS[function]
    lo, hi = (-5.12, 5.12) if function == "rastrigin" else (-5.0, 5.0)
    spec = numeric_spec([(lo, hi)] * dimensions)
    cfg = RunConfig(population_size=population, generations=generations,
                    mutation_rate=0.3, crossover_rate=0.8, elitism=2,
                    tournament_size=3, seed=seed, max_seconds=120.0,
                    stagnation_generations=20)
    t0 = time.perf_counter()
    res = EvolutionEngine(spec, minimize(fn), cfg).run()
    print("\n=== minimize:", function, "===")
    print(f"best vector: {[round(v, 5) for v in res.best.genome]}")
    print(f"best cost:   {res.best.fitness:.6f}   (global minimum is 0)")
    print(f"wall time:   {time.perf_counter() - t0:.2f}s")
    _print_history(res)


def _run_selftune(task: str, meta_generations: int) -> None:
    fn = _BENCHMARKS[task]
    spec = numeric_spec([(-5.0, 5.0)] * 2)
    t0 = time.perf_counter()
    res = self_tune(spec, fn, meta_generations=meta_generations,
                    task_generations=15, task_population=24, eval_seeds=(0,),
                    max_seconds=180.0)
    print("\n=== self-evolve (evolve the engine's own config) ===")
    print(f"task:               {task}")
    print(f"winning config:     {res.best.genome}")
    print(f"meta cost:          {res.best.fitness:.6f}")
    print(f"meta generations:   {res.generations_run}")
    print(f"wall time:          {time.perf_counter() - t0:.2f}s")


def _print_history(res: Any) -> None:
    h = res.history
    if not h:
        return
    first, last = h[0], h[-1]
    print(f"fitness trajectory: gen 0 best={first['best']:.4g}  ->  "
          f"gen {last['generation']} best={last['best']:.6g} "
          f"(diversity {last['diversity']:.3g}, unique {last['unique']})")


def demo() -> None:
    """Run all three demonstrations back-to-back."""
    print("=" * 64)
    print("EVOLVE DEMO — evolutionary intelligence")
    print("=" * 64)
    _run_symbolic([-3, -2, -1, 0, 1, 2, 3],
                  [x * x + 2 * x + 1 for x in [-3, -2, -1, 0, 1, 2, 3]],
                  generations=100, population=150, seed=5)
    _run_minimize("rastrigin", dimensions=5, generations=80, population=80, seed=1)
    _run_selftune("sphere", meta_generations=6)


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.evolve",
                                     description="EVOLVE evolutionary engine")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("symbolic", help="evolve an expression f(x) for sample data")
    p.add_argument("--xs", required=True, help="comma-separated x points")
    p.add_argument("--ys", required=True, help="comma-separated y points")
    p.add_argument("--generations", type=int, default=80)
    p.add_argument("--population", type=int, default=150)
    p.add_argument("--seed", type=int, default=0)

    p = sub.add_parser("minimize", help="minimize a benchmark function")
    p.add_argument("--function", default="sphere", choices=sorted(_BENCHMARKS))
    p.add_argument("--dimensions", type=int, default=4)
    p.add_argument("--generations", type=int, default=60)
    p.add_argument("--population", type=int, default=60)
    p.add_argument("--seed", type=int, default=0)

    p = sub.add_parser("selftune", help="evolve the engine's own configuration")
    p.add_argument("--task", default="sphere", choices=sorted(_BENCHMARKS))
    p.add_argument("--meta-generations", type=int, default=10)

    p = sub.add_parser("auto", help="run the automatic experiment queue (bounded)")
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--report", default=None, help="path to write the JSON report")

    p = sub.add_parser("tune", help="tune real server settings (Ollama latency)")
    p.add_argument("--target", default="ollama", choices=("ollama",))
    p.add_argument("--generations", type=int, default=8)
    p.add_argument("--population", type=int, default=10)
    p.add_argument("--base-url", default="http://127.0.0.1:11434")
    p.add_argument("--model", default="qwen2.5:3b-instruct")

    p = sub.add_parser("tune-command", help="evolve one {level} parameter of a command")
    p.add_argument("--command", required=True, help="shell command with a {level} placeholder")
    p.add_argument("--low", type=int, required=True)
    p.add_argument("--high", type=int, required=True)
    p.add_argument("--runs", type=int, default=2)
    p.add_argument("--generations", type=int, default=15)

    sub.add_parser("demo", help="run all demonstrations")
    sub.add_parser("gui", help="launch the desktop GUI")

    args = parser.parse_args(argv)
    if args.cmd == "symbolic":
        xs = _numbers(args.xs)
        ys = _numbers(args.ys)
        if len(xs) != len(ys) or not xs:
            print("error: xs and ys must be matching non-empty lists", file=sys.stderr)
            return 2
        _run_symbolic(xs, ys, args.generations, args.population, args.seed)
    elif args.cmd == "minimize":
        _run_minimize(args.function, args.dimensions, args.generations,
                      args.population, args.seed)
    elif args.cmd == "selftune":
        _run_selftune(args.task, args.meta_generations)
    elif args.cmd == "auto":
        from .auto import auto_run, _print_report
        _print_report(auto_run(args.minutes, args.report))
    elif args.cmd == "tune":
        from .measure import ollama_latency
        from .tuner import SystemTuner, ollama_space
        fitness = ollama_latency(args.base_url, args.model, "Say OK.", runs=1, warmup=0)
        tuner = SystemTuner(ollama_space(), fitness, name="ollama")
        t0 = time.time()
        res = tuner.tune(generations=args.generations, population=args.population,
                         max_seconds=600.0)
        print("\n=== tune: ollama ===")
        print(f"best settings: {res.best.genome}")
        print(f"measured latency: {res.best.fitness:.3f}s")
        print(f"wall time: {time.time() - t0:.1f}s")
    elif args.cmd == "tune-command":
        from .measure import command_benchmark
        from .settings import Field, SettingsSpace
        from .tuner import SystemTuner
        space = SettingsSpace("command", {"level": Field("int", low=args.low, high=args.high)})
        fitness = command_benchmark(args.command, runs=args.runs)
        tuner = SystemTuner(space, fitness, name="command")
        t0 = time.time()
        res = tuner.tune(generations=args.generations, population=20, max_seconds=600.0)
        print("\n=== tune-command ===")
        print(f"best parameter: {res.best.genome['level']}")
        print(f"mean wall time: {res.best.fitness:.4f}s")
        print(f"wall time: {time.time() - t0:.1f}s")
    elif args.cmd == "demo":
        demo()
    elif args.cmd == "gui":
        from .gui import gui_main
        gui_main()
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
