"""EVOLVE — the evolutionary engine core.

A generic, deterministic, bounded loop: initialise a population → evaluate
fitness → select parents (with elitism and diversity) → crossover + mutate →
evaluate offspring → archive the best → repeat. It is representation-agnostic:
any :class:`~jarvis.evolve.genome.GenomeSpec` works, so the same loop evolves
numeric vectors, expression trees, or future genome types.

Design rules honoured here:

* **Lower fitness is better** (minimisation; matches error/loss objectives).
* **Determinism** — one seeded ``random.Random`` drives the whole run.
* **Bounded** — generation and wall-clock budgets; a stagnation limit stops
  runs that stop improving.
* **Evidence** — every generation's best/mean/worst fitness and diversity are
  recorded, never asserted without a measurement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .genome import GenomeSpec

__all__ = ["Individual", "RunConfig", "RunResult", "EvolutionEngine"]


Fitness = Callable[[Any], float]


@dataclass
class Individual:
    genome: Any
    fitness: float = float("inf")
    id: str = ""
    parents: tuple[str, ...] = ()
    born_gen: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"genome": self.genome, "fitness": self.fitness, "id": self.id,
                "parents": list(self.parents), "born_gen": self.born_gen}


@dataclass
class RunConfig:
    population_size: int = 50
    generations: int = 50
    mutation_rate: float = 0.25      # probability a child is mutated
    crossover_rate: float = 0.8      # probability two parents recombine
    elitism: int = 2                 # top N carried over unchanged
    tournament_size: int = 3
    selection: str = "tournament"    # tournament | rank | roulette
    seed: int = 0
    max_seconds: float = 60.0
    stagnation_generations: int = 15  # stop if no improvement for this many gens
    # Optional progress hook: called once per generation with
    # (generation, stats_dict). Enables live charts / progress reporting.
    on_generation: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "population_size": self.population_size, "generations": self.generations,
            "mutation_rate": self.mutation_rate, "crossover_rate": self.crossover_rate,
            "elitism": self.elitism, "tournament_size": self.tournament_size,
            "selection": self.selection, "seed": self.seed,
            "max_seconds": self.max_seconds,
            "stagnation_generations": self.stagnation_generations,
        }


@dataclass
class RunResult:
    best: Individual
    config: dict[str, Any]
    history: list[dict[str, Any]] = field(default_factory=list)
    converged: bool = False
    generations_run: int = 0
    wall_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "best": self.best.to_dict(), "config": self.config,
            "history": self.history, "converged": self.converged,
            "generations_run": self.generations_run,
            "wall_seconds": round(self.wall_seconds, 3),
        }


class EvolutionEngine:
    """Runs one evolutionary search over a genome space with a fitness function."""

    def __init__(self, spec: GenomeSpec, fitness: Fitness, config: RunConfig) -> None:
        self.spec = spec
        self.fitness = fitness
        self.config = config
        self._rng = None
        self._id_counter = 0

    # -- internal helpers -------------------------------------------------
    def _next_id(self) -> str:
        self._id_counter += 1
        return f"ind_{self._id_counter}"

    def _safe_fitness(self, genome: Any) -> float:
        try:
            return float(self.fitness(genome))
        except Exception:
            return float("inf")
        except (OverflowError, ZeroDivisionError):
            return float("inf")

    def _make(self, genome: Any, parents: tuple[str, ...] = (), gen: int = 0) -> Individual:
        ind = Individual(genome=genome, id=self._next_id(), parents=parents, born_gen=gen)
        ind.fitness = self._safe_fitness(genome)
        return ind

    def _select(self, population: list[Individual], count: int) -> list[Individual]:
        cfg = self.config
        rng = self._rng
        method = cfg.selection
        if method == "rank":
            ranked = sorted(population, key=lambda i: i.fitness)
            return ranked[:count]
        if method == "roulette":
            ranked = sorted(population, key=lambda i: i.fitness)
            n = len(ranked)
            # Rank-weighted: best individual gets weight n, worst gets 1.
            weights = [n - i for i in range(n)]
            return [rng.choices(ranked, weights=weights, k=1)[0] for _ in range(count)]
        # tournament (default)
        out = []
        for _ in range(count):
            contestants = rng.sample(population, min(cfg.tournament_size, len(population)))
            out.append(min(contestants, key=lambda i: i.fitness))
        return out

    # -- main loop --------------------------------------------------------
    def run(self) -> RunResult:
        import random

        cfg = self.config
        self._rng = random.Random(cfg.seed)
        rng = self._rng
        t0 = time.perf_counter()

        population = [self._make(self.spec.random(rng), gen=0)
                      for _ in range(cfg.population_size)]
        population.sort(key=lambda i: i.fitness)

        best = min(population, key=lambda i: i.fitness)
        history: list[dict[str, Any]] = []
        stagnant = 0
        last_best = best.fitness
        ran_generations = 0

        def _stats(gen: int) -> dict[str, Any]:
            fits = [i.fitness for i in population]
            finite = [f for f in fits if f != float("inf")]
            mean = sum(finite) / len(finite) if finite else float("inf")
            worst = max(fits) if fits else float("inf")
            spread = (max(finite) - min(finite)) if finite else 0.0
            return {"generation": gen, "best": best.fitness, "mean": round(mean, 6),
                    "worst": worst, "diversity": round(spread, 6),
                    "unique": len({round(f, 9) for f in finite})}

        for gen in range(1, cfg.generations + 1):
            ran_generations = gen
            stats = _stats(gen - 1)
            history.append(stats)
            if cfg.on_generation is not None:
                try:
                    cfg.on_generation(gen - 1, stats)
                except Exception:  # pragma: no cover - a progress hook must not break the run
                    pass

            # Elitism: carry the top survivors forward unchanged.
            ranked = sorted(population, key=lambda i: i.fitness)
            next_gen: list[Individual] = ranked[:cfg.elitism]

            while len(next_gen) < cfg.population_size:
                p1, p2 = self._select(population, 2)
                child_genome = p1.genome
                parents = (p1.id, p2.id)
                if rng.random() < cfg.crossover_rate:
                    a, b = self.spec.crossover(p1.genome, p2.genome, rng)
                    child_genome = a
                if rng.random() < cfg.mutation_rate:
                    child_genome = self.spec.mutate(child_genome, rng)
                next_gen.append(self._make(child_genome, parents=parents, gen=gen))

            population = next_gen
            population.sort(key=lambda i: i.fitness)

            new_best = min(population, key=lambda i: i.fitness)
            if new_best.fitness < best.fitness:
                best = new_best
                stagnant = 0
            else:
                stagnant += 1
            last_best = best.fitness

            if (time.perf_counter() - t0) > cfg.max_seconds:
                break
            if stagnant >= cfg.stagnation_generations:
                break

        final_stats = _stats(ran_generations)
        history.append(final_stats)
        if cfg.on_generation is not None:
            try:
                cfg.on_generation(ran_generations, final_stats)
            except Exception:  # pragma: no cover - a progress hook must not break the run
                pass
        return RunResult(
            best=best, config=cfg.to_dict(), history=history,
            converged=(stagnant >= cfg.stagnation_generations),
            generations_run=ran_generations,
            wall_seconds=time.perf_counter() - t0,
        )
