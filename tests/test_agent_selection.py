"""Tool selection and the repeat-call guard.

The registry has grown past 100 tools; a small model chooses badly when offered
all of them. These tests pin the relevance shortlist and prove that an exact
repeat is refused rather than spending the step budget on the same call.
"""

from __future__ import annotations

from types import SimpleNamespace

from jarvis.core.agent import Agent
from jarvis.events import EventBus
from jarvis.persona import Persona
from jarvis.tools import REGISTRY, load_builtin_tools

load_builtin_tools()


class CapturingRouter:
    def __init__(self) -> None:
        self.tools: list[dict] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.tools = list(tools or [])
        yield SimpleNamespace(kind="token", text="hello")


class RepeatingRouter:
    """Asks for the same tool twice, then answers."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls <= 2:
            yield SimpleNamespace(kind="tool_calls", tool_calls=[{
                "id": f"call_{self.calls}",
                "function": {"name": "time.now", "arguments": "{}"},
            }])
        else:
            yield SimpleNamespace(kind="token", text="Done.")


class RecordingController:
    def __init__(self) -> None:
        self._cancels: set[str] = set()
        self.executed: list[str] = []
        self.statuses: list[str] = []

    def create(self, request, **kwargs):
        return SimpleNamespace(id="tsk_1", request=request)

    def set_status(self, task, status, **kwargs):
        self.statuses.append(status)

    async def execute_tool(self, task, name, args, **kwargs):
        from jarvis.tools.base import ToolResult
        self.executed.append(name)
        return ToolResult(ok=True, data={"iso": "2026-10-20T00:00:00"}), {"verified": True}


def _agent(cfg, router, controller) -> Agent:
    return Agent(config=cfg, bus=EventBus(), router=router, registry=REGISTRY,
                 controller=controller, persona=Persona(cfg.persona),
                 memory=None, session_store=None)


async def test_shortlist_prefers_tools_named_in_the_request():
    names = REGISTRY.shortlist("what is Anton doing right now?")
    assert "anton.status" in names
    assert "anton.jobs" in names
    assert len(names) <= 24


async def test_shortlist_always_includes_the_core():
    names = REGISTRY.shortlist("hello")
    assert "time.now" in names
    assert "files.read" in names
    assert 0 < len(names) <= 24


async def test_agent_offers_only_the_shortlist(cfg):
    router = CapturingRouter()
    agent = _agent(cfg, router, RecordingController())
    async for _ in agent.run_turn("summarize my notes", session={"id": "s", "messages": []}):
        pass
    offered = [t["function"]["name"] for t in router.tools]
    assert 0 < len(offered) <= 24
    assert "time.now" in offered


async def test_an_exact_repeat_is_refused_not_executed(cfg):
    controller = RecordingController()
    agent = _agent(cfg, RepeatingRouter(), controller)
    events = [e async for e in agent.run_turn(
        "give me a status report", session={"id": "s", "messages": []})]
    assert controller.executed == ["time.now"]
    refused = [e for e in events
               if e.get("kind") == "tool_result" and not e.get("ok")
               and "Already called" in (e.get("summary") or "")]
    assert len(refused) == 1
    final = [e for e in events if e.get("kind") == "final"]
    assert final and "Done" in final[0]["text"]


class CapturingSystemRouter:
    def __init__(self) -> None:
        self.system = ""

    async def chat(self, messages, tools=None, **kwargs):
        self.system = messages[0].content
        yield SimpleNamespace(kind="token", text="Anton is idle, sir.")


class AntonController(RecordingController):
    async def execute_tool(self, task, name, args, **kwargs):
        from jarvis.tools.base import ToolResult
        self.executed.append(name)
        if name == "anton.status":
            return ToolResult(ok=True, data={"queue": {"queued": 0},
                                             "objects": {"count": 23}}), {"verified": True}
        return ToolResult(ok=True, data={"jobs": []}), {"verified": True}


async def test_anton_questions_are_grounded_in_a_real_read(cfg):
    router = CapturingSystemRouter()
    controller = AntonController()
    agent = _agent(cfg, router, controller)
    async for _ in agent.run_turn(
            "what is Anton doing?", session={"id": "s", "messages": []}):
        pass
    assert controller.executed == ["anton.status", "anton.jobs"]
    assert "Live Anton reading" in router.system
    assert '"queued": 0' in router.system
    assert '"count": 23' in router.system
