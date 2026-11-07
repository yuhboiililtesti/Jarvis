"""The sandbox service: capability detection, environment scrubbing and safe runs.

JARVIS reaches the operating system's process tree through exactly two doors —
``shell.run`` and the desktop tools' ``run_cmd`` helper — and both go through
this service. It answers three questions honestly:

  1. **What containment is actually available here?** (:meth:`tiers`)
  2. **What is a child allowed to consume?** (:attr:`limits`)
  3. **What happened when it ran?** (:class:`~jarvis.sandbox.result.SandboxResult`)

Containment is layered: an OS-level tier (a Windows Job Object, or POSIX
rlimits) enforces memory/CPU/process limits and guarantees the whole process tree
dies, and the child's environment is scrubbed of secrets regardless of tier. The
things this service does **not** do — filesystem and network isolation — are
listed in :meth:`status` rather than implied away; those need a VM or container
tier, and the blueprint's answer for them is stated as not integrated.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from . import posix as posix_tier
from . import win_job
from .env import sanitized_env, secret_values_of
from .limits import Limits, limits_from_config
from .result import SandboxResult, SandboxUnavailable

log = get_logger("jarvis.sandbox")

__all__ = ["SandboxService", "Limits", "SandboxResult", "SandboxUnavailable"]

# Best first. Only tiers JARVIS actually drives appear here.
_DRIVEN_TIERS = ("jobobject", "rlimit")


class SandboxService:
    """Owns sandbox policy and performs contained process runs.

    Construct with the kernel (``SandboxService(j)``, as the kernel's optional
    subsystems are) or with an explicit config (``SandboxService(config=cfg)``).
    Stateless apart from its config, so a tool may build a throwaway one per call.
    """

    def __init__(self, kernel: Any = None, config: Any = None) -> None:
        self.kernel = kernel
        self._config = config

    # -- configuration ---------------------------------------------------
    @property
    def config(self) -> Any:
        if self._config is not None:
            return self._config
        if self.kernel is not None:
            return self.kernel.config
        from ..config import Config
        return Config()

    @property
    def sb_config(self) -> Any:
        return getattr(self.config, "sandbox", None)

    @property
    def limits(self) -> Limits:
        return limits_from_config(self.sb_config)

    @property
    def enabled(self) -> bool:
        sb = self.sb_config
        return bool(getattr(sb, "enabled", True)) if sb is not None else True

    @property
    def scrub_env(self) -> bool:
        sb = self.sb_config
        if sb is None:
            return True
        return bool(getattr(sb, "scrub_env", True))

    @property
    def requested_tier(self) -> str:
        sb = self.sb_config
        tier = str(getattr(sb, "tier", "auto") or "auto").strip().lower()
        return tier or "auto"

    # -- tier detection --------------------------------------------------
    @staticmethod
    def _tier_available(tier: str) -> tuple[bool, str]:
        if tier == "jobobject":
            return win_job.available()
        if tier == "rlimit":
            return posix_tier.available()
        return False, f"unknown tier '{tier}'"

    def _best_available(self) -> str:
        for tier in _DRIVEN_TIERS:
            if self._tier_available(tier)[0]:
                return tier
        return "none"

    def active_tier(self) -> str:
        """The tier a run would actually use (``none`` means uncontained)."""
        if not self.enabled:
            return "none"
        requested = self.requested_tier
        if requested == "none":
            return "none"
        if requested in _DRIVEN_TIERS:
            return requested if self._tier_available(requested)[0] else self._best_available()
        return self._best_available()

    def tiers(self) -> list[dict[str, Any]]:
        """Every tier JARVIS knows about, with an honest availability reason."""
        out: list[dict[str, Any]] = []
        for tier in _DRIVEN_TIERS:
            ok, reason = self._tier_available(tier)
            caps = win_job.capabilities() if tier == "jobobject" else posix_tier.capabilities()
            out.append({"tier": tier, "available": ok, "reason": reason,
                        "capabilities": caps if ok else []})
        out.append(self._windows_sandbox_tier())
        out.append(self._container_tier())
        return out

    @staticmethod
    def _windows_sandbox_tier() -> dict[str, Any]:
        caps = ["filesystem-isolation", "network-isolation", "disposable-vm",
                "no-secrets-by-default"]
        if os.name != "nt":
            return {"tier": "windows_sandbox", "available": False,
                    "reason": "Windows Sandbox is Windows-only", "capabilities": []}
        exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsSandbox.exe"
        if exe.is_file():
            reason = ("Windows Sandbox is installed but JARVIS does not drive it: it opens an "
                      "interactive disposable VM, and no unattended run path is implemented")
        else:
            reason = ("Windows Sandbox is not installed/enabled (optional feature "
                      "'Containers-DisposableClientVM'); enabling it would give a disposable VM "
                      "for filesystem and network isolation")
        return {"tier": "windows_sandbox", "available": False, "reason": reason,
                "capabilities": caps}

    @staticmethod
    def _container_tier() -> dict[str, Any]:
        caps = ["filesystem-isolation", "network-isolation", "disposable-fs", "user-namespace"]
        found = [exe for exe in ("docker", "podman") if shutil.which(exe)]
        if found:
            reason = (f"{', '.join(found)} present but no container tier is integrated; "
                      "running untrusted code in a container is not wired up")
        else:
            reason = "no Docker or Podman executable found on PATH"
        return {"tier": "container", "available": False, "reason": reason,
                "capabilities": caps}

    # -- reporting -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        active = self.active_tier()
        lim = self.limits
        contained = active in _DRIVEN_TIERS
        process_cap = "enforced" if active == "jobobject" and lim.max_processes > 0 else (
            "not supported on this tier" if contained else "not enforced")
        isolation = {
            "process_tree": "enforced (whole tree dies)" if contained else "not enforced",
            "wall_timeout": ("enforced (whole tree killed)" if contained
                             else "enforced for the direct child only"),
            "memory_cap": ("enforced" if contained and lim.max_memory_mb > 0
                           else "not configured" if contained else "not enforced"),
            "process_cap": process_cap,
            "cpu_time_cap": ("enforced" if contained and lim.max_cpu_seconds > 0
                             else "not configured" if contained else "not enforced"),
            "file_size_cap": ("enforced" if active == "rlimit" and lim.max_file_mb > 0
                              else "not supported on this tier" if contained
                              else "not enforced"),
            "environment_secrets": "stripped" if self.scrub_env else "inherited",
            "filesystem": "not isolated - the child runs as the owner's account",
            "network": "not isolated - in-process tiers cannot restrict sockets",
        }
        notes = [
            "Child processes never inherit JARVIS_SECRET_* (nor any value matching a stored "
            "secret) when environment scrubbing is on.",
            "Filesystem and network isolation require a VM or container tier; neither is "
            "integrated, so this is process/resource containment, not a full sandbox.",
        ]
        return {
            "enabled": self.enabled,
            "requested_tier": self.requested_tier,
            "active_tier": active,
            "contained": contained,
            "env_scrubbed": self.scrub_env,
            "limits": lim.as_dict(),
            "tiers": self.tiers(),
            "isolation": isolation,
            "notes": notes,
        }

    def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"available": True, "reason": "sandbox disabled by config "
                                                 "(sandbox.enabled=false)"}
        active = self.active_tier()
        if active in _DRIVEN_TIERS:
            _, reason = self._tier_available(active)
            return {"available": True, "reason": f"active tier: {active} - {reason}"}
        return {"available": False,
                "reason": "no containment tier is available on this platform; runs are "
                          "uncontained (environment is still scrubbed)"}

    # -- environment -----------------------------------------------------
    def _secret_values(self) -> set[str]:
        store = None
        if self.kernel is not None:
            services = getattr(self.kernel, "services", None) or {}
            store = services.get("secrets")
        if store is None:
            # Built without a kernel (e.g. ``_win.run_cmd``, ffmpeg): read the
            # secret store from disk so value-based scrubbing still applies.
            try:
                from ..policy.secrets import SecretStore
                store = SecretStore(Path(self.config.data_path) / "secrets.json")
            except Exception:  # pragma: no cover - defensive
                store = None
        return secret_values_of(store)

    def child_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """The environment a child process gets: secrets removed, extras merged."""
        if not self.scrub_env:
            full = dict(os.environ)
            if extra:
                full.update({str(k): str(v) for k, v in extra.items()})
            return full
        return sanitized_env(extra=extra, secret_values=self._secret_values())

    # -- running ---------------------------------------------------------
    def run(self, argv: list[str], *, cwd: str | None = None,
            timeout: float | None = None, extra_env: dict[str, str] | None = None,
            limits: Limits | None = None, stdin_text: str | None = None) -> SandboxResult:
        """Run ``argv`` under the best available tier. Blocking; never raises."""
        lim = limits or self.limits
        effective_timeout = float(timeout if timeout is not None else lim.wall_timeout_s)
        env = self.child_env(extra_env)
        scrubbed = self.scrub_env
        tier = self.active_tier()

        if tier in _DRIVEN_TIERS:
            runner = win_job.run_in_job if tier == "jobobject" else posix_tier.run_with_limits
            try:
                result = runner(list(argv), cwd=cwd, env=env,
                                timeout=effective_timeout, limits=lim,
                                stdin_text=stdin_text)
                result.env_scrubbed = scrubbed
                return result
            except SandboxUnavailable as exc:
                log.warning("sandbox tier %s unavailable at run time: %s; "
                            "running uncontained", tier, exc)
            except Exception as exc:
                # The runner raised after the child was already started. Do NOT
                # retry uncontained: that could execute the command twice.
                log.exception("sandbox tier %s failed after start: %s", tier, exc)
                return SandboxResult(
                    ok=False, tier=tier, contained=False, env_scrubbed=scrubbed,
                    argv=list(argv), error=f"{type(exc).__name__}: {exc}",
                    notes=["the sandbox runner raised after starting; the command was NOT "
                           "retried uncontained (a retry could run it twice)"])
        return self._plain(argv, cwd=cwd, env=env, timeout=effective_timeout,
                           scrubbed=scrubbed, stdin_text=stdin_text)

    async def run_async(self, argv: list[str], *, cwd: str | None = None,
                        timeout: float | None = None,
                        extra_env: dict[str, str] | None = None,
                        limits: Limits | None = None,
                        stdin_text: str | None = None) -> SandboxResult:
        import functools
        loop = asyncio.get_running_loop()
        fn = functools.partial(self.run, list(argv), cwd=cwd, timeout=timeout,
                               extra_env=extra_env, limits=limits, stdin_text=stdin_text)
        return await loop.run_in_executor(None, fn)

    def _plain(self, argv: list[str], *, cwd: str | None, env: dict[str, str],
               timeout: float, scrubbed: bool, stdin_text: str | None = None) -> SandboxResult:
        """Uncontained fallback: still no shell, still scrubbed environment."""
        t0 = time.perf_counter()
        data = stdin_text.encode("utf-8") if stdin_text is not None else None
        try:
            proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                  stdin=subprocess.DEVNULL if data is None else None,
                                  input=data, timeout=timeout)
        except FileNotFoundError:
            return SandboxResult(ok=False, tier="none", contained=False,
                                 env_scrubbed=scrubbed, argv=list(argv),
                                 error=f"executable not found: {argv[0]}",
                                 duration_ms=(time.perf_counter() - t0) * 1000.0)
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                ok=False, tier="none", contained=False, timed_out=True,
                env_scrubbed=scrubbed, argv=list(argv),
                stdout=(exc.stdout or b"").decode("utf-8", errors="replace"),
                stderr=(exc.stderr or b"").decode("utf-8", errors="replace"),
                error=f"command timed out after {timeout:g}s", 
                duration_ms=(time.perf_counter() - t0) * 1000.0)
        except Exception as exc:  # pragma: no cover - defensive
            return SandboxResult(ok=False, tier="none", contained=False,
                                 env_scrubbed=scrubbed, argv=list(argv),
                                 error=f"{type(exc).__name__}: {exc}",
                                 duration_ms=(time.perf_counter() - t0) * 1000.0)
        code = proc.returncode
        return SandboxResult(
            ok=(code == 0), exit_code=code, tier="none", contained=False,
            env_scrubbed=scrubbed, argv=list(argv),
            stdout=(proc.stdout or b"").decode("utf-8", errors="replace"),
            stderr=(proc.stderr or b"").decode("utf-8", errors="replace"),
            error=None if code == 0 else f"command exited with code {code}",
            duration_ms=(time.perf_counter() - t0) * 1000.0)


def from_config(config: Any) -> SandboxService:
    """Build a service bound to a config (used where no kernel is in hand)."""
    return SandboxService(config=config)
