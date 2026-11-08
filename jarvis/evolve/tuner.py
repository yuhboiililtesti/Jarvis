"""EVOLVE — the system tuner: evolve real configurations against real metrics.

:class:`SystemTuner` joins a :class:`~jarvis.evolve.settings.SettingsSpace`
with a *measured* fitness (:mod:`jarvis.evolve.measure`) and an optional
apply function. Two honest modes:

* **measure mode** (default, safe) — each candidate is evaluated by measuring
  it directly (running the command, timing the LLM request).
* **apply mode** (explicitly enabled, disruptive) — each candidate is *applied*
  to the live system (config write + service restart) before measuring. This
  must be owner-approved and used with a backup; it is off by default.

Named settings spaces for real targets ship here (Ollama, Minecraft server,
gzip, make-style build jobs).
"""

from __future__ import annotations

from typing import Any, Callable

from .engine import EvolutionEngine, RunConfig, RunResult
from .settings import Field, SettingsSpace, settings_spec

__all__ = ["SystemTuner", "ollama_space", "minecraft_space", "gzip_space",
           "build_jobs_space"]

Fitness = Callable[[dict[str, Any]], float]
Apply = Callable[[dict[str, Any]], None]


class SystemTuner:
    """Evolve a configuration space against a measured fitness function."""

    def __init__(self, space: SettingsSpace, fitness: Fitness,
                 apply: Apply | None = None, name: str = "") -> None:
        self.space = space
        self.name = name or space.name
        self.fitness = fitness
        self.apply = apply

    def tune(self, *, generations: int = 30, population: int = 40, seed: int = 0,
             apply_each: bool = False,
             on_generation: Callable[[int, dict[str, Any]], None] | None = None,
             max_seconds: float = 300.0) -> RunResult:
        """Evolve settings; ``apply_each`` applies every candidate before
        measuring (disruptive — only for an owner-approved live retune)."""
        spec = settings_spec(self.space)

        def measured(genome: dict[str, Any]) -> float:
            if apply_each and self.apply is not None:
                try:
                    self.apply(genome)
                except Exception:
                    return float("inf")
            return self.fitness(genome)

        cfg = RunConfig(population_size=population, generations=generations,
                        mutation_rate=0.35, crossover_rate=0.8, elitism=2,
                        tournament_size=3, seed=seed, max_seconds=max_seconds,
                        stagnation_generations=12, on_generation=on_generation)
        return EvolutionEngine(spec, measured, cfg).run()

    def apply_best(self, result: RunResult) -> dict[str, Any]:
        """Apply the winning settings to the live system (if an apply fn exists)."""
        if self.apply is None:
            return {"applied": False, "reason": "no apply function configured"}
        genome = result.best.genome
        try:
            self.apply(genome)
        except Exception as exc:  # pragma: no cover
            return {"applied": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {"applied": True, "settings": genome,
                "measured_fitness": result.best.fitness}


# --------------------------------------------------------------------------- #
# Named configuration spaces for real targets
# --------------------------------------------------------------------------- #

def ollama_space() -> SettingsSpace:
    """Ollama request options (per-generation inference settings)."""
    return SettingsSpace("ollama", {
        "num_ctx": Field("int", low=1024, high=8192, default=4096),
        "num_predict": Field("int", low=32, high=512, default=256),
        "temperature": Field("float", low=0.0, high=1.0, default=0.4),
        "top_p": Field("float", low=0.5, high=1.0, default=0.9),
    })


def minecraft_space() -> SettingsSpace:
    """Minecraft ``server.properties`` fields that most affect performance."""
    return SettingsSpace("minecraft-server", {
        "view_distance": Field("int", low=4, high=16, default=10),
        "simulation_distance": Field("int", low=4, high=16, default=8),
        "max_players": Field("int", low=5, high=100, default=20),
        "network_compression_threshold": Field("int", low=64, high=1024, default=256),
        "max_tick_time": Field("int", low=30000, high=120000, default=60000),
        "spawn_protection": Field("int", low=0, high=64, default=16),
    })


def gzip_space() -> SettingsSpace:
    """Compression level of a gzip-style command."""
    return SettingsSpace("gzip", {
        "level": Field("int", low=1, high=9, default=6),
    })


def build_jobs_space() -> SettingsSpace:
    """Parallel job count for a make-style build command."""
    return SettingsSpace("build-jobs", {
        "jobs": Field("int", low=1, high=16, default=4),
    })
