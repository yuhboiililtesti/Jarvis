"""EVOLVE — fitness objectives.

Fitness functions map a genome to a scalar cost (**lower is better**). They are
plain callables, so any measurable target (a benchmark, a loss, a game score
negated, a task validator) plugs into the engine unchanged.

A few ready-made objectives and reference functions ship here for testing and
as worked examples.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Sequence

from .genome import evaluate_expression, tree_size

__all__ = ["symbolic_regression", "minimize", "maximize", "sphere", "rastrigin",
           "ackley", "rosenbrock", "griewank"]

Fitness = Callable[[Any], float]


def symbolic_regression(xs: Sequence[float], ys: Sequence[float], *,
                        parsimony: float = 0.001) -> Fitness:
    """Mean-squared error of an expression tree against sample points.

    The engine evolves an expression ``f(x)`` whose values match ``ys`` at the
    given ``xs``. A **parsimony** penalty proportional to tree size is added so
    the search prefers simple expressions over bloated ones that only overfit
    the sample points — lower MSE is better, but not at the cost of absurd
    complexity.
    """
    assert len(xs) == len(ys) and xs, "need matching, non-empty x/y samples"
    points = list(zip(xs, ys))

    def fitness(tree: Any) -> float:
        err = 0.0
        for x, y in points:
            d = evaluate_expression(tree, x) - y
            err += d * d
        return err / len(points) + parsimony * tree_size(tree)

    return fitness


def minimize(fn: Callable[[Any], float]) -> Fitness:
    """Minimise ``fn``; the genome is passed straight through to ``fn``."""

    def fitness(genome: Any) -> float:
        return float(fn(genome))

    return fitness


def maximize(fn: Callable[[Any], float]) -> Fitness:
    """Maximise ``fn`` by negating it (lower is still better internally)."""

    def fitness(genome: Any) -> float:
        return -float(fn(genome))

    return fitness


# --------------------------------------------------------------------------- #
# Reference numeric benchmark functions (minima at 0).
# --------------------------------------------------------------------------- #

def sphere(x: Sequence[float]) -> float:
    return sum(float(v) ** 2 for v in x)


def rastrigin(x: Sequence[float]) -> float:
    n = len(x)
    return 10.0 * n + sum(float(v) ** 2 - 10.0 * math.cos(2.0 * math.pi * float(v))
                          for v in x)


def ackley(x: Sequence[float]) -> float:
    """Multimodal; global min 0 at the origin. Typical domain ±32.768."""
    n = len(x)
    s1 = sum(float(v) ** 2 for v in x) / n
    s2 = sum(math.cos(2.0 * math.pi * float(v)) for v in x) / n
    return (-20.0 * math.exp(-0.2 * math.sqrt(s1)) - math.exp(s2)
            + 20.0 + math.e)


def rosenbrock(x: Sequence[float]) -> float:
    """Valley-shaped; global min 0 at all-ones. Typical domain [-5, 10]."""
    return sum(100.0 * (float(x[i + 1]) - float(x[i]) ** 2) ** 2
               + (1.0 - float(x[i])) ** 2 for i in range(len(x) - 1))


def griewank(x: Sequence[float]) -> float:
    """Many local minima; global min 0 at the origin. Typical domain ±600."""
    n = len(x)
    s = sum(float(v) ** 2 for v in x) / 4000.0
    p = 1.0
    for i, v in enumerate(x):
        p *= math.cos(float(v) / math.sqrt(i + 1))
    return 1.0 + s - p
