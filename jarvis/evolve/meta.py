"""EVOLVE — meta-evolution (the engine evolves its own configuration).

Level 3 of the plan: optimise the search strategy, not just a solution. The
engine's own :class:`~jarvis.evolve.engine.RunConfig` becomes the genome; the
meta-fitness is *how well that configuration converges* on a fixed benchmark
task. This is bounded self-improvement: it is verified against a fixed,
independent objective and never changes the trusted execution/verification
layer.
"""

from __future__ import annotations

import random
from typing import Any, Callable

from .engine import EvolutionEngine, RunConfig, RunResult
from .genome import GenomeSpec

__all__ = ["CONFIG_SPACE", "config_spec", "self_tune"]

# The searchable engine hyper-parameters and their discrete choices.
CONFIG_SPACE: dict[str, list[Any]] = {
    "population_size": [20, 30, 50, 70],
    "mutation_rate": [0.1, 0.2, 0.3, 0.4],
    "crossover_rate": [0.5, 0.7, 0.8, 0.9],
    "elitism": [1, 2, 3, 5],
    "tournament_size": [2, 3, 5],
    "selection": ["tournament", "rank", "roulette"],
}

_KEYS = list(CONFIG_SPACE.keys())


def _random_config(rng: random.Random) -> dict[str, Any]:
    return {k: rng.choice(CONFIG_SPACE[k]) for k in _KEYS}


def _mutate_config(cfg: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    out = dict(cfg)
    for k in _KEYS:
        if rng.random() < 0.3:
            out[k] = rng.choice(CONFIG_SPACE[k])
    return out


def _crossover_config(a: dict[str, Any], b: dict[str, Any],
                      rng: random.Random) -> tuple[dict[str, Any], dict[str, Any]]:
    ca, cb = dict(a), dict(b)
    for k in _KEYS:
        if rng.random() < 0.5:
            ca[k], cb[k] = cb[k], ca[k]
    return ca, cb


config_spec = GenomeSpec(
    name="config",
    random=_random_config,
    mutate=_mutate_config,
    crossover=_crossover_config,
    to_str=lambda c: repr(c),
)


def self_tune(spec: GenomeSpec, fitness: Callable[[Any], float], *,
              meta_generations: int = 12, meta_population: int = 16,
              task_generations: int = 25, task_population: int = 40,
              eval_seeds: tuple[int, ...] = (0, 1),
              max_seconds: float = 300.0,
              seed: int = 0) -> RunResult:
    """Evolve a :class:`RunConfig` that converges well on ``spec`` + ``fitness``.

    Returns a :class:`RunResult` whose ``best.genome`` is the winning config
    dict and whose ``best.fitness`` is its mean final cost across ``eval_seeds``.
    """

    def meta_fitness(cfg: dict[str, Any]) -> float:
        # Deterministic single-seed score for selection, averaged over a couple
        # of seeds to damp noise from the underlying search's randomness.
        costs = []
        for s in eval_seeds:
            rc = RunConfig(
                population_size=int(cfg["population_size"]),
                generations=task_generations,
                mutation_rate=float(cfg["mutation_rate"]),
                crossover_rate=float(cfg["crossover_rate"]),
                elitism=int(cfg["elitism"]),
                tournament_size=int(cfg["tournament_size"]),
                selection=str(cfg["selection"]),
                seed=s,
                max_seconds=max_seconds,
                stagnation_generations=task_generations,  # no early stop here
            )
            res = EvolutionEngine(spec, fitness, rc).run()
            costs.append(res.best.fitness)
        return sum(costs) / len(costs)

    meta = EvolutionEngine(
        config_spec, meta_fitness,
        RunConfig(
            population_size=meta_population, generations=meta_generations,
            mutation_rate=0.3, crossover_rate=0.8, elitism=1,
            tournament_size=2, selection="tournament", seed=seed,
            max_seconds=max_seconds, stagnation_generations=4,
        ),
    )
    return meta.run()
