# EVOLVE — evolutionary intelligence engine

A self-contained, deterministic evolutionary optimizer that JARVIS can run and
that can evolve **itself**. No heavy dependencies (stdlib + optional numpy).

## What it does

- **Evolve solutions** — numeric vectors and symbolic expression trees.
- **Symbolic regression** — given `(x, y)` data, discover the formula `f(x)`.
- **Numeric optimization** — minimize/maximize functions over bounded spaces.
- **Meta-evolution** — evolve the engine's *own* configuration (population,
  mutation, selection…) against a benchmark, i.e. bounded self-improvement.

## The loop

```
initialise population → evaluate fitness → select parents (elitism + diversity)
→ crossover + mutate → evaluate offspring → archive best → repeat (bounded)
```

Lower fitness is better; every run is deterministic under a seed and bounded by
generation / wall-clock / stagnation limits.

## Usage

```python
from jarvis.evolve import (
    EvolutionEngine, RunConfig, expression_spec, symbolic_regression,
    numeric_spec, minimize, sphere, self_tune,
)

# 1. Symbolic regression: recover f(x) = 2x + 1 from sample points.
xs = [0, 1, 2, 3]; ys = [1, 3, 5, 7]
fit = symbolic_regression(xs, ys)
res = EvolutionEngine(
    expression_spec, fit,
    RunConfig(population_size=150, generations=80, seed=5),
).run()
print(res.best.genome)   # an expression tree

# 2. Numeric optimization: minimise the sphere function.
res = EvolutionEngine(
    numeric_spec([(-5, 5)] * 4), minimize(sphere),
    RunConfig(population_size=60, generations=60, seed=1),
).run()

# 3. Self-evolve: tune the engine's own config on a benchmark.
res = self_tune(numeric_spec([(-5, 5)] * 2), sphere)
print(res.best.genome)   # the winning RunConfig dict
```

## JARVIS tools

`evolve.symbolic`, `evolve.minimize`, `evolve.selftune`, `evolve.status` — all
read-only (`SideEffect.NONE`), bounded, audited, and recorded via the service.

## Safety

EVOLVE evolves *candidates*, never JARVIS's trusted control plane. It has no
permission to change tools, policy, sandbox, or the verifier — its output is
data, gated and audited like any other result.
