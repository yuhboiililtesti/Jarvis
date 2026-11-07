"""The Operations surface: the HUD assets parse and read the real runtime API.

There is no browser or JS test runner here, so this checks what can be checked
honestly: every module parses (Node), the HUD contains the panel and controls it
claims, the static assets are served, and the API returns exactly the shape the
panel consumes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "jarvis" / "ui"
JS_FILES = ("app.js", "reactor.js", "transport.js", "speech.js", "voice.js",
            "pcm-worklet.js")


def test_every_hud_module_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    for name in JS_FILES:
        source = (UI / name).read_bytes()
        target = tmp_path / (name + ".mjs")      # .mjs => parsed as a module
        target.write_bytes(source)
        proc = subprocess.run([node, "--check", str(target)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f"{name} does not parse:\n{proc.stderr}"


def test_hud_has_the_operations_surface_and_controls():
    html = (UI / "index.html").read_text(encoding="utf-8")
    for token in ('data-tab="runtime"', 'id="panel-runtime"', 'id="tab-runtime"',
                  'id="runtime-status"', 'id="runtime-jobs"', 'id="runtime-runs"',
                  'id="runtime-rules"', 'id="runtime-events"', 'id="runtime-tick"',
                  'id="runtime-goals"', 'id="runtime-ops"', 'id="ops-check"',
                  'id="runtime-emit"', 'id="runtime-topic"'):
        assert token in html, token
    # the live subsystem strip is real markup, not generated text
    assert 'aria-label="Subsystem status"' in html
    for chip in ('id="sys-core"', 'id="sys-runtime"', 'id="sys-sandbox"', 'id="sys-audit"'):
        assert chip in html, chip


def test_hud_wires_the_endpoints_it_uses():
    js = (UI / "app.js").read_text(encoding="utf-8")
    for token in ("/api/runtime", "/api/runtime/tick", "/api/events", "/api/goals",
                  "/api/ops", "/api/sandbox", "/api/audit/verify", "loadRuntime",
                  "refreshSystems", "renderRuntime", "renderGoals", "renderOps"):
        assert token in js, token
    # the panel must be reachable through the shared loader table
    assert "runtime: loadRuntime" in js


def test_launcher_waits_for_the_core_and_has_a_stop():
    root = UI.parent.parent
    run = (root / "scripts" / "run-and-open.cmd").read_text(encoding="utf-8")
    assert "launch-hud.ps1" in run
    assert "-m jarvis serve" not in run      # the foreground console was the bug
    launch = (root / "scripts" / "launch-hud.ps1").read_text(encoding="utf-8")
    assert "start-core.ps1" in launch
    assert "Start-Process $Url" in launch    # opens only after a successful start
    assert (root / "scripts" / "stop-jarvis.cmd").is_file()


def test_telemetry_is_linked_to_the_real_metric_names_and_live_frames():
    js = (UI / "app.js").read_text(encoding="utf-8")
    # the monitoring service stores suffixed names; the gauges must ask for them
    for name in ("cpu.percent", "mem.percent", "disk.percent", "gpu.0.temperature_c"):
        assert name in js, name
    assert 'case "metrics"' in js and "ingestMetricsFrame" in js
    assert "data.series" in js          # /api/metrics returns {series:[...]}


def test_nodes_panel_pulls_real_node_telemetry():
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "hydrateNodeTelemetry" in js
    assert "/sysinfo" in js
    assert "used_pct" in js and "uptime_s" in js


def test_api_node_sysinfo_proxies_the_node(kernel, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import create_app

    nodes = kernel.services.get("nodes")
    assert nodes is not None

    async def fake_send(node_id, kind, payload):
        assert node_id == "node_1" and kind == "sysinfo"
        return {"ok": True, "data": {"hostname": "laptop", "cpu_count": 2,
                                     "memory": {"used_pct": 18.2, "used_mb": 683,
                                                "total_mb": 3755},
                                     "uptime_s": 3600, "load": [0.1, 0.2, 0.3]}}

    monkeypatch.setattr(nodes, "send_task", fake_send)
    app = create_app(kernel)
    with fastapi_testclient.TestClient(app) as client:
        r = client.get("/api/nodes/node_1/sysinfo")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["data"]["cpu_count"] == 2


def test_hud_recovers_approvals_and_dedupes_frames_and_scales():
    js = (UI / "app.js").read_text(encoding="utf-8")
    # pending approvals are re-fetched on load and on every reconnect
    assert "/api/approvals" in js and "syncApprovals" in js
    # replayed bus frames must not duplicate the feed
    assert "lastSeq" in js and "frame.seq" in js
    # heartbeats/telemetry stay out of the activity feed
    assert 'frame.topic === "runtime.tick"' in js and 'frame.topic === "metrics"' in js
    # the UI scale is real, persisted and adjustable
    assert "applyUiScale" in js and "jarvis.ui_scale" in js

    html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'id="set-ui-scale"' in html
    css = (UI / "styles.css").read_text(encoding="utf-8")
    assert "--ui-scale" in css
    # four grid rows: topbar, subsystem strip, main, conversation
    assert "grid-template-rows: var(--topbar-h) auto minmax(0, 1fr) auto" in css
    # scrollbars are visible and stable wherever content overflows
    assert "scrollbar-gutter: stable" in css
    assert "::-webkit-scrollbar-thumb" in css
    # a model failure names its cause instead of a generic FAULT
    assert "model offline" in js


def test_api_returns_the_shape_the_panel_consumes_and_serves_the_hud(kernel):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import create_app

    app = create_app(kernel)
    with fastapi_testclient.TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200 and "Operations" in page.text
        asset = client.get("/static/app.js")
        assert asset.status_code == 200 and "loadRuntime" in asset.text

        body = client.get("/api/runtime").json()
        for key in ("enabled", "running", "owner", "in_flight", "sessions",
                    "scheduler", "events"):
            assert key in body, key
        assert "jobs" in body["scheduler"] and "runs" in body["scheduler"]
        assert "rules" in body["events"] and "events_recent" in body["events"]

        tick = client.post("/api/runtime/tick", json={"jobs": False})
        assert tick.status_code == 200
        assert "result" in tick.json() and "status" in tick.json()
        result = tick.json()["result"]
        assert set(result) >= {"enabled", "owner", "jobs", "missed", "events"}
