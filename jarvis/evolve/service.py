"""EVOLVE — the JARVIS service layer.

Exposes the engine to JARVIS as a callable, bounded, audited subsystem. Runs are
CPU-bound and executed off the event loop via ``asyncio.to_thread``; results are
persisted (``kv``) and audited, and the last run is published on the bus so the
HUD/other agents can observe it.

This is the *untrusted-sandbox* side of EVOLVE: the engine may evolve anything,
but the service never changes JARVIS's own policy, tools, or verification layer.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from ..ids import new_id
from ..logging_setup import get_logger
from evolve.engine import EvolutionEngine, RunConfig, RunResult
from evolve.genome import expression_spec, numeric_spec
from evolve.meta import self_tune
from evolve.objectives import rastrigin, sphere, symbolic_regression

log = get_logger("jarvis.evolve")

__all__ = ["EvolveService"]

_BENCHMARKS = {"sphere": sphere, "rastrigin": rastrigin}

_LAST_KEY = "evolve.last"


class EvolveService:
    """Evolve subsystem: runs and records evolutionary experiments."""

    def __init__(self, jarvis: Any = None) -> None:
        self.j = jarvis
        self.db = getattr(jarvis, "db", None)
        self.bus = getattr(jarvis, "bus", None)
        self._tasks: dict[str, dict[str, Any]] = {}
        self._mem: tuple[Any, Any, Any] | None = None

    # -- helpers ----------------------------------------------------------
    def _record(self, result: RunResult, kind: str) -> dict[str, Any]:
        summary = {"kind": kind, "result": result.to_dict()}
        if self.db is not None:
            try:
                self.db.kv_set(_LAST_KEY, summary)
                self.db.audit("evolve.run", actor="evolve", target=kind,
                              detail={"best_fitness": result.best.fitness,
                                      "generations": result.generations_run})
            except Exception as exc:  # pragma: no cover - recording must not break the run
                log.warning("evolve record failed: %s", exc)
        if self.bus is not None:
            try:
                self.bus.publish("evolve.completed", kind=kind,
                                 best_fitness=result.best.fitness,
                                 generations=result.generations_run)
            except Exception as exc:  # pragma: no cover
                log.debug("evolve bus publish failed: %s", exc)
        return summary

    # -- public operations ------------------------------------------------
    async def symbolic_regression(self, xs: list[float], ys: list[float], *,
                                  generations: int = 50, population: int = 100,
                                  seed: int = 0, max_seconds: float = 30.0) -> dict[str, Any]:
        """Evolve an expression ``f(x)`` matching the given sample points."""
        if len(xs) != len(ys) or not xs:
            return {"ok": False, "error": "xs and ys must be matching, non-empty lists"}
        if generations > 500 or population > 2000:
            return {"ok": False, "error": "budget too large (max 500 gens / 2000 pop)"}

        def _run() -> RunResult:
            fitness = symbolic_regression(xs, ys)
            cfg = RunConfig(population_size=population, generations=generations,
                            mutation_rate=0.3, crossover_rate=0.85, elitism=2,
                            tournament_size=3, selection="tournament", seed=seed,
                            max_seconds=max_seconds, stagnation_generations=20)
            return EvolutionEngine(expression_spec, fitness, cfg).run()

        result = await asyncio.to_thread(_run)
        return self._record(result, "symbolic_regression")

    async def minimize(self, function: str, *, dimensions: int = 4,
                       bounds: tuple[float, float] = (-5.0, 5.0),
                       generations: int = 60, population: int = 60,
                       seed: int = 0) -> dict[str, Any]:
        """Minimise a named numeric benchmark (``sphere`` or ``rastrigin``)."""
        fn = _BENCHMARKS.get(function)
        if fn is None:
            return {"ok": False, "error": f"unknown function {function!r} (sphere|rastrigin)"}
        if not 1 <= dimensions <= 50:
            return {"ok": False, "error": "dimensions must be 1..50"}
        spec = numeric_spec([bounds for _ in range(dimensions)])

        def _run() -> RunResult:
            cfg = RunConfig(population_size=population, generations=generations,
                            mutation_rate=0.3, crossover_rate=0.8, elitism=2,
                            tournament_size=3, seed=seed, max_seconds=60.0,
                            stagnation_generations=20)
            return EvolutionEngine(spec, fn, cfg).run()

        result = await asyncio.to_thread(_run)
        return self._record(result, f"minimize:{function}")

    async def evolve_itself(self, *, task: str = "sphere", dimensions: int = 3,
                            meta_generations: int = 10, max_seconds: float = 180.0
                            ) -> dict[str, Any]:
        """Evolve the engine's own configuration on a fixed benchmark task."""
        fn = _BENCHMARKS.get(task)
        if fn is None:
            return {"ok": False, "error": f"unknown task {task!r}"}
        spec = numeric_spec([(-5.0, 5.0) for _ in range(max(1, dimensions))])

        def _run() -> RunResult:
            return self_tune(spec, fn, meta_generations=meta_generations,
                             task_generations=20, task_population=30,
                             eval_seeds=(0, 1), max_seconds=max_seconds)

        result = await asyncio.to_thread(_run)
        return self._record(result, "self_tune")

    def status(self) -> dict[str, Any]:
        """The last run's summary, or an empty report if none has run."""
        if self.db is None:
            return {"available": False, "reason": "no database"}
        try:
            data = self.db.kv_get(_LAST_KEY, None)
        except Exception:  # pragma: no cover
            return {"available": False, "reason": "unreadable"}
        if not data:
            return {"available": True, "last": None}
        return {"available": True, "last": json.loads(json.dumps(data, default=str))}

    # -- real-system tuning ----------------------------------------------
    async def tune_ollama(self, *, generations: int = 8, population: int = 10,
                          prompt: str = "Say OK.") -> dict[str, Any]:
        """Tune Ollama request settings by measuring real inference latency.

        Expensive: every candidate is a real model call, so budgets are small
        by design. Read-only (measurement only; nothing is written).
        """
        from evolve.measure import ollama_latency
        from evolve.tuner import SystemTuner, ollama_space
        llm = getattr(getattr(self.j, "config", None), "llm", None)
        if llm is None:
            return {"ok": False, "error": "no llm config"}
        fitness = ollama_latency(llm.base_url, llm.model, prompt, runs=1, warmup=0)
        tuner = SystemTuner(ollama_space(), fitness, name="ollama")

        def _run() -> RunResult:
            return tuner.tune(generations=generations, population=population,
                              max_seconds=600.0)

        result = await asyncio.to_thread(_run)
        return self._record(result, "tune:ollama")

    async def tune_command(self, template: str, *, low: int, high: int,
                           runs: int = 2, generations: int = 15) -> dict[str, Any]:
        """Evolve one integer parameter (``{level}``) of a shell command by
        measuring its wall time. Runs the command repeatedly — bounded."""
        from evolve.measure import command_benchmark
        from evolve.settings import Field, SettingsSpace
        from evolve.tuner import SystemTuner
        if "{level}" not in template:
            return {"ok": False, "error": "template must contain a {level} placeholder"}
        if not 1 <= low < high <= 10000:
            return {"ok": False, "error": "need 1 <= low < high <= 10000"}
        space = SettingsSpace("command", {"level": Field("int", low=int(low), high=int(high))})
        fitness = command_benchmark(template, runs=runs)
        tuner = SystemTuner(space, fitness, name="command")

        def _run() -> RunResult:
            return tuner.tune(generations=generations, population=20,
                              max_seconds=600.0)

        result = await asyncio.to_thread(_run)
        return self._record(result, "tune:command")

    async def auto_run(self, minutes: float = 5.0) -> dict[str, Any]:
        """Run the bounded automatic experiment queue and return its report."""
        from evolve.auto import auto_run as _auto

        def _run() -> dict[str, Any]:
            return _auto(minutes=minutes)

        report = await asyncio.to_thread(_run)
        if self.db is not None:
            try:
                self.db.kv_set(_LAST_KEY, {"kind": "auto", "report": report})
                self.db.audit("evolve.auto", actor="evolve", target="auto",
                              detail={"experiments": report.get("experiments_run"),
                                      "wall_seconds": report.get("wall_seconds")})
            except Exception as exc:  # pragma: no cover
                log.warning("evolve auto record failed: %s", exc)
        if self.bus is not None:
            try:
                self.bus.publish("evolve.completed", kind="auto",
                                 experiments=report.get("experiments_run"))
            except Exception as exc:  # pragma: no cover
                log.debug("evolve bus publish failed: %s", exc)
        return report

    # -- evolve the agent itself -----------------------------------------
    async def evolve_agent(self, *, generations: int = 4, population: int = 6,
                           task_ids: list[str] | None = None,
                           max_seconds: float = 1800.0) -> dict[str, Any]:
        """Evolve the agent's own behaviour against its held-out eval score.

        Genome = the agent's persona + LLM knobs (:func:`evolve.agent_space`);
        fitness = mean deterministic-oracle score over an eval-suite subset
        (lower cost = better behaviour). Original settings are restored after
        the run — the evolved settings are returned as *data*, never silently
        applied. Bounded (small populations, stagnation stop, wall-clock cap).
        """
        import random as _random
        import time as _time
        from evolve import settings_spec
        from evolve.tuner import agent_space
        from ..eval.harness import EvalHarness, load_suite

        space = agent_space()
        spec = settings_spec(space)
        harness = EvalHarness(self.j)
        suite = load_suite("core")
        if task_ids is None:
            task_ids = ["arithmetic_simple", "persona_intro", "conciseness"]
        tasks = [t for t in suite.tasks if t.id in task_ids]
        if not tasks:
            return {"ok": False, "error": "no matching eval tasks"}

        persona = self.j.config.persona
        llm = self.j.config.llm
        saved = {"formality": persona.formality, "humor": persona.humor,
                 "verbosity": persona.verbosity, "temperature": llm.temperature,
                 "top_p": llm.top_p, "num_predict": llm.num_predict}

        def apply(g: dict[str, Any]) -> None:
            persona.formality = float(g["persona_formality"])
            persona.humor = float(g["persona_humor"])
            persona.verbosity = float(g["persona_verbosity"])
            llm.temperature = float(g["llm_temperature"])
            llm.top_p = float(g["llm_top_p"])
            llm.num_predict = int(g["llm_num_predict"])

        def restore() -> None:
            persona.formality = saved["formality"]
            persona.humor = saved["humor"]
            persona.verbosity = saved["verbosity"]
            llm.temperature = saved["temperature"]
            llm.top_p = saved["top_p"]
            llm.num_predict = saved["num_predict"]

        async def fitness(g: dict[str, Any]) -> float:
            apply(g)
            try:
                total = 0.0
                for t in tasks:
                    report = await harness.run_task(t)
                    total += report.score  # TaskReport.score is a property
                return -total / len(tasks)
            except Exception:
                return float("inf")

        rng = _random.Random(0)
        t0 = _time.perf_counter()
        pop_size = int(population)
        pop = [spec.random(rng) for _ in range(pop_size)]
        fits = [await fitness(g) for g in pop]
        best_g, best_f = min(zip(pop, fits), key=lambda x: x[1])
        history = [{"generation": 0, "best": best_f, "mean": sum(fits) / len(fits)}]
        ran = 0
        try:
            for gen in range(1, generations + 1):
                ran = gen
                if (_time.perf_counter() - t0) > max_seconds:
                    break
                ranked = sorted(zip(pop, fits), key=lambda x: x[1])
                next_pop = [g for g, _ in ranked[:2]]  # elitism 2
                while len(next_pop) < pop_size:
                    p1 = ranked[rng.randrange(min(3, len(ranked)))][0]
                    p2 = ranked[rng.randrange(min(3, len(ranked)))][0]
                    c1, c2 = spec.crossover(p1, p2, rng)
                    if rng.random() < 0.35:
                        c1 = spec.mutate(c1, rng)
                    if rng.random() < 0.35:
                        c2 = spec.mutate(c2, rng)
                    next_pop.append(c1)
                    if len(next_pop) < pop_size:
                        next_pop.append(c2)
                pop = next_pop
                fits = [await fitness(g) for g in pop]
                nb_g, nb_f = min(zip(pop, fits), key=lambda x: x[1])
                history.append({"generation": gen, "best": nb_f,
                                "mean": sum(fits) / len(fits)})
                if nb_f >= best_f:
                    break  # stagnation — no improvement
                best_g, best_f = nb_g, nb_f
        finally:
            restore()

        report = {
            "kind": "evolve.self",
            "best_settings": best_g,
            "best_score": round(-best_f, 4),
            "tasks": [t.id for t in tasks],
            "generations": ran,
            "history": history,
            "settings_restored": True,
        }
        if self.db is not None:
            try:
                self.db.kv_set(_LAST_KEY, report)
                self.db.audit("evolve.self", actor="evolve", target="agent",
                              detail={"best_score": report["best_score"],
                                      "tasks": report["tasks"]})
            except Exception as exc:  # pragma: no cover
                log.warning("evolve.self record failed: %s", exc)
        if self.bus is not None:
            try:
                self.bus.publish("evolve.completed", kind="evolve.self",
                                 best_score=report["best_score"])
            except Exception:  # pragma: no cover
                pass
        return report

    # -- Phase C: task-based learning interface (JARVIS is a client) -----
    def _memory(self) -> tuple[Any, Any, Any]:
        """Lazily build the durable memory (SQLite + artifacts + model registry)."""
        if self._mem is None:
            from evolve.memory import MemoryStore, ArtifactStore, ModelRegistry
            data_dir = Path(self.j.config.data_path)
            data_dir.mkdir(parents=True, exist_ok=True)
            store = MemoryStore(str(data_dir / "evolve-memory.db"))
            artifacts = ArtifactStore(str(data_dir / "evolve-artifacts"))
            self._mem = (store, artifacts, ModelRegistry(store, artifacts))
        return self._mem

    async def train(self, *, env: str = "CartPole-v1",
                    algorithm: str = "dqn", steps: int = 30000) -> dict[str, Any]:
        """Start a training job; returns a task id immediately."""
        try:
            from evolve.learn import DQN, PPO, env_shapes, train as _train
        except ImportError:
            return {"ok": False,
                    "error": "learning engine requires torch + gymnasium on this host"}
        task_id = new_id("evt")
        cancel = threading.Event()
        self._tasks[task_id] = {"kind": "train", "env": env, "algorithm": algorithm,
                                "status": "running", "cancel": cancel, "result": None}

        def work() -> None:
            try:
                obs_dim, act_dim = env_shapes(env)
                agent = (DQN(obs_dim, act_dim) if algorithm == "dqn"
                         else PPO(obs_dim, act_dim))
                result = _train(agent, env, steps, algorithm=algorithm,
                                eval_interval=max(2000, steps // 10),
                                eval_episodes=5, eval_seed=100,
                                max_seconds=1800.0, should_stop=cancel.is_set)
                rec = None
                if not cancel.is_set():
                    _, _, registry = self._memory()
                    rec = registry.save_model(
                        agent, name=task_id,
                        meta={"env": env, "algorithm": algorithm})
                self._tasks[task_id]["status"] = ("cancelled" if cancel.is_set()
                                                  else "done")
                self._tasks[task_id]["result"] = {
                    "result": result.to_dict(),
                    "model_id": rec["model_id"] if rec else None}
                self._publish_learn("train.done", task_id,
                                    final=result.final_eval_mean)
            except Exception as exc:  # pragma: no cover
                self._tasks[task_id]["status"] = "failed"
                self._tasks[task_id]["result"] = {
                    "error": f"{type(exc).__name__}: {exc}"}

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "task_id": task_id, "status": "running"}

    def _publish_learn(self, topic: str, task_id: str, **data: Any) -> None:
        if self.bus is not None:
            try:
                self.bus.publish(f"evolve.{topic}", task_id=task_id, **data)
            except Exception:  # pragma: no cover
                pass

    async def get_status(self, task_id: str) -> dict[str, Any]:
        t = self._tasks.get(task_id)
        if t is None:
            return {"ok": False, "error": f"unknown task {task_id}"}
        return {"ok": True, "task_id": task_id, "kind": t.get("kind"),
                "status": t["status"], "result": t.get("result")}

    async def cancel(self, task_id: str) -> dict[str, Any]:
        t = self._tasks.get(task_id)
        if t is None:
            return {"ok": False, "error": f"unknown task {task_id}"}
        if t["status"] != "running":
            return {"ok": True, "task_id": task_id, "status": t["status"],
                    "cancelled": False}
        t["cancel"].set()
        return {"ok": True, "task_id": task_id, "cancelled": True}

    def list_models(self) -> list[dict[str, Any]]:
        try:
            _, _, registry = self._memory()
            return registry.list()
        except Exception as exc:  # pragma: no cover
            return [{"error": str(exc)}]

    async def evaluate_model(self, model_id: int, *,
                             benchmark: str = "CartPole-v1",
                             episodes: int = 10) -> dict[str, Any]:
        try:
            from evolve.learn import DQN, PPO, env_shapes, evaluate
        except ImportError:
            return {"ok": False,
                    "error": "learning engine requires torch + gymnasium on this host"}
        _, _, registry = self._memory()
        rec = registry.get(model_id)
        if rec is None:
            return {"ok": False, "error": f"no model {model_id}"}
        meta = json.loads(rec["meta"])
        obs_dim, act_dim = env_shapes(benchmark)

        def factory():
            return (DQN(obs_dim, act_dim) if meta.get("algorithm") == "dqn"
                    else PPO(obs_dim, act_dim))

        agent = registry.load_agent(model_id, factory)
        mean = evaluate(agent, benchmark, episodes, seed=200)
        return {"ok": True, "model_id": model_id, "benchmark": benchmark,
                "mean_reward": round(mean, 2), "episodes": episodes}

    async def compare(self, *, envs: list[str] | None = None,
                      steps: int = 8000, seeds: int = 3) -> dict[str, Any]:
        try:
            from evolve.learn import compare_algorithms
        except ImportError:
            return {"ok": False,
                    "error": "learning engine requires torch + gymnasium on this host"}
        env_names = tuple(envs) if envs else None

        def work() -> dict[str, Any]:
            return compare_algorithms(envs=env_names, steps=steps, seeds=seeds)

        return await asyncio.to_thread(work)
