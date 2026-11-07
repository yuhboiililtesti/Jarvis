"""Host power control: node ``power`` task and the core ``host.power`` tool."""

from __future__ import annotations

import pytest

from jarvis.nodes import agent as agent_mod
from jarvis.nodes import protocol
from jarvis.nodes.agent import NodeAgent
from jarvis.tools import REGISTRY
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import hosts  # noqa: F401  (registers host.power + voice.speak)


def test_power_is_a_supported_kind():
    assert protocol.KIND_POWER == "power"
    assert protocol.KIND_POWER in protocol.SUPPORTED_KINDS


@pytest.fixture
def agent():
    return NodeAgent("secret")


def test_power_rejects_unknown_action(agent):
    r = agent._task_power({"action": "explode"}, "t1")
    assert not r["ok"] and "action must be one of" in r["error"]


def test_power_requires_valid_delay(agent):
    r = agent._task_power({"action": "shutdown", "delay_s": 5}, "t1")
    assert not r["ok"] and "delay_s must be between" in r["error"]


def test_power_runs_allowed_action(agent, monkeypatch):
    calls = []

    def fake_run(action, delay, reason):
        calls.append((action, delay, reason))
        return {"ok": True, "argv": ["shutdown.exe", "/s", "/t", "60"],
                "returncode": 0}

    monkeypatch.setattr(agent_mod, "_run_power_action", fake_run)
    r = agent._task_power({"action": "shutdown", "delay_s": 60, "reason": "test"}, "t1")
    assert r["ok"] is True
    assert r["data"]["action"] == "shutdown"
    assert calls == [("shutdown", 60, "test")]


def test_power_reports_failure(agent, monkeypatch):
    monkeypatch.setattr(agent_mod, "_run_power_action",
                        lambda a, d, r: {"ok": False, "error": "no shutdown binary",
                                         "argv": ["x"]})
    r = agent._task_power({"action": "restart", "delay_s": 60}, "t1")
    assert not r["ok"] and "no shutdown binary" in r["error"]


# ---------------------------------------------------------------- core tool ---

class _FakeNodes:
    def __init__(self):
        self.calls = []

    async def send_task(self, node_id, kind, payload):
        self.calls.append((node_id, kind, payload))
        return {"ok": True, "data": {"action": payload.get("action"),
                                     "returncode": 0}}


@pytest.fixture
def ctx(cfg):
    cfg.nodes.known = {"pc": "http://x:2228", "laptop": "http://x:2229"}
    nodes = _FakeNodes()
    c = ToolContext(config=cfg, db=None, bus=None, services={"nodes": nodes})
    return c, nodes


async def test_host_power_dry_run(ctx):
    c, nodes = ctx
    r = await REGISTRY.call("host.power", c, host="pc", action="shutdown", dry_run=True)
    assert r.ok and r.data["dry_run"] is True
    assert nodes.calls == []  # nothing was sent


async def test_host_power_routes_to_node(ctx):
    c, nodes = ctx
    r = await REGISTRY.call("host.power", c, host="laptop", action="restart", delay_s=120)
    assert r.ok
    assert len(nodes.calls) == 1
    node_id, kind, payload = nodes.calls[0]
    assert (node_id, kind) == ("laptop", "power")
    assert payload["action"] == "restart" and payload["delay_s"] == 120


async def test_host_power_rejects_bad_action(ctx):
    c, nodes = ctx
    r = await REGISTRY.call("host.power", c, host="pc", action="destroy")
    assert not r.ok and "must be one of" in r.error
    assert nodes.calls == []
