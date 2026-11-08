"""EVOLVE — evolutionary engine, objectives, meta-evolution, and JARVIS wiring."""

from __future__ import annotations

import pytest

from jarvis.evolve import (EvolutionEngine, RunConfig, ackley, evaluate_expression,
                           expression_str, expression_spec, griewank, minimize,
                           numeric_spec, rastrigin, rosenbrock, self_tune, sphere,
                           symbolic_regression)


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


# -------------------------------------------------------- progress hook -------

def test_on_generation_callback_fires():
    from jarvis.evolve import RunConfig, EvolutionEngine
    spec = numeric_spec([(-5.0, 5.0)] * 2)
    seen = []

    def hook(gen, stats):
        seen.append((gen, stats["best"]))

    cfg = RunConfig(population_size=20, generations=8, seed=0, on_generation=hook)
    EvolutionEngine(spec, minimize(sphere), cfg).run()
    # one call per generation (0..N), monotonic-ish best
    assert len(seen) == 9
    assert seen[-1][1] <= seen[0][1]


# -------------------------------------------------------- new benchmarks ------

def test_new_benchmark_minima():
    assert ackley([0.0, 0.0, 0.0]) == pytest.approx(0.0, abs=1e-12)
    assert rosenbrock([1.0, 1.0, 1.0]) == pytest.approx(0.0, abs=1e-12)
    assert griewank([0.0, 0.0, 0.0]) == pytest.approx(0.0, abs=1e-12)


def test_minimize_ackley_improves():
    spec = numeric_spec([(-32.768, 32.768)] * 3)
    cfg = RunConfig(population_size=60, generations=50, mutation_rate=0.3,
                    crossover_rate=0.8, elitism=2, seed=2)
    res = EvolutionEngine(spec, minimize(ackley), cfg).run()
    assert res.best.fitness < 2.0


# ------------------------------------------------------------- auto runner ----

def test_auto_runner_produces_report():
    from jarvis.evolve import auto_run
    events = []
    report = auto_run(minutes=2.0, on_progress=events.append)
    assert report["experiments_run"] >= 1
    assert report["rows"]
    assert any(r["experiment"].startswith("minimize-") for r in report["rows"])
    assert any(e["kind"] == "experiment_done" for e in events)


# -------------------------------------------------------- settings genome -----

def test_settings_space_sampling_and_mutation():
    from jarvis.evolve import Field, SettingsSpace, settings_spec
    import random
    space = SettingsSpace("t", {
        "n": Field("int", low=1, high=9),
        "r": Field("float", low=0.0, high=1.0),
        "b": Field("bool"),
        "c": Field("choice", choices=("a", "b", "c")),
    })
    spec = settings_spec(space)
    rng = random.Random(0)
    g = spec.random(rng)
    assert 1 <= g["n"] <= 9 and 0.0 <= g["r"] <= 1.0
    assert g["b"] in (True, False) and g["c"] in ("a", "b", "c")
    g2 = spec.mutate(g, rng)
    assert set(g2) == set(g)  # same keys, legal values
    assert 1 <= g2["n"] <= 9 and g2["c"] in ("a", "b", "c")
    a, b = spec.crossover(g, g2, rng)
    assert 1 <= a["n"] <= 9 and 1 <= b["n"] <= 9


def test_command_benchmark_measures_wall_time():
    from jarvis.evolve import command_benchmark
    fitness = command_benchmark(
        'python -c "import time; time.sleep(0.1 * {level})"', runs=1, warmup=0)
    fast = fitness({"level": 1})
    slow = fitness({"level": 4})
    assert fast < slow  # real wall-clock scaling (0.1s vs 0.4s)


def test_system_tuner_finds_fastest_command_parameter():
    from jarvis.evolve import SystemTuner, SettingsSpace, Field, command_benchmark
    space = SettingsSpace("sleep", {"level": Field("int", low=1, high=9)})
    fitness = command_benchmark(
        'python -c "import time; time.sleep(0.02 * {level})"', runs=1, warmup=0)
    tuner = SystemTuner(space, fitness)
    res = tuner.tune(generations=12, population=15, seed=0)
    assert res.best.genome["level"] <= 2  # converges to the fastest setting


def test_process_memory_and_tcp_latency_are_real():
    import os
    from jarvis.evolve import process_memory, tcp_latency
    mem = process_memory(os.getpid(), runs=1)  # this test process itself
    assert mem({}) > 0
    latency = tcp_latency("127.0.0.1", 8765, runs=1)  # core or closed port — either
    assert latency({}) == float("inf") or latency({}) < 5000.0


def test_tune_tools_registered_and_gated(kernel):
    from jarvis.tools import REGISTRY
    names = REGISTRY.names()
    assert "evolve.tune" in names and "evolve.tune_command" in names
    assert "evolve.tune" in kernel.config.policy.auto_allow          # measurement-only
    assert "evolve.tune_command" not in kernel.config.policy.auto_allow  # gated


@pytest.mark.asyncio
async def test_tune_command_service_and_tool(kernel):
    svc = kernel.services["evolve"]
    res = await svc.tune_command(
        'python -c "import time; time.sleep(0.02 * {level})"',
        low=1, high=9, runs=1, generations=10)
    assert res["kind"] == "tune:command"
    assert res["result"]["best"]["genome"]["level"] <= 2


# ------------------------------------------------- server targets & apply -----

def test_new_target_spaces_sample_legally():
    import random
    from jarvis.evolve import (stresstest_space, nginx_space, postgres_space,
                               jvm_space, settings_spec)
    rng = random.Random(1)
    for space in (stresstest_space(), nginx_space(), postgres_space(), jvm_space()):
        g = settings_spec(space).random(rng)
        for key, f in space.fields.items():
            assert key in g
            if f.kind == "int":
                assert f.low <= g[key] <= f.high
            elif f.kind == "choice":
                assert g[key] in f.choices


def test_minecraft_apply_backs_up_and_writes(tmp_path):
    from jarvis.evolve import minecraft_apply, minecraft_space
    props = tmp_path / "server.properties"
    props.write_text("view-distance=10\nsimulation-distance=8\nmotd=A Minecraft Server\n")
    apply = minecraft_apply(str(props))
    apply({"view-distance": 12, "simulation-distance": 6, "max-players": 30})
    text = props.read_text()
    assert "view-distance=12" in text
    assert "simulation-distance=6" in text
    assert "max-players=30" in text
    assert "motd=A Minecraft Server" in text        # untouched lines preserved
    backups = list(tmp_path.glob("server.properties.bak.*"))
    assert backups, "apply must back up the file first"


@pytest.mark.asyncio
async def test_auto_service_and_tool(kernel):
    from jarvis.tools import REGISTRY
    from jarvis.tools.base import ToolContext
    svc = kernel.services["evolve"]
    report = await svc.auto_run(minutes=1.0)
    assert report["experiments_run"] >= 1
    ctx = ToolContext(config=kernel.config, db=kernel.db, bus=kernel.bus,
                      services=kernel.services, user="owner")
    r = await REGISTRY.call("evolve.auto", ctx, minutes=1)
    assert r.ok and r.data["experiments_run"] >= 1


@pytest.mark.asyncio
async def test_evolve_agent_evolves_settings_and_restores(kernel, monkeypatch):
    class FakeReport:
        def __init__(self, s):
            self._s = s

        @property
        def score(self):
            return self._s

    class FakeHarness:
        """Deterministic fake: the optimum is formality 0.5."""

        def __init__(self, j):
            self.j = j

        async def run_task(self, spec):
            p = self.j.config.persona
            return FakeReport(max(0.0, 1.0 - abs(p.formality - 0.5) * 2.0))

    import jarvis.eval.harness as harness_mod
    monkeypatch.setattr(harness_mod, "EvalHarness", FakeHarness)
    svc = kernel.services["evolve"]
    report = await svc.evolve_agent(generations=4, population=6)
    # settings were restored afterwards
    assert report["settings_restored"] is True
    assert kernel.config.persona.formality == pytest.approx(0.85)
    # the evolution found settings near the fake optimum (score >= 0.5)
    assert report["best_score"] >= 0.5
    assert report["best_settings"]["persona_formality"] == pytest.approx(0.5, abs=0.3)
    assert set(report["tasks"]) == {"arithmetic_simple", "persona_intro", "conciseness"}


def test_evolve_self_tool_registered(kernel):
    from jarvis.tools import REGISTRY
    assert "evolve.self" in REGISTRY.names()
    assert "evolve.self" in kernel.config.policy.auto_allow


# ------------------------------------------------- hardware awareness ---------

def test_hardware_detect_and_tiers():
    from jarvis.evolve import detect, tier_of, recommended_workers, recommended_defaults
    prof = detect()
    assert prof.cpu_cores >= 1 and prof.ram_gb > 0
    assert prof.tier in ("constrained", "moderate", "capable", "high_end")
    assert tier_of(2, 3.0) == "constrained"
    assert tier_of(4, 8.0) == "moderate"
    assert tier_of(8, 16.0) == "capable"
    assert tier_of(16, 64.0, vram_gb=16) == "high_end"
    # workers are conservative: never more than half the cores
    assert recommended_workers(prof) <= max(1, prof.cpu_cores // 2)
    defaults = recommended_defaults(prof)
    assert defaults["parallel"] >= 1 and defaults["population"] >= 1


def test_parallel_engine_matches_serial():
    from jarvis.evolve import EvolutionEngine, RunConfig, numeric_spec, minimize, sphere
    spec = numeric_spec([(-5.0, 5.0)] * 3)
    fitness = minimize(sphere)
    base = RunConfig(population_size=30, generations=20, seed=42)
    serial = EvolutionEngine(spec, fitness, base).run()
    parallel = EvolutionEngine(spec, fitness,
                               RunConfig(population_size=30, generations=20,
                                         seed=42, parallel=4)).run()
    # parallel evaluation must not change the evolved result
    assert serial.best.fitness == parallel.best.fitness


def test_c_code_benchmark_compiles_and_times():
    import pytest
    from jarvis.evolve import (C_SUM_SOURCE, c_flags_space, code_benchmark_fitness,
                               compiler_available, SystemTuner)
    cc = "cc"
    if not compiler_available(cc):
        pytest.skip("no C compiler on PATH")
    fitness = code_benchmark_fitness(C_SUM_SOURCE, compiler=cc, runs=1)
    # -O3 is at least as fast as -O0 on this fixed workload
    fast = fitness({"opt": 3, "march": "native"})
    slow = fitness({"opt": 0, "march": "x86-64"})
    assert fast <= slow
    # the tuner converges to -O3
    tuner = SystemTuner(c_flags_space(), fitness, name="c-flags")
    res = tuner.tune(generations=6, population=6, seed=0)
    assert res.best.genome["opt"] >= 2


# ------------------------------------------------------------- games ---------

def test_tictactoe_rules():
    from jarvis.evolve import TicTacToe
    g = TicTacToe([1, 1, 1, -1, -1, 0, 0, 0, 0], player=1)
    assert g.winner() == 1 and g.terminal()
    assert g.features() and len(g.features()) == 9
    empty = TicTacToe()
    assert empty.moves() == list(range(9))


def test_greedy_move_takes_immediate_win():
    from jarvis.evolve import TicTacToe, greedy_move
    # X can win on the top row
    g = TicTacToe([1, 1, 0, -1, -1, 0, 0, 0, 0], player=1)
    assert greedy_move(g, [0.0] * 9) == 2


def test_evolved_heuristic_beats_random():
    from jarvis.evolve import TicTacToe, evolve_heuristic, evaluate_against_random
    res = evolve_heuristic(TicTacToe, generations=15, population=30,
                           games_per_opponent=4, seed=3)
    report = evaluate_against_random(res.best.genome, TicTacToe, games=80, seed=7)
    # the evolved player must win the majority against a random mover
    assert report["win_rate"] >= 0.6


def test_connect4_playable():
    from jarvis.evolve import Connect4, greedy_move, play_game
    w = [0.1] * len(Connect4().features())
    r = play_game(w, w, Connect4)
    assert r in (-1, 0, 1)
    assert greedy_move(Connect4(), w) in range(7)


def test_cluster_profile_lists_local():
    from jarvis.evolve import cluster_profile
    cluster = cluster_profile()
    assert cluster and cluster[0]["reachable"] is True
    assert any(m.get("name") in ("pc-001", "apos", "laptop") for m in cluster)


# ------------------------------------------------------------- wiring -------

def test_evolve_service_is_wired(kernel):
    assert kernel.services.get("evolve") is not None


def test_evolve_tools_registered(kernel):
    from jarvis.tools import REGISTRY
    names = REGISTRY.names()
    for n in ("evolve.symbolic", "evolve.minimize", "evolve.selftune", "evolve.status"):
        assert n in names


def test_evolve_tools_auto_allowed(cfg):
    # Evolve tools are read-only computation (SideEffect.NONE) and must be
    # callable by the agent without an approval prompt.
    for n in ("evolve.symbolic", "evolve.minimize", "evolve.selftune", "evolve.status"):
        assert n in cfg.policy.auto_allow


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
