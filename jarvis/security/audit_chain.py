"""Audit-chain integrity: verify the hash chain and anchor it outside the database.

Every audit row is chained to its predecessor by SHA-256 (see
:meth:`jarvis.db.Database.audit`), so changing or deleting a row in the middle
breaks every later link and :meth:`Database.verify_audit` reports where. Trimming
rows from the *end* leaves a chain that still links correctly, which is why a
short anchor — the head's id and hash — is also kept in a file the database
cannot rewrite (``data/audit-anchor.json``) and re-checked on the next start.

Two integrity rules the reviewers insisted on:

  * a **missing or unreadable** anchor file is a failure once an anchor has ever
    been recorded (a marker is kept in the database kv), not a silent pass — so
    deleting the anchor cannot hide a truncated tail;
  * the anchor is only advanced when the chain verifies, so a detected break is
    **not** re-anchored away.

The chain is self-consistent, not signed: an actor who can write the database
*and* recompute every hash is not detected. That limit is stated, not hidden.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("jarvis.security.audit")
__all__ = ["ANCHOR_NAME", "ANCHOR_KEY", "anchor_path", "verify", "verify_and_anchor"]

ANCHOR_NAME = "audit-anchor.json"
# kv key recording that an anchor has been written (so a *missing* file is an error).
ANCHOR_KEY = "audit_anchor"


def anchor_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / ANCHOR_NAME


def _anchor_id(anchor: Any) -> int | None:
    """The anchor's id as an int, or None if it is not a usable integer."""
    if not isinstance(anchor, dict):
        return None
    try:
        return int(anchor.get("id"))
    except (TypeError, ValueError):
        return None


def _recorded_marker(db: Any) -> Any:
    try:
        return db.kv_get(ANCHOR_KEY)
    except Exception:  # pragma: no cover - kv must never break verification
        return None


def verify(db: Any, data_dir: str | Path) -> dict[str, Any]:
    """Verify the chain against the stored anchor. Never raises."""
    path = anchor_path(data_dir)
    anchor = db.read_anchor(path)
    result: dict[str, Any]
    if anchor is None and _recorded_marker(db):
        # An anchor was written before but the file is now gone or unparseable:
        # that is itself evidence, not a first run.
        result = {"ok": False, "checked": 0, "legacy": 0, "broken_at": None,
                  "reason": ("the audit anchor is missing or unreadable, but one was "
                             "recorded before (the tail may have been truncated)")}
    else:
        # An id of 0 means "the chain was empty when anchored" — there is no row to
        # match. Any other value (including a malformed one) is passed through so
        # verify_audit fails closed on it.
        use = None if _anchor_id(anchor) == 0 else anchor
        result = db.verify_audit(use)
        result["anchored"] = bool(use)
    result["anchor_file"] = str(path)
    return result


def verify_and_anchor(db: Any, data_dir: str | Path) -> dict[str, Any]:
    """Verify, then advance the anchor **only if** the chain is intact."""
    result = verify(db, data_dir)
    if result.get("ok"):
        written = db.write_anchor(anchor_path(data_dir))
        try:
            db.kv_set(ANCHOR_KEY, written)
        except Exception:  # pragma: no cover - the marker is best-effort, not the proof
            log.warning("could not record the audit anchor marker")
        result["anchor_written"] = written
    else:
        log.error("audit chain verification failed: %s", result.get("reason"))
    return result
