"""``shell.run`` - run one local command under a hard destructive-pattern gate.

This is the only builtin that can execute an arbitrary local process, so it is
deliberately conservative:

  * a denylist of destructive patterns is checked *before* anything is spawned
    and refuses outright (the list is extensible via the ``shell_denylist`` kv);
  * the command always runs through an explicit interpreter (never ``shell=True``);
  * it runs inside the process sandbox (:mod:`jarvis.sandbox`) — a Windows Job
    Object or POSIX rlimits — so the whole process tree is killed together and
    the child never inherits ``JARVIS_SECRET_*`` from the environment;
  * output is truncated so a runaway command cannot flood model context;
  * a wall-clock timeout kills the process tree and reports the timeout honestly.

``verify`` never re-runs the command; it only reflects the exit code that was
actually observed. That keeps the verifier an independent observer rather than a
second execution.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.shell")

__all__ = ["ShellRunTool", "DEFAULT_DENY_PATTERNS", "MAX_OUTPUT_CHARS", "MAX_TIMEOUT_S"]

MAX_OUTPUT_CHARS = 8000
MAX_TIMEOUT_S = 600.0

# Case-insensitive regular expressions. These are *refusals*, not filters: a
# match stops the call without spawning a process. Kept intentionally narrow so
# ordinary, benign commands are not blocked by an over-broad pattern.
DEFAULT_DENY_PATTERNS: tuple[str, ...] = (
    r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+/(?:\s|$)",   # rm -rf /
    r"rm\s+-[a-z]*f[a-z]*r[a-z]*\s+/(?:\s|$)",   # rm -fr /
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",  # classic fork bomb
    r"\bmkfs(?:\.\w+)?\b",                        # mkfs / mkfs.ext4
    r"\bformat\s+[a-z]:",                         # format C:
    r"Remove-Item\b[^\n|;]*-Recurse[^\n|;]*-Force\s+[a-z]:\\?(?:\s|$)",
    r"\b(?:shutdown|shutdown\.exe)\b",
    r"\bRestart-Computer\b",
    r"\bStop-Computer\b",
    r"\bClear-Disk\b",
    r"\bdiskpart\b",
    r"\bdd\b[^\n]*\bof=/dev/[a-z]",
    r">\s*/dev/[sh]d[a-z]",
)


def _default_shell() -> str:
    return "powershell" if os.name == "nt" else "bash"


def _argv(shell: str, command: str) -> list[str]:
    """Build the explicit interpreter invocation (never ``shell=True``)."""
    name = shell.strip().lower()
    if name in ("powershell", "pwsh"):
        return [name, "-NoProfile", "-NonInteractive", "-Command", command]
    if name == "cmd":
        return ["cmd", "/d", "/s", "/c", command]
    # bash / sh / zsh / ... on POSIX; last resort for anything else.
    return [name or "sh", "-c", command]


def _clamp_timeout(value: Any) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        t = 30.0
    return max(0.5, min(t, MAX_TIMEOUT_S))


def _denylist(ctx: ToolContext) -> list[str]:
    patterns = list(DEFAULT_DENY_PATTERNS)
    db = getattr(ctx, "db", None)
    if db is not None:
        try:
            extra = db.kv_get("shell_denylist")
        except Exception as exc:  # pragma: no cover - kv must never break a tool
            log.debug("shell_denylist lookup failed: %s", exc)
            extra = None
        if isinstance(extra, (list, tuple)):
            patterns.extend(str(p) for p in extra)
    return patterns


def _deny_hit(command: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return pattern
        except re.error:  # a bad custom pattern is skipped, not fatal
            log.warning("ignoring invalid shell denylist pattern: %r", pattern)
    return None


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _sandbox_for(ctx: ToolContext) -> Any:
    """The kernel's sandbox service, or a config-bound one if none is wired."""
    svc = ctx.service("sandbox") if ctx is not None else None
    if svc is not None:
        return svc
    from ...sandbox import SandboxService
    return SandboxService(config=getattr(ctx, "config", None))


@register(["shell"])
class ShellRunTool(BaseTool):
    spec = ToolSpec(
        name="shell.run",
        summary="Run a local shell/powershell command, returning exit code, stdout and stderr.",
        version="1.0",
        category="system",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(
            ToolParam("command", "string", "Command line to execute.", required=True),
            ToolParam("cwd", "string", "Working directory (must exist). Defaults to the server's cwd."),
            ToolParam("timeout_s", "number", "Wall-clock timeout in seconds (default 30, max 600).",
                      default=30),
            ToolParam("shell", "string", "Interpreter: powershell/pwsh/cmd on Windows, bash/sh on POSIX.",
                      default=_default_shell()),
        ),
        returns="object: {exit_code, stdout, stderr, shell, cwd, truncated}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            command = str(kwargs.get("command") or "").strip()
            if not command:
                return ToolResult(ok=False, error="command is empty")

            shell = str(kwargs.get("shell") or _default_shell())
            timeout = _clamp_timeout(kwargs.get("timeout_s"))
            cwd = kwargs.get("cwd") or None
            if cwd is not None and not os.path.isdir(str(cwd)):
                return ToolResult(ok=False, error=f"cwd is not a directory: {cwd}")
            cwd = str(cwd) if cwd else None

            patterns = _denylist(ctx)
            hit = _deny_hit(command, patterns)
            if hit is not None:
                log.warning("shell.run refused a denied pattern: %s", hit)
                if ctx is not None and getattr(ctx, "db", None) is not None:
                    ctx.db.audit("shell.refused", target=command,
                                 detail={"pattern": hit}, severity="warn")
                return ToolResult(
                    ok=False,
                    error=("refused: command matches a destructive denylist pattern "
                           f"(/{hit}/); it was not executed"),
                    evidence={"refused": True, "pattern": hit},
                    certainty="settled",
                )

            argv = _argv(shell, command)
            sandbox = _sandbox_for(ctx)
            res = await sandbox.run_async(argv, cwd=cwd, timeout=timeout)

            if res.exit_code is None and res.error:
                # The interpreter could not be started at all.
                return ToolResult(ok=False, error=f"interpreter not found: {argv[0]}",
                                  evidence={"sandbox": {"tier": res.tier}},
                                  certainty="unverified")

            stdout, stderr = res.stdout, res.stderr
            truncated = (len(stdout) > MAX_OUTPUT_CHARS
                         or len(stderr) > MAX_OUTPUT_CHARS)
            code = int(res.exit_code if res.exit_code is not None else -1)
            data = {
                "exit_code": code,
                "stdout": _truncate(stdout),
                "stderr": _truncate(stderr),
                "shell": shell,
                "cwd": cwd,
                "truncated": truncated,
                "sandbox": {"tier": res.tier, "contained": res.contained,
                            "env_scrubbed": res.env_scrubbed,
                            "timed_out": res.timed_out},
            }
            if res.timed_out:
                return ToolResult(
                    ok=False,
                    error=f"command timed out after {timeout:g}s and the process tree was killed",
                    data=data,
                    evidence={"method": "sandbox-timeout", "tier": res.tier,
                              "contained": res.contained},
                    certainty="unverified",
                )
            return ToolResult(
                ok=(code == 0),
                data=data,
                error=None if code == 0 else f"command exited with code {code}",
                evidence={"method": "exit-status", "exit_code": code,
                          "sandbox_tier": res.tier, "contained": res.contained},
                certainty="settled",
            )
        except Exception as exc:  # never raise out of run()
            log.exception("shell.run failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")

    def verify(self, ctx: ToolContext, result: ToolResult):
        """No re-run: a zero exit code is the only thing we can attest to."""
        if not result.ok or not isinstance(result.data, dict):
            return None
        if result.data.get("exit_code") == 0:
            return {"verified": True, "method": "exit-status", "exit_code": 0}
        return None
