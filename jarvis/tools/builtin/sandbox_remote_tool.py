"""``sandbox.run_code`` / ``sandbox.remote_status`` — a real remote code runner.

Runs a self-contained program on the dedicated Linux runner (the laptop by
default, through the operator's SSH config) under kernel resource limits and
a wall-clock timeout, in a fresh temp directory that is removed afterwards.
``sandbox.run_code`` is ``LOCAL_EXEC``: it executes code, so it always needs
approval in ask mode. It is deliberately NOT auto-allowed.

Honest boundary: separate machine + rlimits + timeout; network is not
isolated (unprivileged namespaces are unavailable on the runner). Not for
hostile code.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.sandbox_remote")

__all__ = ["SandboxRunCodeTool", "SandboxRemoteStatusTool", "health"]


def health() -> dict[str, Any]:
    try:
        from ...sandbox.remote import RemoteSandbox  # noqa: F401
        return {"available": True, "reason": "remote sandbox module available"}
    except Exception as exc:  # pragma: no cover
        return {"available": False, "reason": str(exc)}


@register(["sandbox.remote_status"])
class SandboxRemoteStatusTool(BaseTool):
    spec = ToolSpec(
        name="sandbox.remote_status",
        summary=("Report the remote code runner: reachability, host, limits, interpreter "
                 "whitelist and what is NOT isolated (network)."),
        category="sandbox",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object: {available, reason, host, limits, network_isolated}",
        cheap=True,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        from ...sandbox.remote import RemoteSandbox
        cfg = getattr(ctx.config, "sandbox", None)
        host = getattr(cfg, "remote_host", "laptop")
        sandbox = RemoteSandbox(host=host)
        status = await asyncio.to_thread(sandbox.status)
        return ToolResult(ok=True, data=status,
                          evidence={"method": "ssh-probe", **status})


@register(["sandbox.run_code"])
class SandboxRunCodeTool(BaseTool):
    spec = ToolSpec(
        name="sandbox.run_code",
        summary=("Run a self-contained program on the isolated runner (separate machine, "
                 "memory/CPU/file limits, wall timeout) and return stdout/stderr/exit code."),
        category="sandbox",
        side_effect=SideEffect.LOCAL_EXEC,   # executes code: approval required
        params=(
            ToolParam("program", "string", "Program source. Required.", required=True),
            ToolParam("interpreter", "string", "python3 | bash | sh | node",
                      default="python3"),
            ToolParam("timeout_s", "number", "Wall-clock limit in seconds.", default=10.0),
            ToolParam("memory_mb", "integer", "Address-space cap in MiB.", default=256),
            ToolParam("cpu_seconds", "integer", "CPU-time cap in seconds.", default=5),
            ToolParam("stdin", "string", "Optional stdin for the program.", default=""),
            ToolParam("host", "string", "SSH host alias of the runner.", default="laptop"),
        ),
        returns="object: {ok, exit_code, stdout, stderr, elapsed_s, host, limits, timed_out}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        from ...sandbox.remote import RemoteLimits, RemoteSandbox
        interpreter = str(kwargs.get("interpreter") or "python3")
        if interpreter not in ("python3", "bash", "sh", "node"):
            return ToolResult(ok=False, error=f"unsupported interpreter {interpreter!r}")
        try:
            limits = RemoteLimits(
                wall_seconds=float(kwargs.get("timeout_s") or 10.0),
                memory_mb=int(kwargs.get("memory_mb") or 256),
                cpu_seconds=int(kwargs.get("cpu_seconds") or 5))
            limits.validate()
        except (TypeError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))
        host = str(kwargs.get("host") or "laptop")
        sandbox = RemoteSandbox(host=host, limits=limits)
        result = await asyncio.to_thread(
            sandbox.run, str(kwargs["program"]),
            interpreter=interpreter,
            stdin_data=str(kwargs.get("stdin") or ""),
            limits=limits)
        data = result.to_dict()
        log.info("sandbox.run_code host=%s ok=%s exit=%s elapsed=%.2fs",
                 host, result.ok, result.exit_code, result.elapsed_s)
        return ToolResult(ok=result.ok, data=data, error=result.error,
                          evidence={"method": "remote-rlimit-sandbox",
                                    "host": host, "limits": data["limits"]})
