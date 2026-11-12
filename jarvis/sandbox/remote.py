"""Remote execution sandbox — a separate machine with kernel-enforced limits.

Runs a self-contained program on a dedicated Linux runner (the laptop by
default) through the operator's existing SSH config, inside a fresh temporary
directory, under kernel resource limits and a wall-clock timeout. The
directory is removed in a trap on exit.

Containment is real but bounded, and the limits are stated here rather than
implied:

* a **separate machine** — a runaway program cannot take the Core's host down;
* **RLIMIT_AS** (address space) — a memory hog raises ``MemoryError`` or is
  killed instead of exhausting the runner;
* **RLIMIT_CPU** (CPU seconds) and a **wall timeout** with SIGKILL;
* **RLIMIT_FSIZE** (file size) and **RLIMIT_NPROC** (process count, best
  effort — it is per-UID, so the value is generous to avoid breaking the
  user's own session);
* environment scrubbed by SSH; no credentials are sent (the SSH config owns
  them).

**Not** a security sandbox: unprivileged network namespaces are unavailable
on the runner (``unshare -n`` fails), so network access is *not* isolated.
Never run hostile code here. EVOLVE keeps generated code disabled for exactly
this reason.
"""

from __future__ import annotations

import base64
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["RemoteLimits", "RemoteResult", "RemoteSandbox", "build_script",
           "ALLOWED_INTERPRETERS"]

ALLOWED_INTERPRETERS = ("python3", "bash", "sh", "node")


@dataclass
class RemoteLimits:
    wall_seconds: float = 10.0
    memory_mb: int = 256
    cpu_seconds: int = 5
    processes: int = 512
    file_mb: int = 16

    def validate(self) -> None:
        if self.wall_seconds <= 0 or self.wall_seconds > 600:
            raise ValueError("wall_seconds must be in (0, 600]")
        if not (16 <= self.memory_mb <= 8192):
            raise ValueError("memory_mb must be in [16, 8192]")
        if not (1 <= self.cpu_seconds <= 600):
            raise ValueError("cpu_seconds must be in [1, 600]")
        if not (1 <= self.file_mb <= 1024):
            raise ValueError("file_mb must be in [1, 1024]")

    def to_dict(self) -> dict[str, Any]:
        return {"wall_seconds": self.wall_seconds, "memory_mb": self.memory_mb,
                "cpu_seconds": self.cpu_seconds, "processes": self.processes,
                "file_mb": self.file_mb}


@dataclass
class RemoteResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    elapsed_s: float
    host: str
    limits: dict[str, Any] = field(default_factory=dict)
    timed_out: bool = False
    error: str | None = None
    tier: str = "remote-rlimit"

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "exit_code": self.exit_code,
                "stdout": self.stdout, "stderr": self.stderr,
                "elapsed_s": round(self.elapsed_s, 3), "host": self.host,
                "limits": self.limits, "timed_out": self.timed_out,
                "error": self.error, "tier": self.tier}


def build_script(program: str, *, interpreter: str = "python3",
                 args: tuple[str, ...] = (), stdin_data: str = "",
                 limits: RemoteLimits | None = None) -> str:
    """The bash script executed on the runner (program passed as base64)."""
    if interpreter not in ALLOWED_INTERPRETERS:
        raise ValueError(f"interpreter must be one of {ALLOWED_INTERPRETERS}")
    limits = limits or RemoteLimits()
    limits.validate()
    encoded = base64.b64encode(program.encode("utf-8")).decode("ascii")
    arg_text = " ".join(shlex.quote(str(a)) for a in args)
    stdin_redirect = ""
    stdin_write = ""
    if stdin_data:
        stdin_b64 = base64.b64encode(stdin_data.encode("utf-8")).decode("ascii")
        stdin_write = (f"printf '%s' '{stdin_b64}' | base64 -d > \"$d/stdin.bin\"\n")
        stdin_redirect = ' < "$d/stdin.bin"'
    return (
        "set -eu\n"
        'd=$(mktemp -d)\n'
        "trap 'rm -rf \"$d\"' EXIT\n"
        f"printf '%s' '{encoded}' | base64 -d > \"$d/program.bin\"\n"
        + stdin_write +
        'cd "$d"\n'
        f"ulimit -v {limits.memory_mb * 1024} 2>/dev/null || true\n"
        f"ulimit -t {limits.cpu_seconds}\n"
        f"ulimit -f {limits.file_mb * 1024}\n"
        f"ulimit -u {limits.processes} 2>/dev/null || true\n"
        "set +e\n"
        f"timeout -s KILL {limits.wall_seconds:g} {interpreter} "
        f"program.bin {arg_text}{stdin_redirect}\n"
        "rc=$?\n"
        "set -e\n"
        'echo "__JARVIS_SANDBOX_RC__=$rc"\n'
        "exit $rc\n"
    )


class RemoteSandbox:
    def __init__(self, host: str = "laptop", *, ssh_bin: str = "ssh",
                 connect_timeout_s: float = 8.0,
                 limits: RemoteLimits | None = None) -> None:
        self.host = host
        self.ssh_bin = ssh_bin
        self.connect_timeout_s = connect_timeout_s
        self.limits = limits or RemoteLimits()

    # -- probing ---------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                [self.ssh_bin, "-o", "BatchMode=yes",
                 "-o", f"ConnectTimeout={self.connect_timeout_s:g}",
                 self.host, "echo", "jarvis-sandbox-ok"],
                capture_output=True, timeout=self.connect_timeout_s + 5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        out = proc.stdout.decode("utf-8", "replace")
        if proc.returncode == 0 and "jarvis-sandbox-ok" in out:
            return True, "reachable"
        return False, (proc.stderr.decode("utf-8", "replace").strip()
                       or f"ssh exited {proc.returncode}")

    # -- execution -------------------------------------------------------
    def run(self, program: str, *, interpreter: str = "python3",
            args: tuple[str, ...] = (), stdin_data: str = "",
            limits: RemoteLimits | None = None) -> RemoteResult:
        limits = limits or self.limits
        try:
            script = build_script(program, interpreter=interpreter, args=args,
                                  stdin_data=stdin_data, limits=limits)
        except ValueError as exc:
            return RemoteResult(ok=False, exit_code=None, stdout="", stderr="",
                                elapsed_s=0.0, host=self.host,
                                limits=limits.to_dict(), error=str(exc))
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                [self.ssh_bin, "-o", "BatchMode=yes",
                 "-o", f"ConnectTimeout={self.connect_timeout_s:g}",
                 self.host, "bash", "-s"],
                input=script.encode("utf-8"), capture_output=True,
                timeout=limits.wall_seconds + self.connect_timeout_s + 10)
        except subprocess.TimeoutExpired:
            return RemoteResult(ok=False, exit_code=None, stdout="", stderr="",
                                elapsed_s=time.perf_counter() - t0,
                                host=self.host, limits=limits.to_dict(),
                                timed_out=True,
                                error="client-side timeout (ssh did not return)")
        except OSError as exc:
            return RemoteResult(ok=False, exit_code=None, stdout="", stderr="",
                                elapsed_s=time.perf_counter() - t0,
                                host=self.host, limits=limits.to_dict(),
                                error=f"{type(exc).__name__}: {exc}")
        elapsed = time.perf_counter() - t0
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        # the script echoes the child's exit code explicitly, so a SIGKILL
        # from the wall timeout (which SSH would otherwise mask) is visible
        import re
        match = re.search(r"^__JARVIS_SANDBOX_RC__=(\d+)\s*$", out, re.MULTILINE)
        rc = int(match.group(1)) if match else proc.returncode
        if match:
            out = re.sub(r"^__JARVIS_SANDBOX_RC__=\d+\s*$", "", out,
                         flags=re.MULTILINE).strip("\n")
        timed_out = rc in (124, 137) or (
            rc is not None and rc not in (0,) and
            elapsed >= limits.wall_seconds * 0.9)
        return RemoteResult(
            ok=(rc == 0) and proc.returncode == 0, exit_code=rc,
            stdout=out, stderr=err, elapsed_s=elapsed, host=self.host,
            limits=limits.to_dict(), timed_out=timed_out,
            error=None if rc == 0 else (
                "timeout" if timed_out else f"exit {rc}"))

    # -- reporting -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        available, reason = self.available()
        return {"available": available, "reason": reason, "host": self.host,
                "limits": self.limits.to_dict(),
                "interpreter_whitelist": list(ALLOWED_INTERPRETERS),
                "network_isolated": False,
                "tier": "remote-rlimit"}
