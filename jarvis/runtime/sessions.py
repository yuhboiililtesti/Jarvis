"""Durable conversation sessions (working memory that survives a restart).

The Core used to keep ``{"id", "messages"}`` in an in-process dict, so closing it
threw the working context away and a restart began from nothing. Sessions are now
stored in SQLite: the same interface, but the conversation survives.

The message list is the model's chat history, so it is capped (oldest dropped) to
bound both storage and the prompt. Only the fields ``ChatMessage`` defines are
persisted; a malformed row degrades to an empty session rather than failing a
turn.
"""

from __future__ import annotations

import json
from typing import Any

from ..ids import now_iso
from ..llm import ChatMessage
from ..logging_setup import get_logger

log = get_logger("jarvis.runtime.sessions")

__all__ = ["SessionStore"]

_MESSAGE_KEYS = ("role", "content", "name", "tool_calls", "tool_call_id")


def _encode(messages: list[Any]) -> str:
    out = []
    for m in messages:
        if isinstance(m, ChatMessage):
            out.append(m.to_dict())
        elif isinstance(m, dict):
            out.append({k: m.get(k) for k in _MESSAGE_KEYS if m.get(k) is not None})
        else:  # pragma: no cover - defensive
            continue
    return json.dumps(out, ensure_ascii=False)


def _decode(raw: str | None) -> list[ChatMessage]:
    try:
        rows = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        log.warning("discarding an unreadable session message list")
        return []
    if not isinstance(rows, list):
        return []
    out: list[ChatMessage] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("role"):
            continue
        out.append(ChatMessage(
            role=str(row.get("role")),
            content=str(row.get("content") or ""),
            name=row.get("name"),
            tool_calls=row.get("tool_calls"),
            tool_call_id=row.get("tool_call_id"),
        ))
    return out


def _trim(messages: list[Any], limit: int) -> list[Any]:
    """Keep the system prompt and the most recent *whole* exchanges.

    A naive slice can begin with a ``tool`` message whose ``assistant``
    ``tool_calls`` message was cut, which providers reject. Drop those leading
    orphans; never drop the system message.
    """
    limit = max(2, int(limit))
    if len(messages) <= limit:
        return messages
    head: list[Any] = []
    rest = messages
    if messages and getattr(messages[0], "role", None) == "system":
        head = [messages[0]]
        rest = messages[1:]
        keep = max(1, limit - 1)
    else:
        keep = limit
    tail = list(rest[-keep:])
    while tail and getattr(tail[0], "role", None) == "tool":
        tail.pop(0)
    return head + tail


class SessionStore:
    """SQLite-backed ``{id, messages}`` store with a bounded history."""

    def __init__(self, db: Any, max_messages: int = 200) -> None:
        self.db = db
        # Honour the configured cap; a floor of 2 keeps at least one exchange.
        self.max_messages = max(2, int(max_messages or 200))

    def get(self, session_id: str = "default") -> dict[str, Any]:
        sid = str(session_id or "default")
        row = self.db.query_one("SELECT messages FROM sessions WHERE id=?", (sid,))
        return {"id": sid, "messages": _decode(row["messages"]) if row else []}

    def save(self, session: dict[str, Any]) -> None:
        sid = str(session.get("id") or "default")
        messages = _trim(list(session.get("messages") or []), self.max_messages)
        # Keep the in-process cache consistent with what was actually stored.
        session["messages"] = messages
        self.db.execute(
            "INSERT INTO sessions(id,updated_at,messages) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, "
            "messages=excluded.messages",
            (sid, now_iso(), _encode(messages)))

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, updated_at, messages FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (min(max(int(limit), 1), 500),))
        out = []
        for r in rows:
            try:
                count = len(json.loads(r["messages"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                count = 0
            out.append({"id": r["id"], "updated_at": r["updated_at"], "messages": count})
        return out

    def delete(self, session_id: str) -> bool:
        cur = self.db.execute("DELETE FROM sessions WHERE id=?", (str(session_id),))
        return cur.rowcount > 0

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM sessions")
        return int(row["n"]) if row else 0
