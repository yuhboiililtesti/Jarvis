"""EVOLVE — an evolutionary intelligence engine JARVIS can run and evolve.

A self-contained, deterministic evolutionary optimizer: it evolves numeric
vectors, symbolic expression trees, and (via meta-evolution) its own search
configuration. See :mod:`jarvis.evolve.engine` for the core loop.
"""

from .engine import EvolutionEngine, Individual, RunConfig, RunResult
from .genome import (GenomeSpec, evaluate_expression, expression_spec,
                     expression_str, numeric_spec)
from .meta import CONFIG_SPACE, config_spec, self_tune
from .objectives import (ackley, griewank, maximize, minimize, rastrigin,
                         rosenbrock, sphere, symbolic_regression)
from .settings import Field, SettingsSpace, settings_spec
from .measure import command_benchmark, ollama_latency, process_memory, tcp_latency
from .tuner import (SystemTuner, ollama_space, minecraft_space, gzip_space,
                    build_jobs_space)
from .auto import AutoRunner, auto_run
from .service import EvolveService

__all__ = [
    "EvolutionEngine", "Individual", "RunConfig", "RunResult",
    "GenomeSpec", "numeric_spec", "expression_spec", "evaluate_expression",
    "expression_str",
    "CONFIG_SPACE", "config_spec", "self_tune",
    "symbolic_regression", "minimize", "maximize", "sphere", "rastrigin",
    "ackley", "rosenbrock", "griewank",
    "Field", "SettingsSpace", "settings_spec",
    "command_benchmark", "ollama_latency", "process_memory", "tcp_latency",
    "SystemTuner", "ollama_space", "minecraft_space", "gzip_space",
    "build_jobs_space",
    "AutoRunner", "auto_run",
    "EvolveService",
]
