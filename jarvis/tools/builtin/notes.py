"""Note tools: create, list, read and delete notes in the shared SQLite store.

Notes live in the `notes` table (see jarvis.db). Writes and deletes carry a
side-effect class so policy gates them, and both provide a real read-back hook so
the verifier can confirm the effect instead of trusting the tool's own word.
"""

from __future__ import annotations

import json
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...ids import new_id, now_iso
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.notes")

__all__ = ["NoteCreateTool", "NoteListTool", "NoteReadTool", "NoteDeleteTool"]


def _db(ctx: ToolContext):
    if ctx.db is None:
        return None
    return ctx.db


def _parse_tags(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    try:
        return [str(t) for t in raw]
    except TypeError:
        return []


def _tags_of(row: Any) -> list[str]:
    try:
        return json.loads(row["tags"] or "[]")
    except (TypeError, ValueError):
        return []


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "tags": _tags_of(row),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deleted": bool(row["deleted"]),
    }


@register(["note.add", "notes_add", "note_create"])
class NoteCreateTool(BaseTool):
    spec = ToolSpec(
        name="notes.create",
        summary="Create a note with a title, body and optional tags.",
        category="notes",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("title", "string", "Note title. Required.", required=True),
            ToolParam("body", "string", "Note body text.", default=""),
            ToolParam("tags", "array", "Optional tags.", default=None),
        ),
        returns="object with the new note id and its fields",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        db = _db(ctx)
        if db is None:
            return ToolResult(ok=False, error="database is unavailable on the tool context")
        try:
            title = str(kwargs["title"]).strip()
            if not title:
                return ToolResult(ok=False, error="title must not be empty")
            body = str(kwargs.get("body") or "")
            tags = _parse_tags(kwargs.get("tags"))
            note_id = new_id("note")
            ts = now_iso()
            db.execute(
                "INSERT INTO notes(id,created_at,updated_at,title,body,tags,user,deleted) "
                "VALUES(?,?,?,?,?,?,?,0)",
                (note_id, ts, ts, title, body, json.dumps(tags), ctx.user),
            )
            return ToolResult(ok=True, data={
                "id": note_id, "title": title, "body": body, "tags": tags,
                "user": ctx.user, "created_at": ts, "updated_at": ts,
            })
        except Exception as exc:
            log.warning("notes.create failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def verify(self, ctx: ToolContext, result: ToolResult) -> dict[str, Any] | None:
        if not result.ok or not isinstance(result.data, dict):
            return {"verified": False, "method": "skipped"}
        if ctx.db is None:
            return None
        row = ctx.db.query_one("SELECT * FROM notes WHERE id=?",
                               (result.data.get("id"),))
        if row is None:
            return {"verified": False, "method": "sqlite-readback",
                    "note": "row not found"}
        return {"verified": (not bool(row["deleted"])) and row["title"] == result.data.get("title"),
                "method": "sqlite-readback", "id": row["id"], "title": row["title"]}


@register(["notes", "note.list", "notes_list"])
class NoteListTool(BaseTool):
    spec = ToolSpec(
        name="notes.list",
        summary="List notes, most recent first.",
        category="notes",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("limit", "integer", "Maximum notes to return.", default=20),
            ToolParam("include_deleted", "boolean", "Include soft-deleted notes.",
                      default=False),
        ),
        returns="object with a list of notes",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        db = _db(ctx)
        if db is None:
            return ToolResult(ok=False, error="database is unavailable on the tool context")
        try:
            try:
                limit = int(kwargs.get("limit") or 20)
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="limit must be an integer")
            limit = max(1, min(limit, 500))
            include_deleted = bool(kwargs.get("include_deleted") or False)
            where = "" if include_deleted else "WHERE deleted=0 AND user=?"
            params: tuple[Any, ...] = () if include_deleted else (ctx.user,)
            rows = db.query(
                f"SELECT * FROM notes {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                params + (limit,),
            )
            notes = [_row_to_dict(r) for r in rows]
            return ToolResult(ok=True, data={"count": len(notes), "notes": notes})
        except Exception as exc:
            log.warning("notes.list failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["note.get", "note.read", "note_read"])
class NoteReadTool(BaseTool):
    spec = ToolSpec(
        name="notes.read",
        summary="Read a single note by id.",
        category="notes",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("id", "string", "Note id. Required.", required=True),
        ),
        returns="object with the note fields",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        db = _db(ctx)
        if db is None:
            return ToolResult(ok=False, error="database is unavailable on the tool context")
        try:
            row = db.query_one("SELECT * FROM notes WHERE id=?", (str(kwargs["id"]),))
            if row is None:
                return ToolResult(ok=False, error=f"note '{kwargs['id']}' not found")
            return ToolResult(ok=True, data=_row_to_dict(row))
        except Exception as exc:
            log.warning("notes.read failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["note.delete", "note_remove", "notes_remove"])
class NoteDeleteTool(BaseTool):
    spec = ToolSpec(
        name="notes.delete",
        summary="Delete a note by id (soft delete, or hard with hard=true).",
        category="notes",
        side_effect=SideEffect.DESTRUCTIVE,
        params=(
            ToolParam("id", "string", "Note id to delete. Required.", required=True),
            ToolParam("hard", "boolean", "Purge the row entirely.", default=False),
        ),
        returns="object confirming the note id was deleted",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        db = _db(ctx)
        if db is None:
            return ToolResult(ok=False, error="database is unavailable on the tool context")
        try:
            note_id = str(kwargs["id"]).strip()
            hard = bool(kwargs.get("hard") or False)
            row = db.query_one("SELECT id FROM notes WHERE id=?", (note_id,))
            if row is None:
                return ToolResult(ok=False, error=f"note '{note_id}' not found")
            if hard:
                db.execute("DELETE FROM notes WHERE id=?", (note_id,))
            else:
                db.execute("UPDATE notes SET deleted=1, updated_at=? WHERE id=?",
                           (now_iso(), note_id))
            return ToolResult(ok=True, data={"id": note_id, "hard": hard, "deleted": True})
        except Exception as exc:
            log.warning("notes.delete failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def verify(self, ctx: ToolContext, result: ToolResult) -> dict[str, Any] | None:
        if not result.ok or not isinstance(result.data, dict):
            return {"verified": False, "method": "skipped"}
        if ctx.db is None:
            return None
        note_id = str(result.data.get("id"))
        hard = bool(result.data.get("hard"))
        row = ctx.db.query_one("SELECT id,deleted FROM notes WHERE id=?", (note_id,))
        if hard:
            return {"verified": row is None, "method": "sqlite-readback",
                    "id": note_id, "row_present": row is not None}
        return {"verified": bool(row is not None and row["deleted"]),
                "method": "sqlite-readback", "id": note_id,
                "deleted": bool(row["deleted"]) if row is not None else None}
