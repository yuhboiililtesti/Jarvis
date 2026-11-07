"""Memory tools: remember, search and forget durable memories.

These are thin, policy-aware wrappers over the shared `MemoryStore`. The store
is fetched from the tool context via `ctx.service("memory")` (the kernel owns its
lifecycle); when it is absent the tool fails loudly rather than pretending to
have remembered anything.
"""

from __future__ import annotations

from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.memory")

__all__ = ["MemoryRememberTool", "MemorySearchTool", "MemoryForgetTool"]

_KINDS = ("fact", "preference", "episode", "summary", "routine")


def _store(ctx: ToolContext):
    return ctx.service("memory")


def _missing_service() -> ToolResult:
    return ToolResult(
        ok=False,
        error=("memory service is unavailable: ctx.service('memory') is not wired "
               "by the kernel"),
    )


def _read_row(ctx: ToolContext, mem_id: str):
    """Read back a memory row for verification, or None when unverifiable."""
    if ctx.db is None:
        return None
    try:
        return ctx.db.query_one(
            "SELECT id,kind,text,deleted FROM memory WHERE id=?", (mem_id,))
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("memory read-back failed: %s", exc)
        return None


@register(["remember", "memory_add", "memory_remember"])
class MemoryRememberTool(BaseTool):
    spec = ToolSpec(
        name="memory.remember",
        summary="Store a durable memory (fact, preference, episode, summary, routine).",
        category="memory",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("text", "string", "The memory content. Required.", required=True),
            ToolParam("kind", "string", "Memory kind.", default="fact", enum=_KINDS),
            ToolParam("tags", "array", "Optional tags.", default=None),
            ToolParam("private", "boolean", "Keep this memory private to the user.",
                      default=True),
        ),
        returns="object with the new memory id and its stored fields",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        store = _store(ctx)
        if store is None:
            return _missing_service()
        try:
            text = str(kwargs["text"]).strip()
            if not text:
                return ToolResult(ok=False, error="text must not be empty")
            kind = str(kwargs.get("kind") or "fact")
            if kind not in _KINDS:
                return ToolResult(ok=False,
                                  error=f"kind must be one of {', '.join(_KINDS)}")
            tags = kwargs.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            private = kwargs.get("private")
            private = True if private is None else bool(private)

            item = await store.remember(
                text, kind=kind, user=ctx.user, source=ctx.task_id or None,
                private=private, tags=list(tags),
            )
            return ToolResult(ok=True, data={
                "id": item.id,
                "text": item.text,
                "kind": item.kind,
                "user": item.user,
                "tags": list(item.tags),
                "private": item.private,
                "created_at": item.created_at,
            })
        except Exception as exc:
            log.warning("memory.remember failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def verify(self, ctx: ToolContext, result: ToolResult) -> dict[str, Any] | None:
        if not result.ok or not isinstance(result.data, dict):
            return {"verified": False, "method": "skipped"}
        row = _read_row(ctx, str(result.data.get("id")))
        if row is None:
            return None
        return {"verified": True, "method": "sqlite-readback", "id": row["id"],
                "kind": row["kind"], "deleted": bool(row["deleted"])}


@register(["recall", "memory_search", "memory.recall"])
class MemorySearchTool(BaseTool):
    spec = ToolSpec(
        name="memory.search",
        summary="Search durable memory (keyword + optional vector).",
        category="memory",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("query", "string", "Search query. Required.", required=True),
            ToolParam("top_k", "integer", "Maximum results to return.", default=6),
        ),
        returns="object with ranked memory items and retrieval evidence",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        store = _store(ctx)
        if store is None:
            return _missing_service()
        try:
            query = str(kwargs["query"]).strip()
            try:
                top_k = int(kwargs.get("top_k") or 6)
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="top_k must be an integer")
            top_k = max(1, min(top_k, 50))
            res = await store.search(query, user=ctx.user, top_k=top_k)
            items = res.get("items", [])
            return ToolResult(ok=True, data={"query": query, "count": len(items),
                                             "items": items,
                                             "evidence": res.get("evidence", {})})
        except Exception as exc:
            log.warning("memory.search failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["forget", "memory_forget"])
class MemoryForgetTool(BaseTool):
    spec = ToolSpec(
        name="memory.forget",
        summary="Forget a durable memory by id (soft delete, or hard with hard=true).",
        category="memory",
        side_effect=SideEffect.DESTRUCTIVE,
        params=(
            ToolParam("id", "string", "Memory id to forget. Required.", required=True),
            ToolParam("hard", "boolean",
                      "Purge the row entirely instead of marking it deleted.",
                      default=False),
        ),
        returns="object confirming the memory id was forgotten",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        store = _store(ctx)
        if store is None:
            return _missing_service()
        try:
            mem_id = str(kwargs["id"]).strip()
            hard = bool(kwargs.get("hard") or False)
            store.forget(mem_id, hard=hard)
            return ToolResult(ok=True, data={"id": mem_id, "hard": hard,
                                             "forgotten": True})
        except Exception as exc:
            log.warning("memory.forget failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def verify(self, ctx: ToolContext, result: ToolResult) -> dict[str, Any] | None:
        if not result.ok or not isinstance(result.data, dict):
            return {"verified": False, "method": "skipped"}
        mem_id = str(result.data.get("id"))
        hard = bool(result.data.get("hard"))
        row = _read_row(ctx, mem_id)
        if ctx.db is None:
            return None
        if hard:
            return {"verified": row is None, "method": "sqlite-readback",
                    "id": mem_id, "row_present": row is not None}
        return {"verified": bool(row is not None and row["deleted"]),
                "method": "sqlite-readback", "id": mem_id,
                "deleted": bool(row["deleted"]) if row is not None else None}
