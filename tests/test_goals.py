"""Durable goals: planning validation, step execution, retry, cancel, reconcile.

The planner model is faked; the executor is real (the task controller, policy and
verifier), so a goal step is gated exactly like any other tool call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _plan(*steps) -> str:
    return json.dumps({"steps": list(steps)})


async def _fake_chat(text: str):
    yield SimpleNamespace(kind="token", text=text)


def _bind_planner(kernel, monkeypatch, text: str):
    async def chat(messages, **kwargs):
        async for ev in _fake_chat(text):
            yield ev
    monkeypatch.setattr(kernel.router, "chat", chat)


async def test_plan_drops_unknown_tools_and_ends_with_an_answer(kernel, monkeypatch):
    _bind_planner(kernel, monkeypatch, _plan(
        {"kind": "tool", "tool": "time.now", "args": {}},
        {"kind": "tool", "tool": "not.a.real.tool", "args": {}},
        {"kind": "answer", "text": "Done."},
    ))
    steps = await kernel.services["goals"].plan("what time is it")
    assert [s["kind"] for s in steps] == ["tool", "answer"]
    assert steps[0]["tool"] == "time.now"
    assert steps[-1]["text"] == "Done."


async def test_plan_falls_back_to_one_request_and_closes(kernel, monkeypatch):
    _bind_planner(kernel, monkeypatch, "I cannot produce JSON today")
    steps = await kernel.services["goals"].plan("do the thing")
    assert steps[0]["kind"] == "request" and steps[0]["prompt"] == "do the thing"
    assert steps[-1]["kind"] == "answer"


async def test_create_persists_a_plan(kernel):
    svc = kernel.services["goals"]
    goal = svc.create("check the time", steps=[
        {"kind": "tool", "tool": "time.now", "args": {}},
        {"kind": "answer", "text": "Done."},
    ])
    assert goal["status"] == "planned" and goal["steps_total"] == 2
    assert len(goal["steps"]) == 2
    assert svc.list_goals()[0]["id"] == goal["id"]


async def test_advance_runs_steps_in_order_and_finishes(kernel):
    svc = kernel.services["goals"]
    goal = svc.create("check the time", steps=[
        {"kind": "tool", "tool": "time.now", "args": {}},
        {"kind": "answer", "text": "All done."},
    ])
    first = await svc.advance(goal["id"])
    assert first["steps"][0]["status"] == "succeeded"
    assert first["steps"][1]["status"] == "pending"

    second = await svc.advance(goal["id"])
    assert second["status"] == "succeeded"
    assert second["result"] == "All done."
    assert second["steps_done"] == 2


async def test_a_placeholder_answer_is_synthesised_from_step_results(kernel, monkeypatch):
    async def chat(messages, **kwargs):
        yield SimpleNamespace(kind="token", text="The time is 12:00.")
    monkeypatch.setattr(kernel.router, "chat", chat)
    svc = kernel.services["goals"]
    goal = svc.create("time", steps=[
        {"kind": "tool", "tool": "time.now", "args": {}},
        {"kind": "answer", "text": "The time is [time]."},
    ])
    await svc.advance(goal["id"])
    final = await svc.advance(goal["id"])
    assert final["status"] == "succeeded"
    assert final["result"] == "The time is 12:00."


async def test_a_failing_step_retries_then_marks_the_goal_failed(kernel, tmp_path):
    kernel.config.goals.max_attempts = 2
    svc = kernel.services["goals"]
    goal = svc.create("read a missing file", steps=[
        {"kind": "tool", "tool": "files.read",
         "args": {"path": str(tmp_path / "does-not-exist.txt")}},
        {"kind": "answer", "text": "Done."},
    ])
    first = await svc.advance(goal["id"])
    step = first["steps"][0]
    assert step["status"] == "pending" and step["attempts"] == 1
    second = await svc.advance(goal["id"])
    assert second["steps"][0]["status"] == "failed"
    assert second["status"] == "failed" and second["error"]


async def test_cancel_stops_pending_steps(kernel):
    svc = kernel.services["goals"]
    goal = svc.create("x", steps=[
        {"kind": "tool", "tool": "time.now", "args": {}},
        {"kind": "answer", "text": "Done."},
    ])
    assert svc.cancel(goal["id"]) is True
    status = svc.status(goal["id"])
    assert status["status"] == "cancelled"
    assert all(s["status"] == "cancelled" for s in status["steps"])
    assert svc.cancel(goal["id"]) is False


async def test_run_pending_skips_a_goal_with_a_running_step(kernel):
    db = kernel.db
    db.execute("INSERT INTO goals(id,created_at,updated_at,request,status,steps_total) "
               "VALUES(?,?,?,?,?,?)", ("goal_x", "t", "t", "x", "running", 1))
    db.execute("INSERT INTO goal_steps(id,goal_id,ordinal,kind,status,created_at,updated_at) "
               "VALUES(?,?,?,?,?,?,?)", ("step_x", "goal_x", 0, "tool", "running", "t", "t"))
    assert await kernel.services["goals"].run_pending() == []


async def test_reconcile_marks_interrupted_steps(kernel):
    db = kernel.db
    db.execute("INSERT INTO goals(id,created_at,updated_at,request,status,steps_total) "
               "VALUES(?,?,?,?,?,?)", ("goal_y", "t", "t", "y", "running", 1))
    db.execute("INSERT INTO goal_steps(id,goal_id,ordinal,kind,status,created_at,updated_at) "
               "VALUES(?,?,?,?,?,?,?)", ("step_y", "goal_y", 0, "tool", "running", "t", "t"))
    assert kernel.services["goals"].reconcile() == 1
    step = db.query_one("SELECT status, error FROM goal_steps WHERE id='step_y'")
    goal = db.query_one("SELECT status FROM goals WHERE id='goal_y'")
    assert step["status"] == "outcome-unknown" and "not retried" in step["error"]
    assert goal["status"] == "outcome-unknown"


async def test_api_goal_lifecycle(kernel, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import create_app

    svc = kernel.services["goals"]

    async def fake_plan(request):
        return [{"kind": "answer", "text": "Planned."}]

    monkeypatch.setattr(svc, "plan", fake_plan)
    app = create_app(kernel)
    with fastapi_testclient.TestClient(app) as client:
        created = client.post("/api/goals", json={"request": "say hello"})
        assert created.status_code == 200
        goal_id = created.json()["id"]

        listed = client.get("/api/goals")
        assert listed.status_code == 200
        assert any(g["id"] == goal_id for g in listed.json()["goals"])

        shown = client.get(f"/api/goals/{goal_id}")
        assert shown.status_code == 200 and shown.json()["id"] == goal_id

        ran = client.post(f"/api/goals/{goal_id}/run")
        assert ran.status_code == 200
        assert ran.json()["status"] in ("succeeded", "running", "planned")

        assert client.get("/api/goals/nope").status_code == 404
        assert client.post("/api/goals", json={}).status_code == 400
