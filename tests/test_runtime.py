"""Persistent runtime: schedules fire once, events react once, sessions survive.

No model and no network: a fake agent stands in for the LLM. Tool reactions use
the **real** TaskController and policy, so the approval gate is exercised for
real rather than mocked away.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from jarvis.events import EventBus
from jarvis.llm import ChatMessage
from jarvis.persona import Persona
from jarvis.runtime import (
    EventEngine, RuntimeService, Scheduler, SessionStore,
    due_occurrence, match_condition, next_occurrence, parse_when,
)
from jarvis.tools import REGISTRY


# --------------------------------------------------------------------- fakes

class FakeAgent:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    async def run_turn(self, request, *, session, source="text", user="owner"):
        self.calls.append(request)
        yield {"kind": "task", "task_id": "tsk_fake"}
        if self.ok:
            yield {"kind": "final", "text": "done", "status": "succeeded"}
        else:
            yield {"kind": "error", "text": "boom"}
            yield {"kind": "final", "text": "", "status": "failed"}


class FakeRouter:
    async def chat(self, messages, tools=None, **kwargs):
        yield SimpleNamespace(kind="token", text="hello")


class FakeController:
    def __init__(self) -> None:
        self._cancels: set[str] = set()
        self.statuses: list[str] = []

    def create(self, request, **kwargs):
        return SimpleNamespace(id="tsk_1", request=request)

    def set_status(self, task, status, **kwargs):
        self.statuses.append(status)

    async def execute_tool(self, task, name, args, **kwargs):
        from jarvis.tools.base import ToolResult
        return ToolResult(ok=True, data={}), {"verified": True}


def _kernel(db, cfg, *, agent=None, controller=None, services=None):
    cache: dict[str, dict] = {}

    def session(sid=None):
        sid = sid or "default"
        return cache.setdefault(sid, {"id": sid, "messages": []})

    return SimpleNamespace(db=db, config=cfg, bus=EventBus(), agent=agent,
                           controller=controller, services=services or {}, session=session)


def _job(job_id: str, when: str, prompt: str = "ping", enabled: bool = True) -> dict:
    return {"id": job_id, "when": when, "prompt": prompt, "enabled": enabled,
            "created_at": "t"}


# ------------------------------------------------------------------ time parsing

def test_parse_when_accepts_daily_and_iso():
    assert parse_when("07:30") == ("daily", (7, 30, 0))
    assert parse_when("7:30:15") == ("daily", (7, 30, 15))
    assert parse_when("2026-10-19T07:30:00")[0] == "once"
    assert parse_when("not a time") == (None, None)
    assert parse_when("") == (None, None)


def test_daily_occurrence_is_not_due_before_its_time():
    now = datetime(2026, 10, 19, 7, 0, 0)
    assert due_occurrence("07:30", now) is None
    due, key = due_occurrence("06:30", now)
    assert due == datetime(2026, 10, 19, 6, 30, 0)
    assert key == "2026-10-19T06:30:00"


def test_a_job_just_before_midnight_is_not_lost_after_midnight():
    now = datetime(2026, 10, 20, 0, 0, 30)
    assert due_occurrence("23:59", now) is None          # no lookback: skipped
    occ = due_occurrence("23:59", now, lookback_s=3600)
    assert occ is not None and occ[1] == "2026-10-19T23:59:00"


def test_next_occurrence_rolls_to_tomorrow():
    now = datetime(2026, 10, 19, 8, 0, 0)
    assert next_occurrence("07:30", now) == datetime(2026, 10, 20, 7, 30, 0)
    assert next_occurrence("09:00", now) == datetime(2026, 10, 19, 9, 0, 0)


# -------------------------------------------------------------------- scheduler

async def test_one_shot_fires_once_and_dedupes(db, cfg):
    cfg.runtime.catch_up_s = 10 ** 9
    db.kv_set("scheduled_jobs", [_job("j1", "2026-10-19T07:00:00")])
    agent = FakeAgent()
    sched = Scheduler(_kernel(db, cfg, agent=agent))

    claims = await sched.fire_due(now=datetime(2026, 10, 19, 7, 0, 1))
    assert len(claims) == 1 and claims[0]["missed"] is False
    await sched.wait_idle()

    assert sched.status()["run_counts"] == {"succeeded": 1}
    assert len(agent.calls) == 1
    assert await sched.fire_due(now=datetime(2026, 10, 19, 7, 0, 2)) == []


async def test_daily_job_dedupes_per_day_then_fires_the_next(db, cfg):
    cfg.runtime.catch_up_s = 120
    db.kv_set("scheduled_jobs", [_job("j1", "06:30")])
    sched = Scheduler(_kernel(db, cfg, agent=FakeAgent()))

    first = await sched.fire_due(now=datetime(2026, 10, 19, 6, 30, 30))
    await sched.wait_idle()
    assert len(first) == 1
    assert await sched.fire_due(now=datetime(2026, 10, 19, 6, 31, 0)) == []

    second = await sched.fire_due(now=datetime(2026, 10, 20, 6, 30, 30))
    assert len(second) == 1 and second[0]["due_key"].startswith("2026-10-20")


async def test_late_beyond_catch_up_is_recorded_missed_not_run(db, cfg):
    cfg.runtime.catch_up_s = 60
    db.kv_set("scheduled_jobs", [_job("j1", "2026-10-19T06:00:00")])
    agent = FakeAgent()
    sched = Scheduler(_kernel(db, cfg, agent=agent))

    claims = await sched.fire_due(now=datetime(2026, 10, 19, 8, 0, 0))
    assert len(claims) == 1 and claims[0]["missed"] is True
    await sched.wait_idle()

    assert sched.status()["run_counts"] == {"missed": 1}
    assert agent.calls == []          # nothing ran


async def test_disabled_job_and_invalid_time_are_ignored(db, cfg):
    db.kv_set("scheduled_jobs", [_job("off", "06:00", enabled=False),
                                 _job("bad", "nonsense")])
    sched = Scheduler(_kernel(db, cfg, agent=FakeAgent()))
    assert await sched.fire_due(now=datetime(2026, 10, 19, 8, 0, 0)) == []
    assert sched.status()["run_counts"] == {}


async def test_a_failed_turn_is_recorded_as_failed(db, cfg):
    cfg.runtime.catch_up_s = 10 ** 9
    db.kv_set("scheduled_jobs", [_job("j1", "2026-10-19T07:00:00")])
    sched = Scheduler(_kernel(db, cfg, agent=FakeAgent(ok=False)))
    await sched.fire_due(now=datetime(2026, 10, 19, 7, 0, 1))
    await sched.wait_idle()
    runs = sched.status()["run_counts"]
    assert runs == {"failed": 1}
    row = db.query_one("SELECT status, error FROM scheduler_runs")
    assert "turn" in (row["error"] or "") or "failed" in (row["error"] or "")


def test_reconcile_marks_interrupted_runs_and_never_replays(db, cfg):
    db.execute("INSERT INTO scheduler_runs(id,job_id,due_key,status) VALUES(?,?,?,?)",
               ("r1", "j1", "2026-10-19T07:00:00", "running"))
    sched = Scheduler(_kernel(db, cfg, agent=FakeAgent()))
    assert sched.reconcile() == 1
    row = db.query_one("SELECT status, error FROM scheduler_runs WHERE id='r1'")
    assert row["status"] == "outcome-unknown"
    assert "not retried" in row["error"]
    assert sched.reconcile() == 0


# ----------------------------------------------------------------- event rules

def test_condition_operators_and_fail_closed():
    data = {"cpu": 95, "name": "plex", "tags": ["a", "b"], "nested": {"x": 1}}
    assert match_condition({"field": "cpu", "op": ">=", "value": 90}, data)
    assert not match_condition({"field": "cpu", "op": "<", "value": 90}, data)
    assert match_condition({"field": "nested.x", "op": "==", "value": 1}, data)
    assert match_condition({"field": "tags", "op": "contains", "value": "a"}, data)
    assert match_condition({"field": "name", "op": "startswith", "value": "pl"}, data)
    assert match_condition({"field": "missing", "op": "exists"}, data) is False
    assert match_condition({"field": "name", "op": "wat", "value": 1}, data) is False
    assert match_condition(None, data) is True


async def test_ingest_queues_only_matching_rules(db, cfg):
    engine = EventEngine(_kernel(db, cfg))
    engine.add_rule("disk", "disk.*", {"kind": "request", "prompt": "look"})
    result = engine.ingest("disk.smart_warning", {"device": "sda"})
    assert result["matched"] == 1
    fires = engine.pending()
    assert len(fires) == 1 and fires[0]["name"] == "disk"
    assert engine.ingest("cpu.high", {})["matched"] == 0


async def test_the_same_event_never_fires_twice(kernel):
    engine = EventEngine(kernel)
    engine.add_rule("now", "tick.*", {"kind": "tool", "tool": "time.now", "args": {}})
    engine.ingest("tick.1", {})
    fire = engine.pending()[0]
    first = await engine.execute_fire(fire)
    assert first["ok"] is True
    assert engine.pending() == []
    again = await engine.execute_fire(fire)
    assert again.get("skipped") is True


async def test_read_only_reaction_is_verified(kernel):
    engine = EventEngine(kernel)
    engine.add_rule("now", "tick.*", {"kind": "tool", "tool": "time.now", "args": {}})
    engine.ingest("tick.1", {})
    out = await engine.execute_fire(engine.pending()[0])
    assert out["ok"] is True and out["verified"] is True


async def test_a_mutating_reaction_still_needs_approval(kernel, register_test_tools, tmp_path):
    kernel.config.policy.approval_timeout_s = 0.1
    engine = EventEngine(kernel)
    target = tmp_path / "gated.txt"
    engine.add_rule("gated", "manual.*",
                    {"kind": "tool", "tool": "test.gated", "args": {"path": str(target)}})
    engine.ingest("manual.go", {})
    out = await engine.execute_fire(engine.pending()[0])

    assert out["ok"] is False
    assert not target.exists(), "an event must not bypass the approval gate"
    row = kernel.db.query_one(
        "SELECT decision FROM approvals ORDER BY created_at DESC LIMIT 1")
    assert row is not None and row["decision"] == "timeout"
    fire = kernel.db.query_one("SELECT status FROM event_fires")
    assert fire["status"] == "failed"


def test_rule_validation_rejects_bad_input(db, cfg):
    engine = EventEngine(_kernel(db, cfg))
    with pytest.raises(ValueError):
        engine.add_rule("x", "t", {"kind": "tool"})            # no tool
    with pytest.raises(ValueError):
        engine.add_rule("x", "t", {"kind": "nonsense"})
    with pytest.raises(ValueError):
        engine.add_rule("", "t", {"kind": "request", "prompt": "p"})
    with pytest.raises(ValueError):
        engine.add_rule("x", "t", {"kind": "request", "prompt": "p"},
                        condition={"op": "wat"})
    engine.add_rule("x", "t", {"kind": "request", "prompt": "p"})
    with pytest.raises(ValueError):
        engine.add_rule("x", "t", {"kind": "request", "prompt": "p"})
    assert engine.remove_rule("x") is True
    assert engine.remove_rule("x") is False


def test_reconcile_marks_an_interrupted_reaction(db, cfg):
    engine = EventEngine(_kernel(db, cfg))
    engine.add_rule("r", "t", {"kind": "request", "prompt": "p"})
    engine.ingest("t", {})
    fire = engine.pending()[0]
    db.execute("UPDATE event_fires SET status='running' WHERE id=?", (fire["id"],))
    assert engine.reconcile() == 1
    row = db.query_one("SELECT status, error FROM event_fires WHERE id=?", (fire["id"],))
    assert row["status"] == "outcome-unknown" and "not retried" in row["error"]


# --------------------------------------------------------------------- sessions

def test_session_round_trip_trims_and_updates_the_cache(db):
    store = SessionStore(db, max_messages=5)
    session = {"id": "s", "messages": [ChatMessage("user", f"m{i}") for i in range(10)]}
    store.save(session)
    got = store.get("s")
    assert len(got["messages"]) == 5
    assert got["messages"][0].content == "m5"
    assert len(session["messages"]) == 5          # the cache matches what was stored


def test_malformed_session_rows_degrade_to_empty(db):
    db.execute("INSERT INTO sessions(id,updated_at,messages) VALUES(?,?,?)",
               ("bad", "t", "{not json"))
    assert SessionStore(db).get("bad")["messages"] == []
    db.execute("UPDATE sessions SET messages=? WHERE id=?",
               (json.dumps([1, 2, {"role": "user", "content": "kept"}]), "bad"))
    messages = SessionStore(db).get("bad")["messages"]
    assert [m.role for m in messages] == ["user"]
    assert messages[0].content == "kept"


def test_session_list_and_delete(db):
    store = SessionStore(db)
    store.save({"id": "a", "messages": [ChatMessage("user", "x")]})
    store.save({"id": "b", "messages": [ChatMessage("user", "y")]})
    assert store.count() == 2
    listed = {row["id"] for row in store.list()}
    assert listed == {"a", "b"}
    assert store.delete("a") is True
    assert store.count() == 1
    assert store.delete("missing") is False


async def test_agent_persists_its_session(db, cfg):
    from jarvis.core.agent import Agent
    store = SessionStore(db)
    agent = Agent(config=cfg, bus=EventBus(), router=FakeRouter(), registry=REGISTRY,
                  controller=FakeController(), persona=Persona(cfg.persona),
                  memory=None, session_store=store)
    session = {"id": "cli", "messages": []}
    async for _ in agent.run_turn("hi", session=session):
        pass
    stored = store.get("cli")["messages"]
    assert stored[-2].content == "hi"
    assert stored[-1].content == "hello"
    assert stored[-1].role == "assistant"


async def test_kernel_session_survives_a_rebuild(cfg):
    from jarvis.core import build
    first = build(cfg)
    try:
        session = first.session("persist")
        session["messages"].append(ChatMessage("user", "kept across restart"))
        first.services["sessions"].save(session)
    finally:
        first.db.close()

    second = build(cfg)
    try:
        restored = second.session("persist")
        assert [m.content for m in restored["messages"]] == ["kept across restart"]
    finally:
        second.db.close()


# ----------------------------------------------------------------- runtime loop

def test_lease_has_exactly_one_owner(kernel):
    first = RuntimeService(kernel)
    second = RuntimeService(kernel)
    assert first._acquire_lease() is True
    assert second._acquire_lease() is False
    assert first.status()["owner"] is True
    assert second.status()["owner"] is False
    first._release_lease()
    assert second._acquire_lease() is True


def test_a_stale_lease_is_reported_as_expired_not_as_a_live_standby(kernel):
    runtime = RuntimeService(kernel)
    kernel.db.kv_set("runtime_lease", {
        "owner": "dead-host:1:abcdef", "expires_at": time.time() - 60})
    status = runtime.status()
    assert status["lease_valid"] is False
    assert status["lease_expired"] is True
    assert "expired" in runtime.health()["reason"]


async def test_tick_records_a_heartbeat(kernel, monkeypatch):
    runtime = RuntimeService(kernel)

    async def no_jobs(**kwargs):
        return []

    monkeypatch.setattr(runtime.scheduler, "fire_due", no_jobs)
    monkeypatch.setattr(runtime.events, "pending", lambda limit=10: [])

    result = await runtime.tick()
    assert result == {"enabled": True, "owner": True, "jobs": 0, "missed": 0,
                      "events": 0, "goals": 0}
    heartbeat = kernel.db.kv_get("runtime_heartbeat")
    assert heartbeat["tick"] == 1 and heartbeat["owner"] == runtime._owner


async def test_loop_survives_a_failing_tick(kernel, monkeypatch):
    kernel.config.runtime.tick_s = 0.05
    runtime = RuntimeService(kernel)

    async def boom(**kwargs):
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(runtime.scheduler, "fire_due", boom)
    await runtime.start()
    try:
        for _ in range(40):
            if runtime._last_error:
                break
            await asyncio.sleep(0.05)
        assert runtime._loop_task is not None and not runtime._loop_task.done()
        assert "scheduler exploded" in (runtime._last_error or "")
    finally:
        await runtime.stop()


async def test_stop_releases_the_lease(kernel):
    runtime = RuntimeService(kernel)
    await runtime.start()
    assert kernel.db.kv_get("runtime_lease")["owner"] == runtime._owner
    await runtime.stop()
    assert kernel.db.kv_get("runtime_lease")["expires_at"] == 0


# -------------------------------------------------------------------------- API

def test_api_runtime_surfaces(kernel):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import create_app

    app = create_app(kernel)
    with fastapi_testclient.TestClient(app) as client:
        status = client.get("/api/runtime")
        assert status.status_code == 200
        body = status.json()
        assert "scheduler" in body and "events" in body

        rules = client.get("/api/runtime/rules")
        assert rules.status_code == 200 and "rules" in rules.json()

        emitted = client.post("/api/events", json={"topic": "test.smoke", "data": {"a": 1}})
        assert emitted.status_code == 200
        assert emitted.json()["event_id"] >= 1

        assert client.post("/api/events", json={}).status_code == 400
        assert client.post("/api/events", json={"topic": "x", "data": "nope"}).status_code == 400

        # an idempotency key makes a retried delivery a no-op
        first = client.post("/api/events", json={"topic": "test.retry", "event_id": "evt-1"})
        second = client.post("/api/events", json={"topic": "test.retry", "event_id": "evt-1"})
        assert first.status_code == second.status_code == 200
        assert second.json()["duplicate"] is True
        assert second.json()["event_id"] == first.json()["event_id"]


# ----------------------------------------------------- review-driven hardening

async def test_a_due_job_near_midnight_still_fires(db, cfg):
    cfg.runtime.catch_up_s = 3600
    db.kv_set("scheduled_jobs", [_job("j1", "23:59")])
    sched = Scheduler(_kernel(db, cfg, agent=FakeAgent()))
    claims = await sched.fire_due(now=datetime(2026, 10, 20, 0, 0, 30))
    assert len(claims) == 1 and claims[0]["missed"] is False
    await sched.wait_idle()


async def test_tick_bounds_reactions_in_flight(kernel, monkeypatch):
    kernel.config.runtime.max_reaction_tasks = 2
    kernel.config.runtime.max_reactions_per_tick = 10
    runtime = RuntimeService(kernel)
    runtime._sem = asyncio.Semaphore(10)
    gate = asyncio.Event()

    async def slow(fire):
        await gate.wait()

    monkeypatch.setattr(runtime, "_guarded_fire", slow)
    monkeypatch.setattr(runtime.events, "pending",
                        lambda limit=10: [{"id": f"f{i}", "rule_id": "r"}
                                          for i in range(limit)])
    await runtime.tick(jobs=False, events=True)
    first = len(runtime._fires)
    assert 0 < first <= 2
    await runtime.tick(jobs=False, events=True)
    assert len(runtime._fires) == first     # capacity exhausted; no new tasks
    gate.set()
    await asyncio.gather(*runtime._fires, return_exceptions=True)


async def test_scheduler_caps_in_flight_and_never_overlaps_a_job(db, cfg):
    cfg.runtime.catch_up_s = 10 ** 9
    cfg.runtime.max_jobs_in_flight = 1
    db.kv_set("scheduled_jobs", [_job("j1", "2026-10-19T07:00:00"),
                                 _job("j2", "2026-10-19T07:00:00")])
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowAgent(FakeAgent):
        async def run_turn(self, request, *, session, source="text", user="owner"):
            started.set()
            await release.wait()
            yield {"kind": "task", "task_id": "t"}
            yield {"kind": "final", "text": "done", "status": "succeeded"}

    sched = Scheduler(_kernel(db, cfg, agent=SlowAgent()))
    first = await sched.fire_due(now=datetime(2026, 10, 19, 7, 0, 1))
    assert len(first) == 1
    await started.wait()
    assert await sched.fire_due(now=datetime(2026, 10, 19, 7, 0, 2)) == []
    release.set()
    await sched.wait_idle()


async def test_a_one_shot_tick_does_not_claim_jobs(db, cfg):
    cfg.runtime.catch_up_s = 10 ** 9
    db.kv_set("scheduled_jobs", [_job("j1", "2026-10-19T07:00:00")])
    runtime = RuntimeService(_kernel(db, cfg, agent=FakeAgent()))
    result = await runtime.tick(now=datetime(2026, 10, 19, 7, 0, 1), jobs=False)
    assert result["jobs"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM scheduler_runs")["n"] == 0


async def test_a_standby_core_does_not_reconcile_the_owners_claims(kernel):
    kernel.config.runtime.tick_s = 60
    owner = RuntimeService(kernel)
    assert owner._acquire_lease() is True
    kernel.db.execute("INSERT INTO scheduler_runs(id,job_id,due_key,status) "
                      "VALUES(?,?,?,?)", ("r1", "j1", "k", "running"))
    standby = RuntimeService(kernel)
    await standby.start()
    try:
        row = kernel.db.query_one("SELECT status FROM scheduler_runs WHERE id='r1'")
        assert row["status"] == "running"          # untouched by the standby
        assert standby.status()["owner"] is False
    finally:
        await standby.stop()
        owner._release_lease()


def test_a_corrupt_condition_skips_the_rule_instead_of_widening_it(db, cfg):
    engine = EventEngine(_kernel(db, cfg))
    engine.add_rule("disk", "disk.*", {"kind": "request", "prompt": "look"},
                    condition={"field": "smart", "op": "==", "value": "warning"})
    db.execute("UPDATE event_rules SET condition='{not json'")
    assert engine.ingest("disk.smart_warning", {"smart": "ok"})["matched"] == 0


def test_conditions_can_inspect_the_event_source(db, cfg):
    engine = EventEngine(_kernel(db, cfg))
    engine.add_rule("api-only", "x.*", {"kind": "request", "prompt": "p"},
                    condition={"field": "source", "op": "==", "value": "api"})
    assert engine.ingest("x.1", {}, source="api")["matched"] == 1
    assert engine.ingest("x.2", {}, source="bus")["matched"] == 0


def test_removing_a_rule_cancels_queued_fires(db, cfg):
    engine = EventEngine(_kernel(db, cfg))
    engine.add_rule("r", "t", {"kind": "request", "prompt": "p"})
    engine.ingest("t", {})
    assert engine.pending()
    assert engine.remove_rule("r") is True
    assert engine.pending() == []
    assert db.query_one("SELECT COUNT(*) AS n FROM event_fires")["n"] == 0


async def test_a_fire_for_a_disabled_rule_does_not_run(kernel):
    engine = EventEngine(kernel)
    engine.add_rule("r", "t", {"kind": "tool", "tool": "time.now", "args": {}})
    engine.ingest("t", {})
    fire = engine.pending()[0]
    kernel.db.execute("UPDATE event_rules SET enabled=0")
    out = await engine.execute_fire(fire)
    assert out.get("skipped") is True


async def test_notify_writes_an_alert_without_needing_approval(kernel):
    engine = EventEngine(kernel)
    engine.add_rule("toast", "note.*",
                    {"kind": "notify", "title": "Disk warning", "message": "sda"})
    engine.ingest("note.1", {})
    out = await engine.execute_fire(engine.pending()[0])
    assert out["ok"] is True and out["alert_id"]
    row = kernel.db.query_one(
        "SELECT title, source FROM alerts ORDER BY created_at DESC LIMIT 1")
    assert row["title"] == "Disk warning" and row["source"].startswith("runtime:")
    assert kernel.db.query_one(
        "SELECT COUNT(*) AS n FROM approvals WHERE tool='notify.toast'")["n"] == 0


def test_idempotent_ingest_creates_one_event_and_one_fire(db, cfg):
    engine = EventEngine(_kernel(db, cfg))
    engine.add_rule("r", "t", {"kind": "request", "prompt": "p"})
    first = engine.ingest("t", {}, dedupe_key="evt-1")
    second = engine.ingest("t", {}, dedupe_key="evt-1")
    assert second["duplicate"] is True and second["event_id"] == first["event_id"]
    assert db.query_one("SELECT COUNT(*) AS n FROM runtime_events")["n"] == 1
    assert len(engine.pending()) == 1


def test_session_trim_keeps_system_and_whole_exchanges(db):
    store = SessionStore(db, max_messages=3)
    store.save({"id": "s", "messages": [
        ChatMessage("system", "sys"),
        ChatMessage("assistant", "", tool_calls=[{"id": "c1"}]),
        ChatMessage("tool", "result", name="t", tool_call_id="c1"),
        ChatMessage("user", "u2"),
    ]})
    assert [m.role for m in store.get("s")["messages"]] == ["system", "user"]


def test_session_cache_is_bounded(kernel):
    for i in range(205):
        kernel.session(f"cache-{i}")
    assert len(kernel.sessions) <= 200


async def test_status_reports_no_longer_owner_after_stop(kernel):
    runtime = RuntimeService(kernel)
    await runtime.start()
    await runtime.stop()
    status = runtime.status()
    assert status["owner"] is False and status["running"] is False
