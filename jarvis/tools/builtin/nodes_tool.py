"""``nodes.status`` / ``nodes.ping`` / ``nodes.exec`` - talk to the node service.

All three forward to the ``nodes`` service registered on the kernel, whose
contract is::

    async def list_nodes() -> list[dict]
    async def ping(node_id: str) -> dict
    async def send_task(node_id: str, kind: str, payload: dict) -> dict

These tools contain no transport of their own: HTTP/token handling, retries and
node topology live in the service. If it is missing we say so plainly instead of
reporting a node as healthy.
"""

from __future__ import annotations

import inspect
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.nodes")

__all__ = ["NodesStatusTool", "NodesPingTool", "NodesExecTool"]

_SERVICE = "nodes"
_MISSING = "nodes service is unavailable: no 'nodes' service is registered on the kernel"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _service(ctx: ToolContext) -> Any:
    getter = getattr(ctx, "service", None)
    return getter(_SERVICE) if callable(getter) else None


@register(["nodes.status", "nodes.list"])
class NodesStatusTool(BaseTool):
    spec = ToolSpec(
        name="nodes.status",
        summary="List known nodes and their status.",
        version="1.0",
        category="nodes",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object: {nodes, count}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            svc = _service(ctx)
            if svc is None or not hasattr(svc, "list_nodes"):
                return ToolResult(ok=False, error=_MISSING)
            try:
                nodes = await _maybe_await(svc.list_nodes())
            except Exception as exc:
                return ToolResult(ok=False,
                                  error=f"nodes.status failed: {type(exc).__name__}: {exc}")
            nodes = nodes or []
            return ToolResult(
                ok=True,
                data={"nodes": nodes, "count": len(nodes)},
                evidence={"method": "nodes-service", "count": len(nodes)},
            )
        except Exception as exc:  # never raise out of run()
            log.exception("nodes.status failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["nodes.ping"])
class NodesPingTool(BaseTool):
    spec = ToolSpec(
        name="nodes.ping",
        summary="Ping a node to check liveness.",
        version="1.0",
        category="nodes",
        side_effect=SideEffect.REMOTE,
        params=(
            ToolParam("node", "string", "Node id to ping.", required=True),
        ),
        returns="object: {node, result}",
        needs_network=True,
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            svc = _service(ctx)
            if svc is None or not hasattr(svc, "ping"):
                return ToolResult(ok=False, error=_MISSING)

            node = str(kwargs.get("node") or "").strip()
            if not node:
                return ToolResult(ok=False, error="node is required")

            try:
                result = await _maybe_await(svc.ping(node))
            except Exception as exc:
                return ToolResult(ok=False,
                                  error=f"nodes.ping failed for '{node}': "
                                        f"{type(exc).__name__}: {exc}",
                                  certainty="unverified")
            return ToolResult(
                ok=True,
                data={"node": node, "result": result},
                evidence={"method": "nodes-service", "node": node},
                certainty="settled",
            )
        except Exception as exc:  # never raise out of run()
            log.exception("nodes.ping failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")


@register(["nodes.exec"])
class NodesExecTool(BaseTool):
    spec = ToolSpec(
        name="nodes.exec",
        summary="Send a task to another node for remote execution.",
        version="1.0",
        category="nodes",
        side_effect=SideEffect.REMOTE,
        params=(
            ToolParam("node", "string", "Target node id.", required=True),
            ToolParam("kind", "string", "Task kind understood by the node.", required=True),
            ToolParam("payload", "object", "Task payload/arguments."),
        ),
        returns="object: {node, kind, result}",
        needs_network=True,
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            svc = _service(ctx)
            if svc is None or not hasattr(svc, "send_task"):
                return ToolResult(ok=False, error=_MISSING)

            node = str(kwargs.get("node") or "").strip()
            if not node:
                return ToolResult(ok=False, error="node is required")
            kind = str(kwargs.get("kind") or "").strip()
            if not kind:
                return ToolResult(ok=False, error="kind is required")
            payload = kwargs.get("payload") or {}
            if not isinstance(payload, dict):
                return ToolResult(ok=False, error="payload must be an object")

            try:
                result = await _maybe_await(svc.send_task(node, kind, payload))
            except Exception as exc:
                return ToolResult(ok=False,
                                  error=f"nodes.exec failed for '{node}' kind '{kind}': "
                                        f"{type(exc).__name__}: {exc}",
                                  certainty="unverified")
            return ToolResult(
                ok=True,
                data={"node": node, "kind": kind, "result": result},
                evidence={"method": "nodes-service", "node": node, "kind": kind},
                certainty="unverified",
            )
        except Exception as exc:  # never raise out of run()
            log.exception("nodes.exec failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")

    def verify(self, ctx: ToolContext, result: ToolResult):
        """Remote effects are confirmed by the node, not locally; nothing to read back."""
        return None
