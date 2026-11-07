"""SQLite persistence: task journal, audit, memory, grants, notes, alerts.

One file database (WAL mode) keeps the Core dependency-free and trivially
backed up. Schema is versioned via `schema_version`; migrations are additive and
never renumber existing columns. All writes that must survive a crash before an
external effect are committed *before* the effect is attempted (see
jarvis.tasks.controller).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .ids import now_iso
from .logging_setup import get_logger, redact

log = get_logger("jarvis.db")

__all__ = ["Database"]

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL
);

-- ---------------------------------------------------------------- tasks
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'text',   -- text | voice | ui | api | schedule | node
    user         TEXT NOT NULL DEFAULT 'owner',
    request      TEXT NOT NULL,
    goal         TEXT,
    status       TEXT NOT NULL DEFAULT 'received',
    plan         TEXT,                            -- json
    result       TEXT,                            -- json
    error        TEXT,
    parent_id    TEXT,
    session_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

-- ------------------------------------------------------------- attempts
-- An attempt row is committed BEFORE any external effect. On restart,
-- attempts with status='running' are reconciled (outcome-unknown -> verify).
CREATE TABLE IF NOT EXISTS attempts (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    step_index   INTEGER NOT NULL DEFAULT 0,
    tool         TEXT NOT NULL,
    tool_version TEXT NOT NULL DEFAULT '1',
    args         TEXT,                            -- json (redacted)
    side_effect  TEXT NOT NULL DEFAULT 'none',
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    evidence     TEXT,                            -- json, read-back proof
    error        TEXT,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);

-- ---------------------------------------------------------------- audit
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor    TEXT NOT NULL DEFAULT 'jarvis',
    action   TEXT NOT NULL,
    target   TEXT,
    detail   TEXT,                                -- json
    severity TEXT NOT NULL DEFAULT 'info'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

-- --------------------------------------------------------------- memory
-- Durable memory with provenance and per-user isolation.
CREATE TABLE IF NOT EXISTS memory (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    user       TEXT NOT NULL DEFAULT 'owner',
    kind       TEXT NOT NULL DEFAULT 'fact',      -- fact | preference | episode | summary | routine
    text       TEXT NOT NULL,
    source     TEXT,                              -- provenance: task id / url / file
    confidence REAL NOT NULL DEFAULT 0.8,
    private    INTEGER NOT NULL DEFAULT 1,
    pinned     INTEGER NOT NULL DEFAULT 0,
    embedding  BLOB,
    tags       TEXT,                              -- json array
    deleted    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_user ON memory(user, deleted);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    text, tags, content='memory', content_rowid='rowid'
);

-- --------------------------------------------------------------- grants
CREATE TABLE IF NOT EXISTS grants (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    scope      TEXT NOT NULL,                     -- tool name or glob
    target     TEXT,
    granted_to TEXT NOT NULL DEFAULT 'owner',
    used_count INTEGER NOT NULL DEFAULT 0,
    max_uses   INTEGER,
    revoked    INTEGER NOT NULL DEFAULT 0,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_grants_scope ON grants(scope, revoked);

-- ------------------------------------------------------- approvals queue
CREATE TABLE IF NOT EXISTS approvals (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    task_id    TEXT,
    tool       TEXT NOT NULL,
    args       TEXT,
    side_effect TEXT NOT NULL DEFAULT 'none',
    reason     TEXT,
    decision   TEXT NOT NULL DEFAULT 'pending',   -- pending | allow | deny | timeout
    decided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_pending ON approvals(decision);

-- ---------------------------------------------------------------- notes
CREATE TABLE IF NOT EXISTS notes (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    tags       TEXT,
    user       TEXT NOT NULL DEFAULT 'owner',
    deleted    INTEGER NOT NULL DEFAULT 0
);

-- --------------------------------------------------------------- alerts
CREATE TABLE IF NOT EXISTS alerts (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    severity   TEXT NOT NULL DEFAULT 'info',      -- info | warn | critical
    source     TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT,
    evidence   TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    delivered  TEXT                              -- json: channels actually reached
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);

-- ---------------------------------------------------------------- nodes
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    base_url    TEXT,
    kind        TEXT NOT NULL DEFAULT 'node',     -- node | satellite
    first_seen  TEXT NOT NULL,
    last_seen   TEXT,
    capabilities TEXT,                            -- json
    status      TEXT NOT NULL DEFAULT 'unknown',
    token_hint  TEXT
);

-- ------------------------------------------------------------ night runs
CREATE TABLE IF NOT EXISTS night_runs (
    id          TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    objective   TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    report_path TEXT,
    summary     TEXT
);

-- ------------------------------------------------------------ key/value
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

-- ----------------------------------------------------------- metrics ring
CREATE TABLE IF NOT EXISTS metrics (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    name   TEXT NOT NULL,
    value  REAL NOT NULL,
    label  TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_name_ts ON metrics(name, ts);

-- ------------------------------------------------------- runtime: schedules
-- A run row is the durable claim for one *occurrence* of a job. The unique
-- (job_id, due_key) pair is what makes firing at-most-once across restarts.
CREATE TABLE IF NOT EXISTS scheduler_runs (
    id         TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL,
    due_key    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | running | succeeded | failed | missed | outcome-unknown
    task_id    TEXT,
    started_at TEXT,
    ended_at   TEXT,
    error      TEXT,
    UNIQUE(job_id, due_key)
);
CREATE INDEX IF NOT EXISTS idx_sched_runs_job ON scheduler_runs(job_id, status);
CREATE INDEX IF NOT EXISTS idx_sched_runs_created ON scheduler_runs(started_at);

-- --------------------------------------------------------- runtime: events
CREATE TABLE IF NOT EXISTS runtime_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    topic      TEXT NOT NULL,
    source     TEXT,
    data       TEXT,                               -- json
    dedupe_key TEXT,                               -- optional idempotency key from the producer
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runtime_events_topic ON runtime_events(topic, id);

CREATE TABLE IF NOT EXISTS event_rules (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    topic      TEXT NOT NULL,                      -- fnmatch pattern
    condition  TEXT,                               -- json {field,op,value}
    action     TEXT NOT NULL,                      -- json {kind,...}
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- One row per (rule, event): a restart can never fire the same event twice.
CREATE TABLE IF NOT EXISTS event_fires (
    id          TEXT PRIMARY KEY,
    rule_id     TEXT NOT NULL,
    event_id    INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | running | succeeded | failed | outcome-unknown
    task_id     TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT,
    UNIQUE(rule_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_event_fires_status ON event_fires(status);

-- ------------------------------------------------------- runtime: sessions
-- Durable working memory: the conversation context survives a restart.
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    messages   TEXT NOT NULL DEFAULT '[]'          -- json array of ChatMessage dicts
);

-- ---------------------------------------------------------------- goals
-- A durable multi-step plan. Steps execute one at a time through the normal
-- controller, so policy/approval/verification still apply.
CREATE TABLE IF NOT EXISTS goals (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    request     TEXT NOT NULL,
    user        TEXT NOT NULL DEFAULT 'owner',
    session_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'received',
    plan        TEXT,                              -- json
    result      TEXT,
    error       TEXT,
    steps_total INTEGER NOT NULL DEFAULT 0,
    steps_done  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);

CREATE TABLE IF NOT EXISTS goal_steps (
    id         TEXT PRIMARY KEY,
    goal_id    TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    kind       TEXT NOT NULL,                      -- tool | request | answer
    tool       TEXT,
    args       TEXT,                               -- json
    prompt     TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',    -- pending|running|succeeded|failed|skipped|cancelled|outcome-unknown
    attempts   INTEGER NOT NULL DEFAULT 0,
    result     TEXT,
    error      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(goal_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_goal_steps_goal ON goal_steps(goal_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_goal_steps_status ON goal_steps(status);
"""


_GENESIS = "0" * 64


def _audit_hash(prev: str, ts: str, actor: str, action: str, target: str | None,
                detail: str | None, severity: str) -> str:
    # Length-prefixed fields so ("ab","c") and ("a","bc") can never collide.
    # ``prev`` should be 64 hex chars; a tampered/garbage value is encoded with
    # replacement rather than raising, so audit() keeps working and verify_audit
    # reports the break it caused.
    h = hashlib.sha256(prev.encode("ascii", "replace"))
    for field_ in (ts, actor, action, target, detail, severity):
        data = b"\xff" if field_ is None else str(field_).encode("utf-8")
        h.update(len(data).to_bytes(8, "big") + data)
    return h.hexdigest()


def _anchor_id_hash(anchor: Any) -> tuple[int, str] | None:
    """Validate an external anchor dict; None when it is not `{"id": int, "hash": str}`."""
    if not isinstance(anchor, dict):
        return None
    aid, ahash = anchor.get("id"), anchor.get("hash")
    if isinstance(aid, bool) or not isinstance(aid, int):   # bool is an int subclass
        return None
    if not isinstance(ahash, str) or not ahash:
        return None
    return aid, ahash


class Database:
    """Thread-safe-ish SQLite wrapper (one connection guarded by a lock)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # Optional value-based scrubber (installed by the kernel after the
        # secret store exists). The audit log stores target/detail verbatim
        # otherwise, so a secret pasted into a request would be captured.
        self._redactor: Any = None
        self._init_schema()

    def set_redactor(self, redactor: Any) -> None:
        """Install ``fn(str) -> str`` used to scrub audit target/detail values."""
        self._redactor = redactor if callable(redactor) else None

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version(version) VALUES (?)",
                                   (SCHEMA_VERSION,))
            # Additive migrations. Never renumber or drop an existing column.
            additions = {
                "audit": {"prev_hash": "TEXT", "hash": "TEXT"},
                "runtime_events": {"dedupe_key": "TEXT"},
            }
            for table, columns in additions.items():
                existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                for col, decl in columns.items():
                    if col not in existing:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            # Indexes that depend on an additive column must be created after it.
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_events_dedupe "
                "ON runtime_events(dedupe_key) WHERE dedupe_key IS NOT NULL")
            self._conn.commit()
        log.info("database ready at %s (schema v%d)", self.path, SCHEMA_VERSION)

    # -- primitives ------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    # -- kv helpers ------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return row["value"]

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), now_iso()),
        )

    # -- audit -----------------------------------------------------------
    def audit(self, action: str, *, actor: str = "jarvis", target: str | None = None,
              detail: Any = None, severity: str = "info") -> None:
        """Append one audit row, chained to the previous row by SHA-256.

        Editing, inserting or deleting a row in the middle of the chain breaks
        every later link, which :meth:`verify_audit` reports. Removing rows from
        the *end* is caught only by comparing against an external anchor
        (:meth:`audit_head` written outside the database).
        """
        ts = now_iso()
        detail = redact(detail) if detail is not None else None
        detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else None
        if self._redactor is not None:
            # Apply to the serialised form too, so a secret in a nested value or
            # an unlabelled field is still replaced. The chain hashes exactly
            # what is stored, so verification stays consistent.
            if target is not None:
                target = self._redactor(target)
            if detail_json is not None:
                detail_json = self._redactor(detail_json)
        with self._lock:
            # BEGIN IMMEDIATE takes the database write lock *before* reading the
            # previous hash, so two processes (serve + CLI, or two Cores) cannot
            # both chain onto the same head. Without this the chain "breaks"
            # during perfectly legitimate concurrent use.
            own_txn = not self._conn.in_transaction
            try:
                if own_txn:
                    self._conn.execute("BEGIN IMMEDIATE")
                last = self._conn.execute(
                    "SELECT hash FROM audit WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1"
                ).fetchone()
                prev = last["hash"] if last else _GENESIS
                digest = _audit_hash(prev, ts, actor, action, target, detail_json, severity)
                self._conn.execute(
                    "INSERT INTO audit(ts,actor,action,target,detail,severity,prev_hash,hash) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (ts, actor, action, target, detail_json, severity, prev, digest))
                if own_txn:
                    self._conn.commit()
            except Exception:
                if own_txn:
                    self._conn.rollback()
                raise

    def audit_head(self) -> dict[str, Any] | None:
        row = self.query_one("SELECT id, hash FROM audit WHERE hash IS NOT NULL "
                             "ORDER BY id DESC LIMIT 1")
        return {"id": row["id"], "hash": row["hash"]} if row else None

    def verify_audit(self, anchor: dict[str, Any] | None = None) -> dict[str, Any]:
        """Walk the chain. Rows written before the migration (no hash) are legacy.

        With ``anchor`` (a previously recorded :meth:`audit_head`, kept outside
        the database) the walk is checked against that final link *after* it
        succeeds, so trimming rows from the tail is reported even though the
        remaining chain still links correctly. A missing or malformed anchor row
        is a failure, never a silent pass.
        """
        valid: tuple[int, str] | None = None
        if anchor is not None:
            valid = _anchor_id_hash(anchor)
            if valid is None:
                return {"ok": False, "reason": "anchor is malformed",
                        "checked": 0, "legacy": 0}
        legacy = checked = 0
        prev = _GENESIS
        started = False
        for r in self.query("SELECT id,ts,actor,action,target,detail,severity,prev_hash,hash "
                            "FROM audit ORDER BY id"):
            if r["hash"] is None:
                if started:   # an unchained row after the chain began was inserted around it
                    return {"ok": False, "broken_at": r["id"], "checked": checked,
                            "legacy": legacy, "reason": "row without hash inside the chain"}
                legacy += 1
                continue
            started = True
            if r["prev_hash"] != prev:
                return {"ok": False, "broken_at": r["id"], "checked": checked, "legacy": legacy,
                        "reason": "link mismatch (a row before this one was changed or removed)"}
            expect = _audit_hash(prev, r["ts"], r["actor"], r["action"], r["target"],
                                 r["detail"], r["severity"])
            if r["hash"] != expect:
                return {"ok": False, "broken_at": r["id"], "checked": checked, "legacy": legacy,
                        "reason": "content changed after it was written"}
            prev = r["hash"]
            checked += 1
        result: dict[str, Any] = {"ok": True, "checked": checked, "legacy": legacy, "head": prev}
        if valid is None:                     # no anchor was supplied: the walk is all we know
            return result
        aid, ahash = valid
        row = self.query_one("SELECT hash FROM audit WHERE id=?", (aid,))
        if row is None:
            return {**result, "ok": False, "broken_at": aid, "reason":
                    "anchored row is missing (audit tail was truncated or the log was reset)"}
        if row["hash"] != ahash:
            return {**result, "ok": False, "broken_at": aid, "reason":
                    "anchored head hash does not match (history was rewritten)"}
        result["anchor"] = "ok"
        return result

    def write_anchor(self, path: str | Path) -> dict[str, Any]:
        """Record the current head *outside* the database, to catch tail truncation later.

        Returns the anchor it wrote (``{"id": 0, "hash": _GENESIS}`` for an empty
        chain). Keep the file somewhere the database cannot rewrite; pass it back
        to :meth:`verify_audit` after a restart as ``db.verify_audit(db.read_anchor(p))``.
        """
        anchor = self.audit_head() or {"id": 0, "hash": _GENESIS}
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a partial write can never leave an unreadable anchor
        # (which would look like tampering on the next start).
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(anchor), encoding="utf-8")
        os.replace(tmp, p)
        return anchor

    def read_anchor(self, path: str | Path) -> dict[str, Any] | None:
        """Read an anchor written by :meth:`write_anchor`; None if absent or unparseable.

        Only a JSON object is returned (a list, scalar or corrupt file gives None).
        Its shape is validated by :meth:`verify_audit`, which fails closed on a
        malformed anchor rather than ignoring it.
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):         # missing/unreadable file, bad JSON/encoding
            return None
        return data if isinstance(data, dict) else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
