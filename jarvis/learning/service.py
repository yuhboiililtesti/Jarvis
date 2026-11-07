"""Learning: JARVIS learns from its own outcomes and adapts.

Deterministic and dependency-free — no model is required to form a lesson.
Failed tool calls are distilled into durable, deduplicated ``lesson`` items in
the shared memory store. A repeated observation *reinforces* the lesson's
confidence instead of duplicating it, so the same mistake is not re-taught as a
new fact each time. Relevant lessons are injected into future turns so the
agent adapts to its own history without any retrain.

Explicit teaching (:meth:`learn`) and review (:meth:`list_lessons`) share the
same store, so a lesson the owner states and a lesson the agent discovers are
first-class peers.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..ids import now_iso
from ..logging_setup import get_logger

log = get_logger("jarvis.learning")

__all__ = ["LearningService"]

_KIND = "lesson"

# Reinforcement schedule: each repeated observation of the same failure raises
# confidence one step, capping at 0.95 (a strongly held, but still fallible,
# belief — never a certainty).
_CONF = (0.5, 0.7, 0.85, 0.95)


def _signature(tool: str, args: Any) -> str:
    payload = json.dumps({"tool": tool, "args": args}, sort_keys=True, default=str)
    return "fail:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _compact(args: Any, limit: int = 100) -> str:
    s = json.dumps(args, sort_keys=True, default=str)
    return s if len(s) <= limit else s[: limit - 1] + "\u2026"


class LearningService:
    """Outcome-based learning layered over the shared :class:`MemoryStore`."""

    def __init__(self, memory: Any) -> None:
        # `memory` is the MemoryStore (remember/search/forget + db handle).
        self.memory = memory

    # -- explicit teaching -----------------------------------------------
    async def learn(self, text: str, *, user: str = "owner",
                    tags: list[str] | None = None, confidence: float = 0.9,
                    source: str | None = None) -> str:
        item = await self.memory.remember(
            text, kind=_KIND, user=user, tags=["lesson"] + (tags or []),
            confidence=confidence, source=source)
        return item.id

    async def relevant(self, text: str, *, user: str = "owner",
                       top_k: int = 4) -> dict[str, Any]:
        return await self.memory.search(text, user=user, top_k=top_k, kinds=[_KIND])

    def context_block(self, items: list[dict[str, Any]], limit: int = 900) -> str:
        if not items:
            return ""
        lines = [
            "LEARNED \u2014 lessons JARVIS recorded from past outcomes and owner teaching. "
            "Apply when relevant; a lesson is guidance, never a substitute for calling a tool.",
            "",
        ]
        for it in items:
            conf = round(float(it.get("confidence", 0.5)), 2)
            lines.append(f"- [lesson conf={conf}] {it['text']}")
            if sum(len(x) for x in lines) > limit:
                break
        return "\n".join(lines)

    def list_lessons(self, *, user: str = "owner", limit: int = 30) -> list[dict[str, Any]]:
        rows = self.memory.db.query(
            "SELECT id,kind,text,source,confidence,tags,created_at FROM memory "
            "WHERE user=? AND kind=? AND deleted=0 "
            "ORDER BY confidence DESC, created_at DESC LIMIT ?",
            (user, _KIND, limit))
        return [{"id": r["id"], "text": r["text"], "source": r["source"],
                 "confidence": r["confidence"],
                 "tags": json.loads(r["tags"] or "[]"),
                 "created_at": r["created_at"]} for r in rows]

    def forget_lesson(self, lesson_id: str) -> bool:
        return self.memory.forget(lesson_id)

    # -- automatic outcome learning --------------------------------------
    async def record_turn(self, *, request: str, tool_calls: list[dict[str, Any]],
                          user: str = "owner", source: str = "text",
                          error: str | None = None) -> int:
        """Distil a turn's outcomes into lessons; returns how many were newly recorded.

        ``tool_calls`` entries are dicts with ``tool``, ``args``, ``ok`` and an
        optional ``error`` (populated when the call failed).
        """
        recorded = 0
        for call in tool_calls or []:
            if call.get("ok"):
                continue
            tool = str(call.get("tool") or "").strip()
            if not tool:
                continue
            err = call.get("error") or error or "failed"
            if await self._observe_failure(tool, call.get("args", {}), err, user):
                recorded += 1
        return recorded

    async def _observe_failure(self, tool: str, args: Any, error: str, user: str) -> bool:
        """Record a failure as a lesson, or reinforce an existing one. True if new."""
        sig = _signature(tool, args)
        existing = self._find_by_source(sig, user)
        if existing:
            new_conf = self._reinforce(existing["id"], existing["confidence"])
            log.info("learning: reinforced lesson for %s (confidence %.2f)", tool, new_conf)
            return False
        text = f"Tool {tool} failed: {error} (args {_compact(args)})"
        await self.memory.remember(
            text, kind=_KIND, user=user, tags=["lesson", "failure", tool],
            confidence=_CONF[0], source=sig)
        return True

    def _find_by_source(self, source: str, user: str) -> dict[str, Any] | None:
        rows = self.memory.db.query(
            "SELECT id,confidence FROM memory "
            "WHERE user=? AND kind=? AND source=? AND deleted=0 LIMIT 1",
            (user, _KIND, source))
        return rows[0] if rows else None

    def _reinforce(self, mem_id: str, current: float) -> float:
        idx = 0
        for i, c in enumerate(_CONF):
            if current >= c - 1e-9:
                idx = i
        new_conf = _CONF[min(idx + 1, len(_CONF) - 1)]
        self.memory.db.execute(
            "UPDATE memory SET confidence=?, updated_at=? WHERE id=?",
            (new_conf, now_iso(), mem_id))
        self.memory.db.audit("learning.reinforce", target=mem_id,
                             detail={"confidence": new_conf})
        return new_conf
