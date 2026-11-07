"""Resource limits applied to a sandboxed child process.

These are the *enforceable* half of the blueprint's sandbox requirements
(``Blueprints/AI-Blueprint/17_SECURITY_PRIVACY_RECOVERY.md`` §Sandboxing):
CPU/RAM quotas and timeouts. Filesystem and network isolation are **not**
implemented in-process; :mod:`jarvis.sandbox.service` reports that honestly
rather than implying otherwise.

A value of ``0`` means "no limit" for that dimension.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Limits", "limits_from_config"]


@dataclass(frozen=True)
class Limits:
    # Per-process resident memory ceiling, in MiB (0 = unlimited).
    max_memory_mb: int = 2048
    # Maximum simultaneous processes in the sandboxed tree (0 = unlimited).
    max_processes: int = 32
    # CPU *time* ceiling in seconds (0 = unlimited). Wall time is separate.
    max_cpu_seconds: int = 0
    # Largest file the process may write, in MiB (0 = unlimited). POSIX only.
    max_file_mb: int = 0
    # Default wall-clock timeout in seconds.
    wall_timeout_s: float = 30.0
    # Kill the whole process tree, not just the direct child.
    kill_tree: bool = True
    # Hand the child a scrubbed environment (see jarvis.sandbox.env).
    scrub_env: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_memory_mb": self.max_memory_mb,
            "max_processes": self.max_processes,
            "max_cpu_seconds": self.max_cpu_seconds,
            "max_file_mb": self.max_file_mb,
            "wall_timeout_s": self.wall_timeout_s,
            "kill_tree": self.kill_tree,
            "scrub_env": self.scrub_env,
        }


def _int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def limits_from_config(cfg: Any) -> Limits:
    """Build :class:`Limits` from a :class:`jarvis.config.SandboxConfig` (or any object)."""
    if cfg is None:
        return Limits()

    def get(name: str, default: Any) -> Any:
        return getattr(cfg, name, default)

    return Limits(
        max_memory_mb=_int(get("max_memory_mb", 2048), 2048),
        max_processes=_int(get("max_processes", 32), 32),
        max_cpu_seconds=_int(get("max_cpu_seconds", 0), 0),
        max_file_mb=_int(get("max_file_mb", 0), 0),
        wall_timeout_s=float(get("wall_timeout_s", 30.0) or 30.0),
        kill_tree=bool(get("kill_tree", True)),
        scrub_env=bool(get("scrub_env", True)),
    )
