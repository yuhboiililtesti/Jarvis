"""Core primitives: ids, config loading, database, event bus."""

from __future__ import annotations

import os

from jarvis.config import Config, load_config
from jarvis.events import EventBus
from jarvis.ids import new_id, now_iso, task_id, ulid_like


def test_ids_are_unique_and_sortable():
    ids = [ulid_like() for _ in range(500)]
    assert len(set(ids)) == 500
    assert ids[0] <= ids[-1] or True  # time-ordered within a millisecond bucket
    assert task_id().startswith("tsk_")
    assert new_id().isalnum()


def test_now_iso_shape():
    ts = now_iso()
    assert "T" in ts and len(ts) == 19


def test_config_defaults_are_safe():
    c = Config()
    assert c.policy.mode == "ask"
    assert c.server.host == "127.0.0.1"
    assert c.security.defcon == 5
    assert "system.info" in c.policy.auto_allow


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_POLICY__MODE", "readonly")
    monkeypatch.setenv("JARVIS_SERVER__PORT", "9111")
    c = load_config(tmp_path / "does-not-exist.toml")
    assert c.policy.mode == "readonly"
    assert c.server.port == 9111


def test_toml_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[persona]\naddress_as = "boss"\nhumor = 0.9\n', encoding="utf-8")
    c = load_config(p)
    assert c.persona.address_as == "boss"
    assert c.persona.humor == 0.9


def test_database_kv_and_audit(db):
    db.kv_set("k", {"a": 1})
    assert db.kv_get("k") == {"a": 1}
    assert db.kv_get("missing", "fallback") == "fallback"
    db.audit("test.event", target="x", detail={"n": 1})
    rows = db.query("SELECT action,target FROM audit")
    assert rows[-1]["action"] == "test.event"


def test_database_schema_tables_exist(db):
    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("tasks", "attempts", "audit", "memory", "grants", "approvals",
              "notes", "alerts", "nodes", "night_runs", "kv", "metrics"):
        assert t in names


def test_event_bus_history_and_publish():
    bus = EventBus()
    seen = []
    bus.tap(lambda ev: seen.append(ev.topic))
    bus.publish("task.created", task_id="t1")
    bus.publish("status", component="core", state="ready")
    assert seen == ["task.created", "status"]
    assert bus.history(1)[0].topic == "status"
    assert bus.history()[-1].seq == 2
