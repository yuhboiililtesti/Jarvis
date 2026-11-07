"""Audit hash chain: append chaining, tamper detection and external anchoring.

Tampering is simulated with direct SQL behind the ``Database.audit()`` API, the
way someone with file access would do it. The walk cannot see rows removed from
the tail; an anchor written outside the database can. No network, no model.
"""

from __future__ import annotations

import json

import pytest


def _seed(db, n: int = 3) -> list[int]:
    """Append n chained rows and return their ids in order."""
    for i in range(n):
        db.audit("test.action", target=f"t{i}", detail={"n": i})
    return [r["id"] for r in db.query("SELECT id FROM audit ORDER BY id")]


# ----------------------------------------------------------------- the happy path
def test_fresh_chain_verifies_and_counts_every_row(db):
    ids = _seed(db, 3)
    rep = db.verify_audit()
    assert rep["ok"] is True
    assert rep["checked"] == len(ids) == 3
    assert rep["legacy"] == 0


def test_audit_head_is_the_last_hash_and_anchor_round_trips(db, tmp_path):
    _seed(db, 3)
    head = db.audit_head()
    last = db.query_one("SELECT id, hash FROM audit ORDER BY id DESC LIMIT 1")
    assert head == {"id": last["id"], "hash": last["hash"]}

    p = tmp_path / "sub" / "audit.anchor"
    written = db.write_anchor(p)
    assert written == head                      # parent dirs created, head returned
    assert db.read_anchor(p) == head
    assert db.verify_audit(db.read_anchor(p))["anchor"] == "ok"


def test_write_anchor_on_empty_chain_writes_genesis(db, tmp_path):
    p = tmp_path / "audit.anchor"
    assert db.audit_head() is None
    assert db.write_anchor(p) == {"id": 0, "hash": "0" * 64}
    assert db.read_anchor(p) == {"id": 0, "hash": "0" * 64}


def test_read_anchor_missing_or_unparseable_returns_none(db, tmp_path):
    assert db.read_anchor(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert db.read_anchor(bad) is None
    bad.write_text("[1, 2]", encoding="utf-8")   # valid JSON, not an object
    assert db.read_anchor(bad) is None


# ----------------------------------------------------------------- tamper in the middle
def test_edited_middle_row_fails_content_check(db):
    ids = _seed(db, 3)
    mid = ids[1]
    db.execute("UPDATE audit SET detail=? WHERE id=?", (json.dumps({"n": 99}), mid))
    rep = db.verify_audit()
    assert rep["ok"] is False
    assert rep["broken_at"] == mid
    assert rep["reason"] == "content changed after it was written"


def test_deleted_middle_row_fails_link_check(db):
    ids = _seed(db, 3)
    db.execute("DELETE FROM audit WHERE id=?", (ids[1],))
    rep = db.verify_audit()
    assert rep["ok"] is False and rep["broken_at"] == ids[2]
    assert "link mismatch" in rep["reason"]


def test_row_without_hash_inside_the_chain_is_reported(db):
    ids = _seed(db, 3)
    db.execute("DELETE FROM audit WHERE id=?", (ids[1],))
    db.execute("INSERT INTO audit(id,ts,actor,action,severity) VALUES(?,?,?,?,?)",
               (ids[1], "2020-01-01T00:00:00+00:00", "attacker", "sneaky", "info"))
    rep = db.verify_audit()
    assert rep["ok"] is False and rep["broken_at"] == ids[1]
    assert rep["reason"] == "row without hash inside the chain"


# ----------------------------------------------------------------- the tail needs an anchor
def test_deleted_tail_is_invisible_to_the_walk_but_caught_by_the_anchor(db, tmp_path):
    _seed(db, 3)
    anchor = db.write_anchor(tmp_path / "audit.anchor")
    last = db.query_one("SELECT id FROM audit ORDER BY id DESC LIMIT 1")["id"]
    db.execute("DELETE FROM audit WHERE id=?", (last,))

    plain = db.verify_audit()
    assert plain["ok"] is True and plain["checked"] == 2       # the walk alone cannot see it

    anchored = db.verify_audit(db.read_anchor(tmp_path / "audit.anchor"))
    assert anchored["ok"] is False and anchored["broken_at"] == anchor["id"]
    assert "truncated" in anchored["reason"]


def test_anchor_hash_mismatch_reports_a_rewrite(db):
    _seed(db, 3)
    head = db.audit_head()
    rep = db.verify_audit({"id": head["id"], "hash": "f" * 64})
    assert rep["ok"] is False and rep["broken_at"] == head["id"]
    assert "rewritten" in rep["reason"]


def test_anchor_pointing_at_a_missing_row_is_a_failure(db):
    _seed(db, 3)
    rep = db.verify_audit({"id": 9999, "hash": "0" * 64})
    assert rep["ok"] is False and rep["broken_at"] == 9999
    assert "missing" in rep["reason"]


# ----------------------------------------------------------------- legacy rows
def test_legacy_rows_before_the_chain_are_allowed_and_counted(db):
    for i in range(2):
        db.execute("INSERT INTO audit(ts,actor,action,severity) VALUES(?,?,?,?)",
                   ("2020-01-01T00:00:00+00:00", "legacy", f"old{i}", "info"))
    _seed(db, 3)
    rep = db.verify_audit()
    assert rep["ok"] is True and rep["legacy"] == 2 and rep["checked"] == 3


# ----------------------------------------------------------------- bad anchors fail closed
def test_malformed_anchor_is_reported_and_never_raises(db):
    _seed(db, 1)
    for bad in ({"nope": 1}, {"id": 1}, {"id": "1", "hash": "x"}, {"id": True, "hash": "x"}):
        rep = db.verify_audit(bad)
        assert rep["ok"] is False
        assert rep["reason"] == "anchor is malformed"


# ----------------------------------------------------------------- the startup helper
def test_verify_and_anchor_writes_then_catches_a_tail_deletion(db, tmp_path):
    from jarvis.security.audit_chain import verify, verify_and_anchor

    _seed(db, 3)
    res = verify_and_anchor(db, tmp_path)
    assert res["ok"] is True and "anchor_written" in res
    assert (tmp_path / "audit-anchor.json").is_file()
    assert verify(db, tmp_path)["ok"] is True

    last = db.query_one("SELECT id FROM audit ORDER BY id DESC LIMIT 1")["id"]
    db.execute("DELETE FROM audit WHERE id=?", (last,))
    bad = verify(db, tmp_path)
    assert bad["ok"] is False and "truncated" in bad["reason"]


def test_verify_and_anchor_refuses_to_reanchor_a_broken_chain(db, tmp_path):
    from jarvis.security.audit_chain import anchor_path, verify_and_anchor

    _seed(db, 3)
    verify_and_anchor(db, tmp_path)
    good = db.read_anchor(anchor_path(tmp_path))

    mid = db.query("SELECT id FROM audit ORDER BY id")[1]["id"]
    db.execute("UPDATE audit SET detail=? WHERE id=?", ("{}", mid))
    broken = verify_and_anchor(db, tmp_path)
    assert broken["ok"] is False and "anchor_written" not in broken
    assert db.read_anchor(anchor_path(tmp_path)) == good   # evidence preserved, not erased


# ----------------------------------------------------------------- over HTTP
def test_api_audit_verify_endpoint(kernel):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import create_app

    app = create_app(kernel)
    with fastapi_testclient.TestClient(app) as client:
        r = client.get("/api/audit/verify")
        assert r.status_code == 200
        body = r.json()
        assert "ok" in body and "checked" in body and "anchor_file" in body


# ----------------------------------------------------------------- review-driven hardening

def test_chain_verify_fails_closed_on_a_malformed_anchor_file(db, tmp_path):
    from jarvis.security.audit_chain import anchor_path, verify

    _seed(db, 2)
    anchor_path(tmp_path).write_text('{"id": "notanint", "hash": "x"}', encoding="utf-8")
    res = verify(db, tmp_path)             # must not raise (F2)
    assert res["ok"] is False and "malformed" in res["reason"]


def test_deleting_the_anchor_file_after_it_was_recorded_is_a_failure(db, tmp_path):
    from jarvis.security.audit_chain import anchor_path, verify, verify_and_anchor

    _seed(db, 3)
    verify_and_anchor(db, tmp_path)        # writes the file + the kv marker
    anchor_path(tmp_path).unlink()
    res = verify(db, tmp_path)
    assert res["ok"] is False and "missing or unreadable" in res["reason"]   # F3
    again = verify_and_anchor(db, tmp_path)
    assert again["ok"] is False and "anchor_written" not in again            # not re-anchored


def test_audit_survives_a_tampered_previous_hash(db):
    _seed(db, 2)
    last = db.query_one("SELECT id FROM audit ORDER BY id DESC LIMIT 1")["id"]
    db.execute("UPDATE audit SET hash=? WHERE id=?", ("\u00e9", last))
    db.audit("after.tamper")               # must not raise (F5)
    assert db.verify_audit()["ok"] is False  # and the tamper is still detected


def test_write_anchor_leaves_no_temp_file(db, tmp_path):
    from jarvis.security.audit_chain import anchor_path

    _seed(db, 1)
    db.write_anchor(anchor_path(tmp_path))
    assert anchor_path(tmp_path).is_file()
    assert list(tmp_path.glob("*.tmp")) == []


def test_two_writers_do_not_break_the_chain(tmp_path):
    """Regression: serve + a CLI command (or two Cores) must not fork the chain."""
    from jarvis.db import Database

    path = tmp_path / "shared.db"
    first, second = Database(path), Database(path)
    try:
        for i in range(30):
            first.audit("first.event", detail={"i": i})
            second.audit("second.event", detail={"i": i})
        report = first.verify_audit()
        assert report["ok"] is True, report
        assert report["checked"] == 60
    finally:
        first.close()
        second.close()

# ----------------------------------------------------------------- redaction (F11)
def test_audit_redacts_secret_values_at_the_write_boundary(db, cfg):
    from pathlib import Path
    from jarvis.policy import SecretStore

    store = SecretStore(Path(cfg.data_path) / "secrets.json")
    store.set("api", "supersecret123")
    db.set_redactor(store.scrub)
    db.audit("test.paste", target="run with supersecret123",
             detail={"note": "value supersecret123", "token": "short"})
    row = db.query_one("SELECT target, detail FROM audit ORDER BY id DESC LIMIT 1")
    assert "supersecret123" not in (row["target"] or "")
    assert "supersecret123" not in (row["detail"] or "")
    assert "***REDACTED***" in row["target"]
    assert "***REDACTED***" in row["detail"]
    assert db.verify_audit()["ok"] is True
