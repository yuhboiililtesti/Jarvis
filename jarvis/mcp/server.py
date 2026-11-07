"""Minimal MCP (Model Context Protocol) stdio server for JARVIS.

This is how another agent — Claude Code, OpenCode, or a script — can use JARVIS
as a tool provider. It speaks JSON-RPC 2.0, newline-delimited, over stdin/stdout
and exposes the real tool registry, so a call is the same policy-gated,
verified action the HUD uses.

**Read-only by default.** MCP has no approval UI, so a tool that would need the
owner's authorisation is refused with an actionable message instead of hanging
for five minutes. Pass ``--allow-mutations`` only on a trusted local console
where you accept that mutations still cannot be approved (and will be refused).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from ..logging_setup import get_logger
from ..tools.base import SideEffect

log = get_logger("jarvis.mcp")

__all__ = ["McpServer", "PROTOCOL_VERSION", "run_stdio"]

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "jarvis"
SERVER_VERSION = "0.1.0"


class McpServer:
    def __init__(self, kernel: Any, *, allow_mutations: bool = False) -> None:
        self.kernel = kernel
        self.allow_mutations = allow_mutations
        if allow_mutations and getattr(kernel.policy, "effective_mode", "ask") == "autonomous":
            log.warning("MCP --allow-mutations with policy.mode=autonomous: non-destructive "
                        "mutating tools can run unattended; the 'refused, no approval "
                        "channel' guarantee applies to ask/readonly mode.")

    # -- exposure --------------------------------------------------------
    def exposed(self) -> list[str]:
        if self.allow_mutations:
            return self.kernel.registry.names()
        auto = set(self.kernel.config.policy.auto_allow)
        return sorted(
            name for name in self.kernel.registry.names()
            if self.kernel.registry.get(name).spec.side_effect == SideEffect.NONE
            or name in auto)

    def _tool(self, name: str) -> dict[str, Any]:
        spec = self.kernel.registry.get(name).spec
        return {
            "name": spec.name,
            "description": f"{spec.summary} [side_effect={spec.side_effect.value}]",
            "inputSchema": spec.parameters_schema(),
        }

    # -- JSON-RPC --------------------------------------------------------
    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = str(message.get("method") or "")
        mid = message.get("id")
        params = message.get("params") or {}
        if method == "notifications/initialized" or method.startswith("notifications/"):
            return None
        if method == "initialize":
            return _ok(mid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": [self._tool(n) for n in self.exposed()]})
        if method == "tools/call":
            return _ok(mid, await self._call(params))
        return _error(mid, -32601, f"method not found: {method}")

    async def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _content("arguments must be an object", error=True)
        tool = self.kernel.registry.get(name)
        if tool is None:
            return _content(f"unknown tool '{name}'", error=True)
        if name not in self.exposed():
            return _content(
                f"'{name}' needs the owner's approval; MCP has no approval channel. "
                "Use the JARVIS HUD or the console.", error=True)
        decision = self.kernel.policy.decide(name, tool.spec.side_effect, args)
        if not decision.allow:
            return _content(f"refused by policy: {decision.reason}", error=True)
        task = self.kernel.controller.create(f"mcp: {name}", source="api")
        result, evidence = await self.kernel.controller.execute_tool(task, name, args)
        payload = {
            "ok": result.ok,
            "data": result.data,
            "error": result.error,
            "verified": bool(evidence.get("verified")),
            "certainty": result.certainty,
        }
        return _content(json.dumps(payload, ensure_ascii=False, default=str),
                        error=not result.ok)

    # -- stdio -----------------------------------------------------------
    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                return
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError as exc:
                _write(_error(None, -32700, f"parse error: {exc}"))
                continue
            if not isinstance(message, dict):
                _write(_error(None, -32600, "invalid request"))
                continue
            response = await self.handle(message)
            if response is not None:
                _write(response)


def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _content(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": bool(error)}


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def run_stdio(kernel: Any, *, allow_mutations: bool = False) -> None:
    await McpServer(kernel, allow_mutations=allow_mutations).serve()
