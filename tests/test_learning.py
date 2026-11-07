"""Learning subsystem: outcome-based self-adaptation."""

from __future__ import annotations

import pytest

from jarvis.learning import LearningService
from jarvis.memory import MemoryStore


@pytest.fixture
def learning(db):
    """A LearningService over a router-less MemoryStore (fast, deterministic)."""
    return LearningService(MemoryStore(db, router=None))


# ------------------------------------------------------------------ service

async def test_learn_relevant_and_list(learning):
    await learning.learn("Owner prefers British spelling", tags=["preference"])
    res = await learning.relevant("British spelling")
    assert any("British spelling" in i["text"] for i in res["items"])
    lessons = learning.list_lessons()
    assert any("British spelling" in l["text"] for l in lessons)
    assert lessons[0]["confidence"] == 0.9


async def test_learn_then_forget(learning):
    lesson_id = await learning.learn("temporary lesson")
    assert learning.list_lessons()
    assert learning.forget_lesson(lesson_id) is True
    assert learning.list_lessons() == []


async def test_record_turn_creates_failure_lesson(learning):
    calls = [{"tool": "system.info", "args": {}, "ok": False,
              "error": "no such tool"}]
    recorded = await learning.record_turn(request="get info", tool_calls=calls)
    assert recorded == 1
    lessons = learning.list_lessons()
    assert len(lessons) == 1
    assert "system.info" in lessons[0]["text"]
    assert lessons[0]["confidence"] == 0.5
    assert "failure" in lessons[0]["tags"]


async def test_repeated_failure_reinforces_not_duplicates(learning):
    calls = [{"tool": "apps.launch", "args": {"app": "steam"}, "ok": False,
              "error": "not found"}]
    first = await learning.record_turn(request="open steam", tool_calls=calls)
    second = await learning.record_turn(request="open steam", tool_calls=calls)
    assert first == 1
    assert second == 0  # reinforced, not newly recorded
    lessons = learning.list_lessons()
    assert len(lessons) == 1
    assert lessons[0]["confidence"] == 0.7


async def test_record_turn_ignores_success(learning):
    calls = [{"tool": "system.info", "args": {}, "ok": True}]
    recorded = await learning.record_turn(request="info", tool_calls=calls)
    assert recorded == 0
    assert learning.list_lessons() == []


async def test_context_block_marks_lessons(learning):
    await learning.learn("Never run rm -rf without asking")
    res = await learning.relevant("rm rf", top_k=1)
    block = learning.context_block(res["items"])
    assert "LEARNED" in block
    assert "rm -rf" in block


# ---------------------------------------------------------------------- tools

def test_learning_tools_registered(kernel):
    from jarvis.tools import REGISTRY
    names = REGISTRY.names()
    assert "learning.learn" in names
    assert "learning.list" in names
    assert "learning.forget" in names


async def test_learn_tool_end_to_end(kernel):
    from jarvis.tools import REGISTRY
    from jarvis.tools.base import ToolContext
    ctx = ToolContext(config=kernel.config, db=kernel.db, bus=kernel.bus,
                      services=kernel.services, user="owner")
    r = await REGISTRY.call("learning.learn", ctx, text="prefer tabs over spaces")
    assert r.ok, r.error
    assert any("tabs" in l["text"] for l in kernel.learning.list_lessons())


async def test_lessons_tool_lists(kernel):
    from jarvis.tools import REGISTRY
    from jarvis.tools.base import ToolContext
    ctx = ToolContext(config=kernel.config, db=kernel.db, bus=kernel.bus,
                      services=kernel.services, user="owner")
    await REGISTRY.call("learning.learn", ctx, text="lesson for listing")
    r = await REGISTRY.call("learning.list", ctx)
    assert r.ok, r.error
    assert r.data["count"] >= 1
    assert any("lesson for listing" in l["text"] for l in r.data["lessons"])


async def test_learn_tool_fails_without_service(cfg, db):
    from jarvis.tools import REGISTRY
    from jarvis.tools.base import ToolContext
    ctx = ToolContext(config=cfg, db=db, bus=None, services={}, user="owner")
    r = await REGISTRY.call("learning.learn", ctx, text="anything")
    assert not r.ok and "unavailable" in r.error


def test_agent_is_wired_with_learning(kernel):
    assert kernel.learning is not None
    assert kernel.agent.learning is kernel.learning
    assert kernel.services.get("learning") is kernel.learning
