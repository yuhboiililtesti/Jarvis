"""Task controller: durable journal, approval gate, verification, reconciliation.

These are the properties that make the safety claims real, so they are tested
directly rather than inferred from the API.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.ids import now_iso


@pytest.mark.asyncio
async def test_read_only_tool_runs_and_settles_verified(kernel, register_test_tools):
    t = kernel.controller.create("echo hi")
    result, evidence = await kernel.controller.execute_tool(t, "test.echo", {"text": "hi"})
    assert result.ok
    assert evidence["verified"] is True
    row = kernel.db.query_one("SELECT status,evidence FROM attempts WHERE task_id=?", (t.id,))
    assert row["status"] == "succeeded"


@pytest.mark.asyncio
async def test_side_effect_tool_has_real_readback(kernel, register_test_tools, tmp_path):
    target = tmp_path / "out.txt"
    t = kernel.controller.create("write a file")
    result, evidence = await kernel.controller.execute_tool(
        t, "test.write", {"path": str(target), "content": "hello"})
    assert result.ok and evidence["verified"] is True
    assert target.read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_failed_tool_is_recorded_failed_not_success(kernel, register_test_tools):
    t = kernel.controller.create("fail please")
    result, evidence = await kernel.controller.execute_tool(t, "test.fail", {})
    assert not result.ok
    row = kernel.db.query_one("SELECT status FROM attempts WHERE task_id=?", (t.id,))
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_unknown_tool_fails_loudly(kernel):
    t = kernel.controller.create("do something imaginary")
    result, _ = await kernel.controller.execute_tool(t, "no.such.tool", {})
    assert not result.ok and "unknown tool" in result.error


@pytest.mark.asyncio
async def test_attempt_row_exists_before_effect(kernel, register_test_tools, tmp_path):
    """The durable marker must be committed BEFORE the side effect."""
    target = tmp_path / "marker.txt"
    t = kernel.controller.create("write")
    # wrap the tool so we can inspect the DB mid-effect
    seen = {}
    tool = kernel.registry.get("test.write")
    original = tool.run

    async def spy(ctx, **kwargs):
        row = kernel.db.query_one("SELECT id,status FROM attempts WHERE id=?", (ctx.attempt_id,))
        seen["during"] = dict(row) if row else None
        return await original(ctx, **kwargs)

    tool.run = spy
    try:
        await kernel.controller.execute_tool(t, "test.write",
                                             {"path": str(target), "content": "x"})
    finally:
        tool.run = original
    assert seen["during"] is not None, "no attempt row existed during the effect"
    assert seen["during"]["status"] == "running"


@pytest.mark.asyncio
async def test_gated_tool_requests_approval_and_denial_blocks(kernel, register_test_tools, tmp_path):
    target = tmp_path / "gated.txt"
    t = kernel.controller.create("do a gated thing")

    async def run():
        return await kernel.controller.execute_tool(t, "test.gated", {"path": str(target)})

    task = asyncio.create_task(run())
    for _ in range(200):                       # wait for the approval row
        if kernel.controller.pending_approvals():
            break
        await asyncio.sleep(0.01)
    pending = kernel.controller.pending_approvals()
    assert pending, "no approval was requested for a gated tool"

    kernel.controller.resolve_approval(pending[0]["id"], "deny")
    result, evidence = await task
    assert not result.ok
    assert not target.exists(), "gated tool ran despite denial"


@pytest.mark.asyncio
async def test_gated_tool_runs_when_approved(kernel, register_test_tools, tmp_path):
    target = tmp_path / "allowed.txt"
    t = kernel.controller.create("do a gated thing")

    async def run():
        return await kernel.controller.execute_tool(t, "test.gated", {"path": str(target)})

    task = asyncio.create_task(run())
    for _ in range(200):
        if kernel.controller.pending_approvals():
            break
        await asyncio.sleep(0.01)
    kernel.controller.resolve_approval(kernel.controller.pending_approvals()[0]["id"], "allow")
    result, evidence = await task
    assert result.ok
    assert target.exists()
    assert evidence["verified"] is True


@pytest.mark.asyncio
async def test_approval_timeout_is_denied_not_allowed(kernel, register_test_tools, cfg, tmp_path):
    cfg.policy.approval_timeout_s = 0.2
    t = kernel.controller.create("gated")
    result, _ = await kernel.controller.execute_tool(t, "test.gated",
                                                     {"path": str(tmp_path / "x.txt")})
    assert not result.ok
    assert "not authorised" in result.error


def test_reconcile_marks_running_attempts_outcome_unknown(kernel):
    t = kernel.controller.create("interrupted")
    kernel.db.execute(
        "INSERT INTO attempts(id,task_id,tool,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        ("att_interrupted", t.id, "test.write", "running", now_iso(), now_iso()))
    out = kernel.controller.reconcile()
    assert out["reconciled"] >= 1
    row = kernel.db.query_one("SELECT status FROM attempts WHERE id='att_interrupted'")
    assert row["status"] == "outcome-unknown"


def test_cancel_sets_cancelled(kernel):
    t = kernel.controller.create("long job")
    assert kernel.controller.cancel(t.id)
    row = kernel.db.query_one("SELECT status FROM tasks WHERE id=?", (t.id,))
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_denied_by_policy_does_not_create_attempt(kernel, cfg, register_test_tools):
    cfg.policy.mode = "readonly"
    t = kernel.controller.create("write in readonly")
    before = kernel.db.query("SELECT id FROM attempts WHERE task_id=?", (t.id,))
    result, _ = await kernel.controller.execute_tool(t, "test.write", {"path": "x"})
    after = kernel.db.query("SELECT id FROM attempts WHERE task_id=?", (t.id,))
    assert not result.ok
    assert len(before) == len(after) == 0
