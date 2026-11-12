"""Sandbox: environment scrubbing, tier detection honesty, and real containment.

These tests start **real** child processes (a Python interpreter) because the
whole point of the sandbox is OS-level behaviour that cannot be faked with a
mock. They are written to be quick and to pass on both Windows (Job Object tier)
and POSIX (rlimit tier); where a guarantee is platform-specific it is asserted
only for the tier that provides it.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from jarvis.sandbox import SandboxService, limits_from_config, sanitized_env
from jarvis.sandbox.env import MIN_SECRET_LEN
from jarvis.sandbox.limits import Limits
from jarvis.sandbox.result import SandboxResult

PY = sys.executable


def _svc(cfg) -> SandboxService:
    return SandboxService(config=cfg)


# ---------------------------------------------------------------- env scrubbing

def test_sanitized_env_removes_secret_names_and_values():
    base = {
        "PATH": "/usr/bin",
        "JARVIS_SECRET_API_KEY": "abcdef123456",
        "SAFE": "ok",
        "INDIRECT": "abcdef123456",          # same value, different name
        "SHORT_COINCIDENCE": "1",            # too short to value-match
    }
    out = sanitized_env(base=base, secret_values=["abcdef123456", "1"])
    assert "JARVIS_SECRET_API_KEY" not in out
    assert "INDIRECT" not in out
    assert out["SAFE"] == "ok"
    assert out["PATH"] == "/usr/bin"
    assert out["SHORT_COINCIDENCE"] == "1"   # short secrets are not used for matching


def test_sanitized_env_scrubs_extras_but_keeps_non_secret_extras():
    out = sanitized_env(base={"PATH": "x"}, extra={"JARVIS_SECRET_T": "leak", "TITLE": "hi"},
                        secret_values=["leak"])
    assert "JARVIS_SECRET_T" not in out
    assert out["TITLE"] == "hi"


def test_min_secret_len_is_documented():
    assert MIN_SECRET_LEN >= 4


def test_scrub_can_be_disabled_by_config(cfg):
    cfg.sandbox.scrub_env = False
    env = _svc(cfg).child_env({"X": "1"})
    assert env["X"] == "1"
    assert set(os.environ).issubset(set(env))   # nothing removed


# ---------------------------------------------------------------- tier reporting

def test_tier_matrix_reports_every_tier_with_a_reason(cfg):
    status = _svc(cfg).status()
    seen = {t["tier"] for t in status["tiers"]}
    assert {"jobobject", "rlimit", "windows_sandbox", "container"} <= seen
    for tier in status["tiers"]:
        assert tier["reason"], f"{tier['tier']} must say why"


def test_active_tier_is_consistent_with_containment(cfg):
    status = _svc(cfg).status()
    if os.name == "nt":
        assert status["active_tier"] == "jobobject"
        assert status["contained"] is True
    else:
        assert status["active_tier"] in ("rlimit", "none")
    assert status["env_scrubbed"] is True
    assert status["isolation"]["network"].startswith("not isolated")


def test_contained_run_does_not_exist_when_disabled(cfg):
    cfg.sandbox.enabled = False
    svc = _svc(cfg)
    assert svc.active_tier() == "none"
    status = svc.status()
    assert status["contained"] is False
    assert status["isolation"]["process_tree"] == "not enforced"
    assert svc.health()["available"] is True   # disabled on purpose is not a fault


def test_health_names_the_active_tier(cfg):
    h = _svc(cfg).health()
    assert h["available"] is True and h["reason"]


def test_limits_from_config_reads_values(cfg):
    cfg.sandbox.max_memory_mb = 256
    cfg.sandbox.max_processes = 4
    lim = limits_from_config(cfg.sandbox)
    assert lim.max_memory_mb == 256 and lim.max_processes == 4


def test_limits_reject_nonsense(cfg):
    cfg.sandbox.max_memory_mb = "banana"
    assert limits_from_config(cfg.sandbox).max_memory_mb == 2048   # falls back to default


# ---------------------------------------------------------------- real runs

def test_run_executes_and_reports_tier(cfg):
    r = _svc(cfg).run([PY, "-c", "print('sandbox-ok')"])
    assert r.ok and r.exit_code == 0
    assert r.stdout.strip() == "sandbox-ok"
    assert r.tier in ("jobobject", "rlimit", "none")
    if r.tier in ("jobobject", "rlimit"):
        assert r.contained is True
    assert r.env_scrubbed is True


def test_missing_executable_is_reported_not_raised(cfg):
    r = _svc(cfg).run(["definitely-not-a-real-executable-xyz"])
    assert r.ok is False and r.exit_code is None
    assert "not found" in (r.error or "")


def test_child_does_not_inherit_secret_environment(cfg, monkeypatch):
    monkeypatch.setenv("JARVIS_SECRET_UNIT_TEST", "top-secret-value")
    r = _svc(cfg).run([PY, "-c",
                       "import os;print('JARVIS_SECRET_UNIT_TEST' in os.environ)"])
    assert r.ok
    assert r.stdout.strip() == "False"


def test_run_async_executes(cfg):
    import asyncio

    async def go():
        return await _svc(cfg).run_async([PY, "-c", "print('async-ok')"])

    r = asyncio.run(go())
    assert r.ok and r.stdout.strip() == "async-ok"


def test_timeout_kills_the_tree(cfg):
    r = _svc(cfg).run([PY, "-c", "import time;time.sleep(30)"], timeout=2)
    assert r.ok is False and r.timed_out is True
    assert "timed out" in (r.error or "")
    assert r.tier in ("jobobject", "rlimit")


def test_timeout_reaps_a_grandchild(cfg):
    psutil = pytest.importorskip("psutil")
    child = (
        "import subprocess,sys,time\n"
        "g = subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
        "print('GRANDCHILD', g.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    r = _svc(cfg).run([PY, "-c", child], timeout=3)
    assert r.timed_out is True
    line = next((l for l in r.stdout.splitlines() if l.startswith("GRANDCHILD")), None)
    assert line, f"grandchild pid was not reported: {r.stdout!r} / {r.stderr!r}"
    pid = int(line.split()[1])
    try:
        for _ in range(30):
            if not psutil.pid_exists(pid):
                break
            time.sleep(0.1)
        assert not psutil.pid_exists(pid), f"grandchild {pid} survived the sandbox timeout"
    finally:
        if psutil.pid_exists(pid):   # pragma: no cover - cleanup only
            try:
                os.kill(pid, 9)
            except Exception:
                pass


def test_memory_cap_stops_a_large_allocation(cfg):
    r = _svc(cfg).run(
        [PY, "-c", "b = bytearray(400*1024*1024); print('allocated')"],
        timeout=30, limits=Limits(max_memory_mb=96, wall_timeout_s=30))
    assert r.ok is False
    assert "allocated" not in r.stdout
    assert "MemoryError" in r.stderr or r.limit_terminated > 0


# ---------------------------------------------------------------- shell.run door

async def _shell(ctx, **kwargs):
    from jarvis.tools import REGISTRY, load_builtin_tools
    load_builtin_tools()
    return await REGISTRY.get("shell.run").run(ctx, **kwargs)


def _ctx(cfg):
    from jarvis.tools.base import ToolContext
    return ToolContext(config=cfg, db=None, bus=None)


async def test_shell_run_reports_the_sandbox_it_used(cfg):
    r = await _shell(_ctx(cfg), command="Write-Output sandbox-door" if os.name == "nt"
                     else "echo sandbox-door")
    assert r.ok, r.error
    assert "sandbox" in r.data and r.data["sandbox"]["env_scrubbed"] is True
    if os.name == "nt":
        assert r.data["sandbox"]["contained"] is True


async def test_shell_run_child_cannot_read_a_secret(cfg, monkeypatch):
    monkeypatch.setenv("JARVIS_SECRET_SHELL_TEST", "nope-nope-nope")
    cmd = ("if ($env:JARVIS_SECRET_SHELL_TEST) { 'LEAK' } else { 'clean' }"
           if os.name == "nt" else "echo ${JARVIS_SECRET_SHELL_TEST:-clean}")
    r = await _shell(_ctx(cfg), command=cmd)
    assert r.ok, r.error
    assert "LEAK" not in r.data["stdout"]
    assert "clean" in r.data["stdout"]


async def test_shell_run_timeout_is_reported(cfg):
    cmd = "Start-Sleep -Seconds 30" if os.name == "nt" else "sleep 30"
    r = await _shell(_ctx(cfg), command=cmd, timeout_s=2)
    assert r.ok is False
    assert r.data["exit_code"] not in (0, None)   # killed, with an observed code
    assert "timed out" in (r.error or "")
    assert r.data["sandbox"]["timed_out"] is True


# ---------------------------------------------------------------- the status tool

async def test_sandbox_status_tool_reads_the_service(cfg):
    from jarvis.tools import REGISTRY, load_builtin_tools, SideEffect
    load_builtin_tools()
    tool = REGISTRY.get("sandbox.status")
    assert tool is not None and tool.spec.side_effect == SideEffect.NONE
    r = await tool.run(_ctx(cfg))
    assert r.ok
    assert r.data["active_tier"] in ("jobobject", "rlimit", "none")
    assert "isolation" in r.data


async def test_sandbox_status_tool_uses_kernel_service(cfg):
    from jarvis.tools import REGISTRY, load_builtin_tools
    from jarvis.tools.base import ToolContext
    load_builtin_tools()
    svc = _svc(cfg)
    ctx = ToolContext(config=cfg, db=None, bus=None, services={"sandbox": svc})
    r = await REGISTRY.get("sandbox.status").run(ctx)
    assert r.ok
    assert r.data["active_tier"] == svc.active_tier()


def test_result_as_dict_is_serialisable():
    import json
    r = SandboxResult(ok=True, exit_code=0, tier="none")
    blob = json.dumps(r.as_dict())
    assert '"tier": "none"' in blob


# ---------------------------------------------------------------- over HTTP

def test_api_sandbox_endpoint(kernel):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from jarvis.api import create_app

    app = create_app(kernel)
    with fastapi_testclient.TestClient(app) as client:
        r = client.get("/api/sandbox")
        assert r.status_code == 200
        body = r.json()
        assert body["active_tier"] in ("jobobject", "rlimit", "none")
        assert "isolation" in body and "limits" in body

        health = client.get("/api/health").json()
        assert "sandbox" in health


# ---------------------------------------------------------------- the run_cmd door

def test_run_cmd_passes_stdin_through_the_sandbox():
    from jarvis.tools.builtin import _win
    r = _win.run_cmd([PY, "-c", "import sys;print('STDIN='+sys.stdin.read().strip())"],
                     input_text="ping-through")
    assert r["ok"], r
    assert r["stdout"].strip() == "STDIN=ping-through"


def test_run_cmd_scrubs_secret_environment(monkeypatch):
    from jarvis.tools.builtin import _win
    monkeypatch.setenv("JARVIS_SECRET_RC_TEST", "top-secret-value")
    r = _win.run_cmd([PY, "-c",
                      "import os;print('JARVIS_SECRET_RC_TEST' in os.environ)"])
    assert r["ok"] and r["stdout"].strip() == "False"


def test_run_cmd_timeout_is_reported():
    from jarvis.tools.builtin import _win
    r = _win.run_cmd([PY, "-c", "import time;time.sleep(30)"], timeout=2)
    assert r["ok"] is False
    assert r["code"] not in (0, None)
    assert "timed out" in (r["error"] or "")


def test_clipboard_write_goes_through_run_cmd(monkeypatch):
    if os.name != "nt":
        pytest.skip("exercises the Windows clipboard path via run_cmd")
    from jarvis.tools.builtin import _win, clipboard_tool
    calls = []

    def fake(argv, timeout=15, env=None, input_text=None):
        calls.append({"argv": argv, "input_text": input_text})
        return {"ok": True, "code": 0, "stdout": "x", "stderr": "", "error": None}

    monkeypatch.setattr(_win, "run_cmd", fake)
    ok, err = clipboard_tool._write_clipboard("clip-me")
    assert ok and err is None
    assert calls and calls[0]["input_text"] == "clip-me"


class _RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    async def run_async(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return SandboxResult(ok=True, exit_code=0, stdout="{}", stderr="",
                             tier="jobobject", contained=True)


def test_ffmpeg_exec_routes_through_the_sandbox_without_resource_caps():
    import asyncio
    from jarvis.tools.builtin import ffmpeg_tool

    rec = _RecordingSandbox()
    code, out, err = asyncio.run(ffmpeg_tool._exec(["ffprobe", "x"], 5.0, sandbox=rec))
    assert code == 0 and out == "{}"
    assert rec.calls and rec.calls[0][0] == ["ffprobe", "x"]
    lim = rec.calls[0][1]["limits"]
    # containment (tree-kill + scrubbed env) applies; resource caps are off for media
    assert lim.max_memory_mb == 0 and lim.max_processes == 0 and lim.scrub_env is True


# ---------------------------------------------------------------- independent-review fixes

def test_value_scrubbing_works_without_a_kernel(cfg, monkeypatch, tmp_path):
    """A service built from config alone (run_cmd, ffmpeg) still scrubs by value."""
    cfg.data_dir = str(tmp_path)
    (tmp_path / "secrets.json").write_text('{"mykey": "supersecret123"}', encoding="utf-8")
    monkeypatch.setenv("ALIAS_VAR", "supersecret123")   # same value, different name
    r = SandboxService(config=cfg).run(
        [PY, "-c", "import os;print('SEEN' if os.environ.get('ALIAS_VAR') else 'clean')"])
    assert r.ok and r.stdout.strip() == "clean"


def test_service_never_reruns_a_command_when_the_runner_raises(cfg, monkeypatch):
    from jarvis.sandbox import posix as posix_mod
    from jarvis.sandbox import win_job

    svc = _svc(cfg)
    tier = svc.active_tier()
    if tier == "none":
        pytest.skip("no containment tier on this platform")
    monkeypatch.setattr(svc, "active_tier", lambda: tier)
    mod, fn = ((win_job, "run_in_job") if tier == "jobobject"
               else (posix_mod, "run_with_limits"))

    def boom(*a, **k):
        raise RuntimeError("runner exploded after the child started")

    monkeypatch.setattr(mod, fn, boom)
    plain_calls = []
    monkeypatch.setattr(svc, "_plain", lambda *a, **k: plain_calls.append(a) or
                        SandboxResult(ok=True))
    res = svc.run([PY, "-c", "print('x')"])
    assert res.ok is False and res.contained is False
    assert any("NOT retried" in n for n in res.notes)
    assert plain_calls == []          # it must not run the command a second time


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects only")
def test_configure_failure_is_reported_not_hidden(monkeypatch):
    from jarvis.sandbox import win_job
    monkeypatch.setattr(win_job, "_configure", lambda job, limits: False)
    res = win_job.run_in_job([PY, "-c", "print('ran')"], env=dict(os.environ),
                             timeout=30, limits=Limits())
    assert res.exit_code == 0            # the command still runs...
    assert res.contained is False        # ...but containment is not claimed
    assert any("could not be applied" in n for n in res.notes)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects only")
def test_kill_tree_flag_reaches_the_job():
    import ctypes
    from ctypes import wintypes
    from jarvis.sandbox import win_job

    k = win_job._kernel32()
    for kill_tree, expected in ((True, True), (False, False)):
        job = k.CreateJobObjectW(None, None)
        try:
            assert win_job._configure(
                job, Limits(max_memory_mb=0, max_processes=0, kill_tree=kill_tree)) is True
            info = win_job._JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            ret = wintypes.DWORD(0)
            ok = k.QueryInformationJobObject(
                job, win_job._JobObjectExtendedLimitInformation, ctypes.byref(info),
                ctypes.sizeof(info), ctypes.byref(ret))
            assert ok
            flag = bool(info.BasicLimitInformation.LimitFlags
                        & win_job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
            assert flag is expected, f"kill_tree={kill_tree}"
        finally:
            k.CloseHandle(job)
