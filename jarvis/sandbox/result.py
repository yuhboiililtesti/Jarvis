"""Result type for a sandboxed process run.

Every field states what actually happened, including the parts that went wrong
(``timed_out``, ``limit_terminated``, ``contained``). A caller can always tell
"the command ran inside a real sandbox" from "the sandbox was unavailable and
the command ran uncontained".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["SandboxResult", "SandboxUnavailable"]


class SandboxUnavailable(RuntimeError):
    """Raised when a requested containment tier cannot be set up at all."""


@dataclass
class SandboxResult:
    ok: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    tier: str = "none"
    # True only when the OS-level containment was actually applied.
    contained: bool = False
    timed_out: bool = False
    # Processes the OS terminated for exceeding a memory/CPU/process limit.
    limit_terminated: int = 0
    # True when the child's environment was scrubbed of secrets.
    env_scrubbed: bool = True
    error: str | None = None
    # Non-fatal caveats (e.g. containment could not be applied but the command ran).
    notes: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    argv: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "tier": self.tier,
            "contained": self.contained,
            "timed_out": self.timed_out,
            "limit_terminated": self.limit_terminated,
            "env_scrubbed": self.env_scrubbed,
            "error": self.error,
            "notes": list(self.notes),
            "duration_ms": round(self.duration_ms, 1),
        }
