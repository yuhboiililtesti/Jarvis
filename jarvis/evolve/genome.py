"""EVOLVE — genome representations.

Genomes are plain Python values; each representation is described by a
:class:`GenomeSpec` that bundles the three operations the engine needs:
``random`` (produce a fresh genome), ``mutate`` (perturb one) and ``crossover``
(combine two). The engine is generic over these, so new representations (plans,
neural architectures, workflow graphs…) can be added without touching the loop.

Two representations ship out of the box:

* :class:`numeric_spec` — a fixed list of bounded floats (numeric optimisation).
* :class:`expression_spec` — a symbolic expression tree (symbolic regression /
  program synthesis).

Everything is deterministic under a supplied ``random.Random`` instance.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "GenomeSpec", "numeric_spec", "expression_spec",
    "evaluate_expression", "expression_str", "tree_size",
]

Genome = Any
RNG = random.Random


@dataclass(frozen=True)
class GenomeSpec:
    """The operations an evolutionary engine needs for one genome type."""

    name: str
    random: Callable[[RNG], Genome]
    mutate: Callable[[Genome, RNG], Genome]
    crossover: Callable[[Genome, Genome, RNG], tuple[Genome, Genome]]
    to_str: Callable[[Genome], str] = repr


# --------------------------------------------------------------------------- #
# Numeric genome: a fixed-length vector of bounded floats.
# --------------------------------------------------------------------------- #

def _numeric_random(bounds: list[tuple[float, float]]) -> Callable[[RNG], list[float]]:
    def random_genome(rng: RNG) -> list[float]:
        return [rng.uniform(lo, hi) for lo, hi in bounds]

    return random_genome


def _numeric_mutate(bounds: list[tuple[float, float]]) -> Callable[[list[float], RNG], list[float]]:
    def mutate(genome: list[float], rng: RNG) -> list[float]:
        out = list(genome)
        for i, (lo, hi) in enumerate(bounds):
            if rng.random() < 0.5:  # mutate this gene with 50% probability
                span = hi - lo
                out[i] = min(hi, max(lo, out[i] + rng.gauss(0.0, span * 0.15)))
        return out

    return mutate


def _numeric_crossover(_bounds) -> Callable[[list[float], list[float], RNG],
                                            tuple[list[float], list[float]]]:
    def crossover(a: list[float], b: list[float], rng: RNG) -> tuple[list[float], list[float]]:
        # Uniform (per-gene) arithmetic crossover.
        ca, cb = list(a), list(b)
        for i in range(len(a)):
            if rng.random() < 0.5:
                alpha = rng.random()
                ca[i] = alpha * a[i] + (1.0 - alpha) * b[i]
                cb[i] = (1.0 - alpha) * a[i] + alpha * b[i]
        return ca, cb

    return crossover


def numeric_spec(bounds: list[tuple[float, float]]) -> GenomeSpec:
    """A numeric genome over the given per-gene ``(lo, hi)`` bounds."""
    return GenomeSpec(
        name="numeric",
        random=_numeric_random(bounds),
        mutate=_numeric_mutate(bounds),
        crossover=_numeric_crossover(bounds),
        to_str=lambda g: "[" + ", ".join(f"{v:.4g}" for v in g) + "]",
    )


# --------------------------------------------------------------------------- #
# Expression genome: a symbolic expression tree (genetic programming).
# --------------------------------------------------------------------------- #

# Binary and unary operators.  Trees are tuples: ("+", left, right),
# ("sin", child), ("x",), or ("c", value).
_BINARY = ("+", "-", "*", "/")
_UNARY = ("sin", "cos", "exp", "log", "neg")
_ARITY = {op: 2 for op in _BINARY}
_ARITY.update({op: 1 for op in _UNARY})

_MAX_DEPTH = 5
_CONST_RANGE = 5.0
_TERMINAL_PROB = 0.3


def evaluate_expression(tree: Any, x: float) -> float:
    """Evaluate an expression tree at ``x`` with protected math operators."""
    if not isinstance(tree, tuple):
        return float(tree)
    op = tree[0]
    if op == "c":
        return float(tree[1])
    if op == "x":
        return float(x)
    if op in _BINARY:
        a = evaluate_expression(tree[1], x)
        b = evaluate_expression(tree[2], x)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b if abs(b) > 1e-9 else math.copysign(1.0, b) * 1e6
    # unary
    a = evaluate_expression(tree[1], x)
    if op == "sin":
        return math.sin(a)
    if op == "cos":
        return math.cos(a)
    if op == "neg":
        return -a
    if op == "exp":
        return math.exp(max(-50.0, min(50.0, a)))
    if op == "log":
        return math.log(abs(a) + 1e-9)
    raise ValueError(f"unknown op {op!r}")


def expression_str(tree: Any) -> str:
    """Compact infix rendering of an expression tree."""
    if not isinstance(tree, tuple):
        return str(tree)
    op = tree[0]
    if op == "c":
        return f"{tree[1]:.3g}"
    if op == "x":
        return "x"
    if op in _BINARY:
        return f"({expression_str(tree[1])} {op} {expression_str(tree[2])})"
    if op == "neg":
        return f"(-{expression_str(tree[1])})"
    return f"{op}({expression_str(tree[1])})"


def tree_size(tree: Any) -> int:
    """Number of nodes in an expression tree (used for parsimony pressure)."""
    if not isinstance(tree, tuple):
        return 1
    return 1 + sum(tree_size(child) for child in tree[1:])


def _random_tree(rng: RNG, depth: int) -> Any:
    if depth <= 0 or rng.random() < _TERMINAL_PROB:
        if rng.random() < 0.5:
            return ("x",)
        return ("c", rng.uniform(-_CONST_RANGE, _CONST_RANGE))
    if rng.random() < 0.6:
        op = rng.choice(_BINARY)
        return (op, _random_tree(rng, depth - 1), _random_tree(rng, depth - 1))
    op = rng.choice(_UNARY)
    return (op, _random_tree(rng, depth - 1))


def _paths(tree: Any) -> list[tuple[tuple[int, ...], Any]]:
    out: list[tuple[tuple[int, ...], Any]] = []

    def rec(t: Any, p: tuple[int, ...]) -> None:
        out.append((p, t))
        if isinstance(t, tuple) and t[0] in _ARITY:
            for i in range(1, len(t)):
                rec(t[i], p + (i,))

    rec(tree, ())
    return out


def _replace(tree: Any, path: tuple[int, ...], new: Any) -> Any:
    if not path:
        return new
    t = list(tree)
    t[path[0]] = _replace(t[path[0]], path[1:], new)
    return tuple(t)


def _random_expression(rng: RNG) -> Any:
    return _random_tree(rng, _MAX_DEPTH)


def _mutate_expression(tree: Any, rng: RNG) -> Any:
    nodes = _paths(tree)
    path, node = rng.choice(nodes)
    if isinstance(node, tuple) and node[0] == "c":
        # Perturb the constant instead of replacing the whole node sometimes.
        new_val = node[1] + rng.gauss(0.0, 0.5)
        return _replace(tree, path, ("c", max(-_CONST_RANGE, min(_CONST_RANGE, new_val))))
    return _replace(tree, path, _random_tree(rng, min(3, _MAX_DEPTH)))


def _crossover_expression(a: Any, b: Any, rng: RNG) -> tuple[Any, Any]:
    pa = _paths(a)
    pb = _paths(b)
    _, na = rng.choice(pa)
    _, nb = rng.choice(pb)
    # Prefer to swap a subtree; fall back to swapping whole trees on edges.
    return _replace(a, rng.choice(pa)[0], nb), _replace(b, rng.choice(pb)[0], na)


expression_spec = GenomeSpec(
    name="expression",
    random=_random_expression,
    mutate=_mutate_expression,
    crossover=_crossover_expression,
    to_str=expression_str,
)
