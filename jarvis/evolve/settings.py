"""EVOLVE — typed settings genome (the "config" genome for real systems).

Evolve *configurations*, not just numbers: a :class:`SettingsSpace` declares
named fields (integer / float / boolean / choice) with their legal ranges, and
:func:`settings_spec` turns it into a :class:`~jarvis.evolve.genome.GenomeSpec`
whose individuals are plain dicts. Combine with a *measured* fitness
(:mod:`jarvis.evolve.measure`) to tune real servers — game servers, LLM
servers, build commands — by evolving their settings against real metrics.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .genome import GenomeSpec

__all__ = ["Field", "SettingsSpace", "settings_spec"]


@dataclass(frozen=True)
class Field:
    """One configurable setting and its legal values."""

    kind: str                                   # int | float | bool | choice
    low: float | None = None                    # int/float: inclusive lower bound
    high: float | None = None                   # int/float: inclusive upper bound
    choices: tuple[Any, ...] | None = None      # choice: allowed values
    default: Any = None

    def sample(self, rng: random.Random) -> Any:
        if self.kind == "int":
            return rng.randint(int(self.low), int(self.high))
        if self.kind == "float":
            return rng.uniform(self.low, self.high)
        if self.kind == "bool":
            return rng.random() < 0.5
        if self.kind == "choice":
            return rng.choice(self.choices)
        raise ValueError(f"unknown field kind {self.kind!r}")


@dataclass
class SettingsSpace:
    """A named configuration space: ``{key: Field}``."""

    name: str
    fields: dict[str, Field] = field(default_factory=dict)

    def sample(self, rng: random.Random) -> dict[str, Any]:
        return {k: f.sample(rng) for k, f in self.fields.items()}


def settings_spec(space: SettingsSpace, *, mutate_prob: float = 0.3) -> GenomeSpec:
    """A :class:`GenomeSpec` whose individuals are dicts over ``space``."""

    def random_genome(rng: random.Random) -> dict[str, Any]:
        return space.sample(rng)

    def mutate(genome: dict[str, Any], rng: random.Random) -> dict[str, Any]:
        out = dict(genome)
        for key, f in space.fields.items():
            if rng.random() < mutate_prob:
                out[key] = f.sample(rng)
        return out

    def crossover(a: dict[str, Any], b: dict[str, Any],
                  rng: random.Random) -> tuple[dict[str, Any], dict[str, Any]]:
        ca, cb = dict(a), dict(b)
        for key in space.fields:
            if rng.random() < 0.5:
                ca[key], cb[key] = cb[key], ca[key]
        return ca, cb

    def to_str(genome: dict[str, Any]) -> str:
        return ", ".join(f"{k}={genome.get(k)!r}" for k in space.fields)

    return GenomeSpec(name=space.name, random=random_genome, mutate=mutate,
                      crossover=crossover, to_str=to_str)
