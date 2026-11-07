"""Self-diagnostics: honest checks and one bounded, audited repair.

The model provider is faked; the node/tunnel path is exercised with real database
rows and a stubbed sandbox run, so the cooldown is the real one.
"""

from __future__ import annotations

import pytest

from jarvis.sandbox.result import SandboxResult


class _Node:
    def __init__(self):
        self.pinged = 0

    async def list_nodes(self):
        return [{"id": "node_1", "name": "laptop",
                 "base_url": "http://127.0.0.1:2226", "status": "enrolled"}]

    async def ping(self, node_id):
        self.pinged += 1
        return {"ok": False, "error": "unreachable"}


async def test_check_reports_every_subsystem(kernel, monkeypatch):
    async def health():
        return {"ok": True, "provider": "ollama", "model": "qwen2.5:7b-instruct"}
    monkeypatch.setattr(kernel.router, "health", health)
    ops = kernel.services["ops"]
    result = await ops.check()
    names = {c["name"] for c in result["checks"]}
    assert {"llm", "runtime", "goals", "audit", "sandbox", "database", "disk"} <= names
    llm = next(c for c in result["checks"] if c["name"] == "llm")
    audit = next(c for c in result["checks"] if c["name"] == "audit")
    assert llm["ok"] is True and audit["ok"] is True
    assert isinstance(result["ok"], bool) and result["ts"]


async def test_a_broken_provider_is_reported_with_a_repair(kernel, monkeypatch):
    async def health():
        return {"ok": False, "provider": "ollama", "error": "connection refused"}
    monkeypatch.setattr(kernel.router, "health", health)
    result = await kernel.services["ops"].check()
    llm = next(c for c in result["checks"] if c["name"] == "llm")
    assert llm["ok"] is False
    assert "Ollama" in llm["repair"]


async def test_tunnel_repair_runs_once_per_cooldown(kernel, monkeypatch):
    nodes = _Node()
    kernel.services["nodes"] = nodes
    calls: list[list[str]] = []

    async def run_async(argv, **kwargs):
        calls.append(list(argv))
        return SandboxResult(ok=True, exit_code=0, tier="jobobject", contained=True)

    sandbox = kernel.services["sandbox"]
    monkeypatch.setattr(sandbox, "run_async", run_async)
    ops = kernel.services["ops"]
    ops.cfg.auto_repair_tunnel = True
    ops._last_repair = 0.0

    first = await ops.check()
    assert len(calls) == 1
    assert any(r["name"] == "node:laptop" for r in first["repaired"] or [])

    second = await ops.check()
    assert len(calls) == 1          # cooldown held
    assert second["repaired"] == []


async def test_api_ops_endpoints(kernel, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import create_app

    async def health():
        return {"ok": True, "provider": "ollama", "model": "qwen2.5:7b-instruct"}
    monkeypatch.setattr(kernel.router, "health", health)
    app = create_app(kernel)
    with fastapi_testclient.TestClient(app) as client:
        checked = client.post("/api/ops/check")
        assert checked.status_code == 200
        body = checked.json()
        assert "checks" in body and "ok" in body
        status = client.get("/api/ops")
        assert status.status_code == 200 and "checks" in status.json()


async def test_diagnose_tool_returns_the_snapshot(kernel):
    from jarvis.tools.base import ToolContext
    from jarvis.tools import REGISTRY, load_builtin_tools

    load_builtin_tools()
    ctx = ToolContext(config=kernel.config, db=kernel.db, bus=kernel.bus,
                      services={"ops": kernel.services["ops"]})
    result = await REGISTRY.get("system.diagnose").run(ctx)
    assert result.ok and "checks" in result.data
