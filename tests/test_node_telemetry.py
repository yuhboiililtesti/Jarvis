"""Per-node telemetry: the node ``metrics`` task and the core's poll loop."""

from __future__ import annotations

import pytest

from jarvis.monitoring.service import MonitoringService
from jarvis.nodes import agent as agent_mod
from jarvis.nodes import protocol
from jarvis.nodes.agent import NodeAgent


def test_metrics_is_a_supported_kind():
    assert protocol.KIND_METRICS == "metrics"
    assert protocol.KIND_METRICS in protocol.SUPPORTED_KINDS


def test_node_metrics_snapshot_has_shape():
    snap = agent_mod._node_metrics_snapshot()
    assert isinstance(snap, dict)
    assert "method" in snap
    assert "disk_percent" in snap  # shutil.disk_usage is cross-platform


@pytest.fixture
def agent():
    return NodeAgent("secret")


def test_metrics_task_returns_snapshot(agent, monkeypatch):
    monkeypatch.setattr(agent_mod, "_node_metrics_snapshot",
                        lambda: {"method": "psutil", "cpu_percent": 5.0,
                                 "disk_percent": 10.0})
    r = agent._task_metrics({}, "t1")
    assert r["ok"] is True
    assert r["data"]["cpu_percent"] == 5.0


# ---------------------------------------------------------------- core poll ---

class _FakeConn:
    def __init__(self):
        self.rows = []

    def executemany(self, _sql, rows):
        self.rows.extend(rows)


class _FakeDb:
    def __init__(self):
        self.conn = _FakeConn()

    def tx(self):
        class _Ctx:
            def __enter__(self_):
                return self.conn

            def __exit__(self_, *a):
                return False
        return _Ctx()


class _FakeNodes:
    def __init__(self, results, fail=frozenset()):
        self.results = results
        self.fail = fail
        self.calls = []

    async def send_task(self, node_id, kind, payload):
        self.calls.append((node_id, kind))
        if node_id in self.fail:
            raise RuntimeError("unreachable")
        return self.results.get(node_id, {"ok": False})


class _FakeJarvis:
    def __init__(self, db, nodes, config):
        self.db = db
        self.services = {"nodes": nodes}
        self.config = config
        self.bus = None


@pytest.fixture
def svc_factory(cfg):
    def build(nodes, *, poll_nodes=True):
        cfg.nodes.known = {"pc": "http://x", "laptop": "http://y"}
        cfg.monitoring.poll_nodes = poll_nodes
        db = _FakeDb()
        svc = MonitoringService(_FakeJarvis(db, nodes, cfg))
        return svc, db
    return build


async def test_sample_nodes_polls_and_writes(svc_factory):
    nodes = _FakeNodes({
        "pc": {"ok": True, "data": {"cpu_percent": 12.5, "mem_percent": 40.0}},
        "laptop": {"ok": True, "data": {"load_1": 0.3, "disk_percent": 20.0}},
    })
    svc, db = svc_factory(nodes)
    n = await svc._sample_nodes()
    assert n == 4
    names = [r[1] for r in db.conn.rows]
    assert "node.pc.cpu_percent" in names
    assert "node.pc.mem_percent" in names
    assert "node.laptop.load_1" in names
    assert "node.laptop.disk_percent" in names
    assert all(r[3] in ("pc", "laptop") for r in db.conn.rows)  # label = node id


async def test_sample_nodes_skips_down_node(svc_factory):
    nodes = _FakeNodes({"pc": {"ok": True, "data": {"cpu_percent": 1.0}}},
                       fail={"laptop"})
    svc, db = svc_factory(nodes)
    n = await svc._sample_nodes()
    assert n == 1
    names = [r[1] for r in db.conn.rows]
    assert names == ["node.pc.cpu_percent"]


async def test_sample_nodes_respects_disabled(svc_factory):
    nodes = _FakeNodes({"pc": {"ok": True, "data": {"cpu_percent": 1.0}}})
    svc, db = svc_factory(nodes, poll_nodes=False)
    assert await svc._sample_nodes() == 0
    assert db.conn.rows == []
