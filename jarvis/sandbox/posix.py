"""POSIX containment tier: resource limits and a killable process group.

On Linux/macOS the child is started in a new session (its own process group) and
its rlimits are lowered before ``exec``, so it cannot raise them itself:

  * ``RLIMIT_CPU``      — CPU-time ceiling;
  * ``RLIMIT_AS``       — address-space (memory) ceiling;
  * ``RLIMIT_FSIZE``    — largest file it may write;
  * ``RLIMIT_CORE``     — no core dumps.

On timeout the whole process group is sent ``SIGKILL``, so children die with the
parent. Process-count capping is deliberately **not** attempted: ``RLIMIT_NPROC``
is per-*user*, so a low value stops the child from starting at all on a normal
desktop session. That limitation is reported, not hidden.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Any

from ..logging_setup import get_logger
from .limits import Limits
from .result import SandboxResult, SandboxUnavailable

log = get_logger("jarvis.sandbox.posix")

__all__ = ["available", "run_with_limits", "capabilities"]

TIER = "rlimit"


def _resource() -> Any:
    import resource  # POSIX-only
    return resource


def available() -> tuple[bool, str]:
    if os.name == "nt":
        return False, f"POSIX rlimits are not available on Windows (this is {sys.platform})"
    try:
        _resource()
    except Exception as exc:  # pragma: no cover - unusual interpreter
        return False, f"the resource module is unavailable: {exc}"
    return True, "POSIX rlimits (CPU/memory/file-size) with a killable process group"


def capabilities() -> list[str]:
    return ["kill-tree", "memory-cap", "cpu-time-cap", "file-size-cap"]


def _apply(limits: Limits):
    res = _resource()

    def _preexec() -> None:  # runs in the child, after fork, before exec
        try:
            if limits.max_cpu_seconds > 0:
                res.setrlimit(res.RLIMIT_CPU,
                              (limits.max_cpu_seconds, limits.max_cpu_seconds + 1))
            if limits.max_memory_mb > 0:
                b = int(limits.max_memory_mb) * 1024 * 1024
                res.setrlimit(res.RLIMIT_AS, (b, b))
            if limits.max_file_mb > 0:
                b = int(limits.max_file_mb) * 1024 * 1024
                res.setrlimit(res.RLIMIT_FSIZE, (b, b))
            res.setrlimit(res.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):  # a limit that cannot be set is reported, not fatal
            pass

    return _preexec


def run_with_limits(argv: list[str], *, cwd: str | None = None,
                    env: dict[str, str] | None = None, timeout: float = 30.0,
                    limits: Limits | None = None, stdin_text: str | None = None,
                    ) -> SandboxResult:
    limits = limits or Limits()
    ok, reason = available()
    if not ok:
        raise SandboxUnavailable(reason)

    t0 = time.perf_counter()
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=bool(limits.kill_tree), preexec_fn=_apply(limits))
    except FileNotFoundError:
        raise SandboxUnavailable(f"executable not found: {argv[0]}")

    timed_out = False
    try:
        data = stdin_text.encode("utf-8") if stdin_text is not None else None
        out_b, err_b = proc.communicate(input=data, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed_group = False
        if limits.kill_tree:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                killed_group = True
            except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
                pass
        if not killed_group:
            proc.kill()
        try:
            out_b, err_b = proc.communicate(timeout=10.0)
        except Exception:  # pragma: no cover
            out_b, err_b = b"", b""

    duration = (time.perf_counter() - t0) * 1000.0
    code = proc.returncode
    notes: list[str] = []
    if not limits.kill_tree:
        notes.append("kill_tree is off; only the direct child is terminated")
    error: str | None = None
    if timed_out:
        error = (f"command timed out after {timeout:g}s; the sandbox killed the "
                 f"{'process group' if limits.kill_tree else 'process'}")
    elif code != 0:
        error = f"command exited with code {code}"
    return SandboxResult(
        ok=(code == 0 and not timed_out),
        exit_code=code,
        stdout=(out_b or b"").decode("utf-8", errors="replace"),
        stderr=(err_b or b"").decode("utf-8", errors="replace"),
        tier=TIER, contained=True, timed_out=timed_out, env_scrubbed=True,
        error=error, notes=notes, duration_ms=duration, argv=list(argv))
