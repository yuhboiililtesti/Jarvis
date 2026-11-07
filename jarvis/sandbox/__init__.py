"""Sandbox: OS-level process containment and secret-free child environments.

The blueprint requires that unknown code runs with no secrets by default, CPU/RAM
quotas, timeouts and, ideally, a disposable filesystem
(``Blueprints/AI-Blueprint/17_SECURITY_PRIVACY_RECOVERY.md`` §Sandboxing). JARVIS
implements the enforceable half — a Windows Job Object or POSIX rlimits around
every child process, plus environment scrubbing — and *reports* the half it does
not (filesystem and network isolation, which need a VM or container tier).

    from jarvis.sandbox import SandboxService
    svc = SandboxService(config=cfg)
    svc.status()   # what is available here, and what is not
    svc.run(["python", "-c", "print(1)"])

Nothing in this package performs an effect at import time.
"""

from __future__ import annotations

from .env import SECRET_PREFIX, sanitized_env, secret_values_of
from .limits import Limits, limits_from_config
from .result import SandboxResult, SandboxUnavailable
from .service import SandboxService, from_config

__all__ = [
    "SandboxService", "SandboxResult", "SandboxUnavailable", "Limits",
    "from_config", "limits_from_config", "sanitized_env", "secret_values_of",
    "SECRET_PREFIX",
]
