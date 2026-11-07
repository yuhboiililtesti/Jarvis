"""Memory: durable, provenance-tracked, per-user isolated recall.

Storage is SQLite; retrieval is hybrid — SQLite FTS5 keyword ranking fused with
vector cosine when an embedding model is available. If embeddings are
unavailable, retrieval silently degrades to keyword-only and says so in the
returned evidence rather than pretending a semantic match occurred.
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..ids import new_id, now_iso
from ..logging_setup import get_logger

log = get_logger("jarvis.memory")

__all__ = ["MemoryItem", "MemoryStore"]


@dataclass
class MemoryItem:
    id: str
    text: str
    kind: str = "fact"
    user: str = "owner"
    source: str | None = None
    confidence: float = 0.8
    private: bool = True
    pinned: bool = False
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


def _pack(vec: Iterable[float]) -> bytes:
    v = list(vec)
    return struct.pack(f"<{len(v)}f", *v)


def _unpack(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class MemoryStore:
    def __init__(self, db: Any, router: Any = None) -> None:
        self.db = db
        self.router = router            # ModelRouter (optional)

    # -- writes ----------------------------------------------------------
    async def remember(self, text: str, *, kind: str = "fact", user: str = "owner",
                       source: str | None = None, confidence: float = 0.8,
                       private: bool = True, tags: list[str] | None = None,
                       pinned: bool = False) -> MemoryItem:
        item = MemoryItem(id=new_id("mem"), text=text.strip(), kind=kind, user=user,
                          source=source, confidence=confidence, private=private,
                          pinned=pinned, tags=tags or [],
                          created_at=now_iso(), updated_at=now_iso())
        blob = None
        if self.router is not None:
            try:
                vec = await self.router.embed(item.text)
                if vec:
                    blob = _pack(vec)
            except Exception as exc:  # pragma: no cover
                log.debug("embed failed: %s", exc)
        self.db.execute(
            "INSERT INTO memory(id,created_at,updated_at,user,kind,text,source,confidence,"
            "private,pinned,embedding,tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (item.id, item.created_at, item.updated_at, item.user, item.kind, item.text,
             item.source, item.confidence, int(item.private), int(item.pinned), blob,
             json.dumps(item.tags)),
        )
        self.db.execute("INSERT INTO memory_fts(rowid,text,tags) VALUES("
                        "(SELECT rowid FROM memory WHERE id=?),?,?)",
                        (item.id, item.text, " ".join(item.tags)))
        return item

    def forget(self, mem_id: str, *, hard: bool = False) -> bool:
        if hard:
            self.db.execute("DELETE FROM memory WHERE id=?", (mem_id,))
            self.db.execute("DELETE FROM memory_fts WHERE rowid NOT IN (SELECT rowid FROM memory)")
        else:
            self.db.execute("UPDATE memory SET deleted=1, updated_at=? WHERE id=?",
                            (now_iso(), mem_id))
        self.db.audit("memory.forget", target=mem_id, detail={"hard": hard})
        return True

    def correct(self, mem_id: str, new_text: str) -> bool:
        self.db.execute("UPDATE memory SET text=?, updated_at=? WHERE id=?",
                        (new_text, now_iso(), mem_id))
        self.db.audit("memory.correct", target=mem_id)
        return True

    def export(self, user: str = "owner") -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id,created_at,kind,text,source,confidence,tags FROM memory "
            "WHERE user=? AND deleted=0 ORDER BY created_at", (user,))
        return [dict(r) for r in rows]

    # -- reads -----------------------------------------------------------
    async def search(self, query: str, *, user: str = "owner", top_k: int = 6,
                     kinds: list[str] | None = None) -> dict[str, Any]:
        evidence: dict[str, Any] = {"method": "keyword"}
        # keyword leg
        kw: dict[str, float] = {}
        try:
            rows = self.db.query(
                "SELECT m.id AS id, bm25(memory_fts) AS score FROM memory_fts "
                "JOIN memory m ON m.rowid = memory_fts.rowid "
                "WHERE memory_fts MATCH ? AND m.deleted=0 AND m.user=? "
                "ORDER BY rank LIMIT ?",
                (_fts_query(query), user, top_k * 3),
            )
            for r in rows:
                kw[r["id"]] = 1.0 / (1.0 + abs(float(r["score"] or 0.0)))
        except Exception:
            pass

        # vector leg
        vec: dict[str, float] = {}
        qvec: list[float] = []
        if self.router is not None:
            try:
                qvec = await self.router.embed(query)
            except Exception:
                qvec = []
        if qvec:
            rows = self.db.query(
                "SELECT id, embedding FROM memory WHERE deleted=0 AND user=? "
                "AND embedding IS NOT NULL", (user,))
            for r in rows:
                score = _cosine(qvec, _unpack(r["embedding"]))
                if score > 0.25:
                    vec[r["id"]] = score
            if vec:
                evidence["method"] = "hybrid"

        fused: dict[str, float] = {}
        for mid, s in kw.items():
            fused[mid] = fused.get(mid, 0.0) + 0.5 * s
        for mid, s in vec.items():
            fused[mid] = fused.get(mid, 0.0) + 0.5 * s
        if not fused:
            return {"items": [], "evidence": evidence}

        order = sorted(fused, key=lambda k: fused[k], reverse=True)[:top_k]
        placeholders = ",".join("?" for _ in order)
        rows = self.db.query(
            f"SELECT id,created_at,kind,text,source,confidence,private,tags,pinned "
            f"FROM memory WHERE id IN ({placeholders}) AND deleted=0", order)
        by_id = {r["id"]: r for r in rows}
        items = []
        for mid in order:
            r = by_id.get(mid)
            if not r:
                continue
            if kinds and r["kind"] not in kinds:
                continue
            items.append({
                "id": r["id"], "text": r["text"], "kind": r["kind"],
                "source": r["source"], "confidence": r["confidence"],
                "private": bool(r["private"]), "pinned": bool(r["pinned"]),
                "tags": json.loads(r["tags"] or "[]"),
                "score": round(fused[mid], 4),
                "created_at": r["created_at"],
            })
        return {"items": items, "evidence": evidence}

    def context_block(self, items: list[dict[str, Any]], limit: int = 1200) -> str:
        """Render recalled memory for the prompt, respecting privacy.

        Everything here is labelled as historical on purpose. Recalled notes are
        a record of what was said before, not a source of current readings — the
        model was previously happy to answer "what time is it" with a timestamp
        from its own earlier reply, which is exactly the failure this header
        exists to prevent.
        """
        if not items:
            return ""
        lines = [
            "RECALLED MEMORY — historical notes about past conversations and stored "
            "preferences. This is NOT current data. Do not reuse any value from here as "
            "a present-day reading; call a tool for anything that can change.",
            "",
        ]
        for it in items:
            tag = "private" if it["private"] else "shared"
            kind = it["kind"]
            if kind == "episode":
                lines.append(f"- [past exchange/{tag}] {it['text']}")
            else:
                lines.append(f"- [{kind}/{tag}] {it['text']}")
            if sum(len(x) for x in lines) > limit:
                break
        return "\n".join(lines)


def _fts_query(q: str) -> str:
    """Sanitise a user string into a safe FTS5 MATCH expression."""
    tokens = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in q).split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens[:12])
