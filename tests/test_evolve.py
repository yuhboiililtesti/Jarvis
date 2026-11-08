"""EVOLVE — evolutionary engine, objectives, meta-evolution, and JARVIS wiring."""

from __future__ import annotations

import pytest

from jarvis.evolve import (EvolutionEngine, RunConfig, evaluate_expression,
                           expression_str, expression_spec, minimize, numeric_spec,
                           rastrigin, self_tune, sphere, symbolic_regression)


# ------------------------------------------------------------- expression ----

def test_evaluate_expression_basics():
    # 2x + 1
    expr = ("+", ("*", ("c", 2.0), ("x",)), ("c", 1.0))
    assert evaluate_expression(expr, 3.0) == pytest.approx(7.0)
    assert expression_str(expr) == "((2 * x) + 1)"


def test_expression_protected_division():
    # x / x  -> 1 except at x=0 where it is guarded, not NaN/exception
    expr = ("/", ("x",), ("x",))
    assert evaluate_expression(expr, 4.0) == pytest.approx(1.0)
    assert abs(evaluate_expression(expr, 0.0)) > 1e5  # protected, finite


# ------------------------------------------------------------- numeric ------

def test_numeric_minimize_sphere_converges():
    spec = numeric_spec([(-5.0, 5.0)] * 3)
    cfg = RunConfig(population_size=60, generations=60, mutation_rate=0.3,
                    crossover_rate=0.8, elitism=2, tournament_size=3, seed=7)
    res = EvolutionEngine(spec, minimize(sphere), cfg).run()
    # sphere minimum is 0; evolution should get close.
    assert res.best.fitness < 0.01


def test_numeric_minimize_rastrigin_improves():
    spec = numeric_spec([(-5.12, 5.12)] * 2)
    cfg = RunConfig(population_size=80, generations=60, mutation_rate=0.3,
                    crossover_rate=0.8, elitism=3, seed=3)
    res = EvolutionEngine(spec, minimize(rastrigin), cfg).run()
    assert res.best.fitness < 5.0  # well below a random start's typical cost


def test_engine_is_deterministic():
    spec = numeric_spec([(-5.0, 5.0)] * 4)
    fitness = minimize(sphere)
    cfg = RunConfig(population_size=40, generations=30, seed=123)
    a = EvolutionEngine(spec, fitness, cfg).run()
    b = EvolutionEngine(spec, fitness, cfg).run()
    assert a.best.fitness == b.best.fitness
    assert a.generations_run == b.generations_run


def test_engine_is_bounded_by_generations_and_history():
    spec = numeric_spec([(-5.0, 5.0)] * 2)
    cfg = RunConfig(population_size=20, generations=10, seed=0)
    res = EvolutionEngine(spec, minimize(sphere), cfg).run()
    assert res.generations_run <= 10
    assert len(res.history) == res.generations_run + 1  # includes generation 0
    assert res.history[0]["generation"] == 0


# ------------------------------------------------------------- symbolic ------

def test_symbolic_regression_recovers_linear_target():
    # Target: f(x) = 2x + 1 over sample points.
    xs = [-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0]
    ys = [2.0 * x + 1.0 for x in xs]
    fitness = symbolic_regression(xs, ys)
    cfg = RunConfig(population_size=120, generations=80, mutation_rate=0.35,
                    crossover_rate=0.9, elitism=2, tournament_size=3, seed=11,
                    max_seconds=30.0, stagnation_generations=25)
    res = EvolutionEngine(expression_spec, fitness, cfg).run()
    # MSE of a good fit is tiny; assert convergence to a low error.
    assert res.best.fitness < 0.05


# ------------------------------------------------------------- meta ---------

def test_self_tune_returns_a_config():
    spec = numeric_spec([(-5.0, 5.0)] * 2)
    res = self_tune(spec, sphere, meta_generations=3, meta_population=6,
                    task_generations=8, task_population=15, eval_seeds=(0,),
                    max_seconds=60.0)
    cfg = res.best.genome
    assert isinstance(cfg, dict)
    assert "population_size" in cfg and "selection" in cfg and "mutation_rate" in cfg
    assert res.best.fitness != float("inf")


# ------------------------------------------------------------- wiring -------

def test_evolve_service_is_wired(kernel):
    assert kernel.services.get("evolve") is not None


def test_evolve_tools_registered(kernel):
    from jarvis.tools import REGISTRY
    names = REGISTRY.names()
    for n in ("evolve.symbolic", "evolve.minimize", "evolve.selftune", "evolve.status"):
        assert n in names


@pytest.mark.asyncio
async def test_evolve_service_minimize(kernel):
    svc = kernel.services["evolve"]
    res = await svc.minimize("sphere", dimensions=3, generations=25, population=40)
    assert res["kind"] == "minimize:sphere"
    assert res["result"]["best"]["fitness"] < 0.05


@pytest.mark.asyncio
async def test_evolve_service_symbolic(kernel):
    svc = kernel.services["evolve"]
    xs = [0.0, 1.0, 2.0, 3.0]
    ys = [3.0, 5.0, 7.0, 9.0]  # y = 2x + 3
    res = await svc.symbolic_regression(xs, ys, generations=50, population=80)
    assert res["kind"] == "symbolic_regression"
    assert res["result"]["best"]["fitness"] < 0.05


@pytest.mark.asyncio
async def test_evolve_tool_via_registry(kernel):
    from jarvis.tools import REGISTRY
    from jarvis.tools.base import ToolContext
    ctx = ToolContext(config=kernel.config, db=kernel.db, bus=kernel.bus,
                      services=kernel.services, user="owner")
    r = await REGISTRY.call("evolve.minimize", ctx, function="sphere", dimensions=2,
                            generations=20)
    assert r.ok, r.error
    assert "best_cost" in r.data
