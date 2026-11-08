"""EVOLVE — measurement adapters: fitness from the *real* world.

Each adapter returns a fitness function that measures an actual system, so
EVOLVE can optimise servers by evolving their settings against observed metrics
instead of synthetic functions:

* :func:`command_benchmark` — wall-clock time of a shell command (build/coding
  servers, compression, batch jobs). The command template references the genome
  keys, e.g. ``"gzip -{level} -c data.bin > /dev/null"``.
* :func:`ollama_latency` — real LLM inference latency for request settings
  (``num_ctx`` / ``num_predict`` / ``temperature``).
* :func:`process_memory` — resident memory of a running process (game servers).
* :func:`tcp_latency` — connect latency to a host:port (server reachability).

All measurements are time-bound and exception-safe: a failed measurement
returns ``+inf`` (the candidate dies) rather than crashing the run.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Callable

__all__ = ["command_benchmark", "ollama_latency", "process_memory", "tcp_latency"]

Fitness = Callable[[dict[str, Any]], float]


def _safe_measure(fn: Callable[[], float]) -> float:
    try:
        return float(fn())
    except Exception:
        return float("inf")


def command_benchmark(template: str, *, runs: int = 3, timeout: float = 120.0,
                      warmup: int = 1) -> Fitness:
    """Mean wall-clock seconds of ``template.format(**settings)`` (lower better)."""

    def fitness(settings: dict[str, Any]) -> float:
        try:
            cmd = template.format(**settings)
        except (KeyError, ValueError):
            return float("inf")
        times: list[float] = []

        def once() -> float:
            t0 = time.perf_counter()
            proc = subprocess.run(cmd, shell=True, capture_output=True,
                                  timeout=timeout)
            if proc.returncode != 0:
                raise RuntimeError(f"command exited {proc.returncode}")
            return time.perf_counter() - t0

        for _ in range(warmup):
            if _safe_measure(once) == float("inf"):
                return float("inf")
        for _ in range(runs):
            times.append(_safe_measure(once))
        return sum(times) / len(times) if times else float("inf")

    return fitness


def ollama_latency(base_url: str, model: str, prompt: str, *, runs: int = 2,
                   warmup: int = 1) -> Fitness:
    """Mean wall-clock seconds of a real Ollama chat (lower better).

    The genome keys are Ollama request options: ``num_ctx``, ``num_predict``,
    ``temperature``, ``top_p`` (any subset)."""
    import json
    import httpx

    def once(settings: dict[str, Any]) -> float:
        options = {k: v for k, v in settings.items()}
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": options,
        }
        t0 = time.perf_counter()
        with httpx.Client(timeout=300.0) as client:
            r = client.post(f"{base_url.rstrip('/')}/api/generate", json=payload)
            r.raise_for_status()
            json.loads(r.text)
        return time.perf_counter() - t0

    def fitness(settings: dict[str, Any]) -> float:
        for _ in range(warmup):
            if _safe_measure(lambda: once(settings)) == float("inf"):
                return float("inf")
        times = [_safe_measure(lambda: once(settings)) for _ in range(runs)]
        return sum(times) / len(times) if times else float("inf")

    return fitness


def process_memory(pid: int, *, kind: str = "rss", runs: int = 2,
                   interval_s: float = 0.2) -> Fitness:
    """Mean memory usage of a live process, in MiB (lower better).

    ``kind`` is a ``psutil`` memory attribute (``rss``, ``uss``, ``vms``). Use
    with a settings space over that process's configuration; the measurement
    itself needs no server access beyond ``psutil``.
    """
    import psutil

    def once(_settings: dict[str, Any]) -> float:
        info = getattr(psutil.Process(pid).memory_info(), kind, None)
        if info is None:
            raise RuntimeError(f"no memory metric {kind!r}")
        return float(info) / (1024.0 * 1024.0)

    def fitness(settings: dict[str, Any]) -> float:
        samples = []
        for _ in range(runs):
            samples.append(_safe_measure(lambda: once(settings)))
            time.sleep(interval_s)
        return sum(samples) / len(samples) if samples else float("inf")

    return fitness


def tcp_latency(host: str, port: int, *, runs: int = 3, timeout: float = 5.0) -> Fitness:
    """Mean TCP connect latency in milliseconds (lower better)."""
    import socket

    def once(_settings: dict[str, Any]) -> float:
        t0 = time.perf_counter()
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return (time.perf_counter() - t0) * 1000.0

    def fitness(settings: dict[str, Any]) -> float:
        times = [_safe_measure(lambda: once(settings)) for _ in range(runs)]
        return sum(times) / len(times) if times else float("inf")

    return fitness
