"""media.*, power.* and process.kill.

All effects run against FAKE backends: no real volume change, media key, lock,
sleep, shutdown or process kill can happen from this file. A guard swaps the
real SendInput and run_cmd for functions that fail the test if reached.
"""

from __future__ import annotations

import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import SideEffect, ToolContext, ToolResult
from jarvis.tools.builtin import _win, media_tool, power_tool, process_tool


@pytest.fixture(autouse=True)
def _guard(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("a real OS effect was reached from a test")
    monkeypatch.setattr(_win, "send_inputs", _boom)
    monkeypatch.setattr(_win, "run_cmd", _boom)
    yield
    media_tool.set_backend(None)
    power_tool.set_backend(None)
    process_tool.set_backend(None)


@pytest.fixture
def ctx(cfg):
    return ToolContext(config=cfg, db=None, bus=None)


def test_side_effect_classes():
    expect = {
        "media.volume_get": SideEffect.NONE, "media.volume_set": SideEffect.LOCAL_WRITE,
        "media.mute": SideEffect.LOCAL_WRITE, "media.play_pause": SideEffect.LOCAL_EXEC,
        "media.next": SideEffect.LOCAL_EXEC, "media.previous": SideEffect.LOCAL_EXEC,
        "power.lock": SideEffect.LOCAL_EXEC, "power.sleep": SideEffect.DESTRUCTIVE,
        "power.shutdown": SideEffect.DESTRUCTIVE, "power.restart": SideEffect.DESTRUCTIVE,
        "power.cancel": SideEffect.LOCAL_EXEC, "process.kill": SideEffect.DESTRUCTIVE,
    }
    for name, side in expect.items():
        tool = REGISTRY.get(name)
        assert tool is not None, name
        assert tool.spec.side_effect == side, name
        params = [p.name for p in tool.spec.params]
        assert "name" not in params
        if side != SideEffect.NONE:
            assert "dry_run" in params, f"{name} lacks dry_run"


# ------------------------------------------------------------------ media ---

class FakeAudio:
    def __init__(self, level=40, muted=False):
        self.level, self.muted = level, muted
        self.keys = []
        self.ignore_set = False

    def get(self):
        return {"level": self.level, "muted": self.muted}

    def set_level(self, level):
        if not self.ignore_set:
            self.level = level

    def set_mute(self, muted):
        if not self.ignore_set:
            self.muted = muted

    def media_key(self, action):
        self.keys.append(action)
        return 2


@pytest.fixture
def audio():
    fake = FakeAudio()
    media_tool.set_backend(fake)
    return fake


async def test_volume_get(ctx, audio):
    r = await REGISTRY.call("media.volume_get", ctx)
    assert r.ok and r.data == {"level": 40, "muted": False}


async def test_volume_set_and_readback(ctx, audio):
    tool = REGISTRY.get("media.volume_set")
    r = await tool.run(ctx, level=75)
    assert r.ok and r.data == {"level": 75, "previous": 40}
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["observed"] == 75


async def test_volume_set_readback_detects_ignored_write(ctx, audio):
    audio.ignore_set = True
    tool = REGISTRY.get("media.volume_set")
    r = await tool.run(ctx, level=90)
    v = await tool.verify(ctx, r)
    assert v["verified"] is False and v["observed"] == 40


async def test_volume_set_validation(ctx, audio):
    tool = REGISTRY.get("media.volume_set")
    for bad in (-1, 101, "loud", 50.5, True):
        r = await tool.run(ctx, level=bad)
        assert not r.ok, bad
    assert audio.level == 40


async def test_volume_set_dry_run_does_not_act(ctx, audio):
    tool = REGISTRY.get("media.volume_set")
    r = await tool.run(ctx, level=10, dry_run=True)
    assert r.ok and r.data["dry_run"] is True and audio.level == 40
    assert "40 -> 10" in r.data["expected_effect"]
    assert r.data["rollback"] == "media.volume_set level=40"
    v = await tool.verify(ctx, r)
    assert v["verified"] is False and v["method"] == "dry-run"


async def test_mute_toggle_explicit_and_readback(ctx, audio):
    tool = REGISTRY.get("media.mute")
    r = await tool.run(ctx)
    assert r.ok and r.data == {"muted": True, "previous": False}
    assert (await tool.verify(ctx, r))["verified"] is True
    r = await tool.run(ctx, state=False)
    assert r.ok and audio.muted is False
    r = await tool.run(ctx, state="maybe")
    assert not r.ok and "boolean" in r.error


async def test_media_keys(ctx, audio):
    for name, action in (("media.play_pause", "play_pause"), ("media.next", "next"),
                         ("media.previous", "previous")):
        tool = REGISTRY.get(name)
        r = await tool.run(ctx)
        assert r.ok and r.certainty == "unverified"
        assert tool.verify(ctx, r) is None
        assert audio.keys[-1] == action
    r = await REGISTRY.get("media.next").run(ctx, dry_run=True)
    assert r.ok and r.data["dry_run"] and audio.keys.count("next") == 1


async def test_media_backend_failure_is_captured(ctx, audio):
    def boom():
        raise OSError("no default audio endpoint")
    audio.get = boom
    r = await REGISTRY.call("media.volume_get", ctx)
    assert not r.ok and "no default audio endpoint" in r.error


async def test_media_unavailable_on_linux_without_pactl(ctx, monkeypatch):
    media_tool.set_backend(None)
    monkeypatch.setattr(_win, "IS_WINDOWS", False)
    monkeypatch.setattr(media_tool.shutil, "which", lambda _x: None)
    r = await REGISTRY.call("media.volume_get", ctx)
    assert not r.ok and "pactl" in r.error


def test_pactl_backend_parsing(monkeypatch):
    outputs = {
        ("get-sink-volume",): "Volume: front-left: 42598 /  65% / -11.23 dB,   front-right: ...",
        ("get-sink-mute",): "Mute: yes",
    }
    monkeypatch.setattr(media_tool.shutil, "which", lambda _x: "/usr/bin/pactl")
    monkeypatch.setattr(_win, "run_cmd", lambda argv, timeout=5: {
        "ok": True, "code": 0, "stdout": outputs[(argv[1],)], "stderr": "", "error": None})
    assert media_tool.PactlBackend().get() == {"level": 65, "muted": True}


# ------------------------------------------------------------------ power ---

class FakePower:
    def __init__(self):
        self.calls = []
        self.locked = False
        self.schedule_result = {"ok": True, "code": 0, "argv": ["shutdown.exe"]}
        self.cancel_result = {"ok": True, "code": 0, "argv": ["shutdown.exe", "/a"]}

    def lock(self):
        self.calls.append("lock")
        self.locked = True
        return {"ok": True, "code": 0, "method": "fake"}

    def is_locked(self):
        return self.locked

    def sleep(self):
        self.calls.append("sleep")
        return {"ok": True, "code": 0, "method": "fake"}

    def schedule(self, action, delay, message):
        self.calls.append(("schedule", action, delay, message))
        return self.schedule_result

    def cancel(self):
        self.calls.append("cancel")
        return self.cancel_result


@pytest.fixture
def power():
    fake = FakePower()
    power_tool.set_backend(fake)
    return fake


async def test_power_lock_and_readback(ctx, power):
    tool = REGISTRY.get("power.lock")
    r = await tool.run(ctx)
    assert r.ok and power.calls == ["lock"]
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["method"] == "lock-screen-readback"


async def test_power_lock_dry_run(ctx, power):
    r = await REGISTRY.get("power.lock").run(ctx, dry_run=True)
    assert r.ok and r.data["dry_run"] and power.calls == []


async def test_power_sleep_unverified(ctx, power):
    tool = REGISTRY.get("power.sleep")
    r = await tool.run(ctx)
    assert r.ok and power.calls == ["sleep"]
    assert tool.verify(ctx, r) is None


async def test_shutdown_default_delay_and_message(ctx, power):
    tool = REGISTRY.get("power.shutdown")
    r = await tool.run(ctx, reason='updates"; rm -rf /')
    assert r.ok and r.data["delay_s"] == power_tool.DEFAULT_DELAY_S == 60
    _, action, delay, message = power.calls[0]
    assert action == "shutdown" and delay == 60
    assert '"' not in message and "rm -rf /" not in message and "updates" in message
    assert "power.cancel" in r.data["cancel_with"]
    assert tool.verify(ctx, r) is None


async def test_restart_delay_bounds(ctx, power):
    tool = REGISTRY.get("power.restart")
    for bad in (0, 5, power_tool.MIN_DELAY_S - 1, power_tool.MAX_DELAY_S + 1, "soon"):
        r = await tool.run(ctx, delay_s=bad)
        assert not r.ok, bad
    assert power.calls == []
    r = await tool.run(ctx, delay_s=300)
    assert r.ok and power.calls[0][1:3] == ("restart", 300)


async def test_shutdown_dry_run_explains_cancel(ctx, power):
    r = await REGISTRY.get("power.shutdown").run(ctx, dry_run=True, delay_s=120)
    assert r.ok and r.data["dry_run"] and power.calls == []
    assert "power.cancel" in r.data["rollback"] and r.data["delay_s"] == 120


async def test_shutdown_failure_is_not_retried(ctx, power):
    power.schedule_result = {"ok": False, "code": 1190, "argv": [], "error": "scheduled"}
    r = await REGISTRY.get("power.shutdown").run(ctx)
    assert not r.ok and "already scheduled" in r.error
    assert len(power.calls) == 1            # exactly one attempt


async def test_power_cancel(ctx, power):
    tool = REGISTRY.get("power.cancel")
    r = await tool.run(ctx)
    assert r.ok and r.data["cancelled"] is True
    power.cancel_result = {"ok": False, "code": 1116, "argv": []}
    r = await tool.run(ctx)
    assert not r.ok and "no shutdown" in r.error


def test_real_backend_builds_cancellable_argv(monkeypatch):
    seen = {}

    def fake_run(argv, timeout=15, **k):
        seen["argv"] = argv
        return {"ok": True, "code": 0, "stdout": "", "stderr": "", "error": None}
    monkeypatch.setattr(_win, "run_cmd", fake_run)
    monkeypatch.setattr(_win, "IS_WINDOWS", True)
    power_tool.SystemPowerBackend().schedule("restart", 90, "msg")
    assert seen["argv"] == ["shutdown.exe", "/r", "/t", "90", "/c", "msg"]
    monkeypatch.setattr(_win, "IS_WINDOWS", False)
    power_tool.SystemPowerBackend().schedule("shutdown", 90, "msg")
    assert seen["argv"] == ["shutdown", "-h", "+2", "msg"]
    power_tool.SystemPowerBackend().cancel()
    assert seen["argv"] == ["shutdown", "-c"]


# ---------------------------------------------------------------- process ---

class FakeProcs:
    def __init__(self):
        self.procs = {
            500: {"pid": 500, "name": "notepad.exe", "create_time": 1.0, "username": "me"},
            501: {"pid": 501, "name": "chrome.exe", "create_time": 2.0, "username": "me"},
            502: {"pid": 502, "name": "chrome.exe", "create_time": 3.0, "username": "me"},
            600: {"pid": 600, "name": "lsass.exe", "create_time": 0.5, "username": "SYSTEM"},
            700: {"pid": 700, "name": "python.exe", "create_time": 4.0, "username": "me"},
            701: {"pid": 701, "name": "powershell.exe", "create_time": 0.9, "username": "me"},
        }
        self.me = {700, 701}
        self.stubborn = set()
        self.stopped = []

    def self_and_ancestors(self):
        return set(self.me)

    def info(self, pid):
        p = self.procs.get(pid)
        return dict(p) if p else None

    def by_name(self, image):
        return [dict(p) for p in self.procs.values() if p["name"].lower() == image.lower()]

    def stop(self, targets, force, timeout):
        gone, alive = [], []
        for t in targets:
            self.stopped.append((t["pid"], force))
            if t["pid"] in self.stubborn and not force:
                alive.append(t["pid"])
            else:
                self.procs.pop(t["pid"], None)
                gone.append(t["pid"])
        return gone, alive, {}

    def alive(self, pid, create_time):
        p = self.procs.get(pid)
        return bool(p) and (create_time is None or p["create_time"] == create_time)


@pytest.fixture
def procs():
    fake = FakeProcs()
    process_tool.set_backend(fake)
    return fake


async def test_kill_by_pid_and_verify(ctx, procs):
    tool = REGISTRY.get("process.kill")
    r = await tool.run(ctx, pid=500)
    assert r.ok and r.data["terminated"] == [500] and procs.stopped == [(500, False)]
    v = await tool.verify(ctx, r)
    assert v["verified"] is True


async def test_verify_detects_survivor_and_ignores_pid_reuse(ctx, procs):
    tool = REGISTRY.get("process.kill")
    fake_result = ToolResult(ok=True, data={"targets": [
        {"pid": 501, "create_time": 2.0},            # same process still alive
        {"pid": 502, "create_time": 99.0}]})         # pid reused by a different process
    v = await tool.verify(ctx, fake_result)
    assert v["verified"] is False and v["still_running"] == [501]


async def test_kill_protected_and_self_refused(ctx, procs):
    tool = REGISTRY.get("process.kill")
    for kw in ({"pid": 600}, {"process": "LSASS.EXE"}, {"pid": 700}, {"pid": 701},
               {"pid": 4}):
        if kw.get("pid") == 4:
            procs.procs[4] = {"pid": 4, "name": "System", "create_time": 0, "username": None}
        r = await tool.run(ctx, **kw)
        assert not r.ok and r.error.startswith("refused"), kw
    assert procs.stopped == []


async def test_kill_by_name_ambiguity_and_all(ctx, procs):
    tool = REGISTRY.get("process.kill")
    r = await tool.run(ctx, process="chrome.exe")
    assert not r.ok and "2 processes" in r.error and procs.stopped == []
    r = await tool.run(ctx, process="chrome.exe", all=True)
    assert r.ok and sorted(r.data["terminated"]) == [501, 502]


async def test_kill_argument_validation(ctx, procs):
    tool = REGISTRY.get("process.kill")
    for kw in ({}, {"pid": 500, "process": "notepad.exe"}, {"pid": "x"},
               {"process": "C:\\Windows\\notepad.exe"}, {"pid": 99999},
               {"process": "nothing.exe"}):
        r = await tool.run(ctx, **kw)
        assert not r.ok, kw
    assert procs.stopped == []


async def test_kill_survivor_is_reported_not_escalated(ctx, procs):
    procs.stubborn.add(500)
    r = await REGISTRY.get("process.kill").run(ctx, pid=500)
    assert not r.ok and "still running" in r.error and "force=true" in r.error
    assert procs.stopped == [(500, False)]         # no automatic force retry


async def test_kill_dry_run(ctx, procs):
    r = await REGISTRY.get("process.kill").run(ctx, pid=500, dry_run=True)
    assert r.ok and r.data["dry_run"] and r.data["reversible"] is False
    assert r.data["target"][0]["name"] == "notepad.exe" and procs.stopped == []


def test_real_backend_protects_own_process():
    backend = process_tool.PsutilProcessBackend()
    import os
    assert os.getpid() in backend.self_and_ancestors()
