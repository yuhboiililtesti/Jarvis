"""apps.launch: validate before asking, and be honest about what is installed.

`preflight` exists so an impossible request (an app that is not allowlisted, or
points at a missing executable) is refused *before* the owner is asked to approve
something that cannot work.
"""

from __future__ import annotations

import sys

from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import apps


def test_installed_resolves_paths_and_names():
    assert apps._installed(sys.executable) is True
    assert apps._installed("definitely-not-a-real-executable-xyz") is False
    assert apps._installed("") is False


def test_default_allowlist_is_used_when_kv_is_absent(cfg, db):
    ctx = ToolContext(config=cfg, db=db, bus=None)
    default = apps._allowlist(ctx)
    assert len(default) >= 1                       # a fresh install can open something
    db.kv_set(apps.ALLOWLIST_KEY, {"custom": "notepad.exe"})
    assert apps._allowlist(ctx) == {
        "custom": {"target": "notepad.exe", "allow_args": False}}   # owner override wins


def test_catalog_reports_installed(cfg, db):
    from types import SimpleNamespace

    db.kv_set(apps.ALLOWLIST_KEY, {"real": sys.executable, "ghost": "no-such-exe-xyz"})
    entries = apps.catalog(SimpleNamespace(db=db))
    assert entries["real"]["installed"] is True
    assert entries["ghost"]["installed"] is False


async def test_the_agent_prompt_lists_ready_apps(kernel, monkeypatch):
    monkeypatch.setattr(apps, "_installed", lambda target: True)
    kernel.db.kv_set(apps.ALLOWLIST_KEY, {"steam": "steam.exe", "notepad": "notepad.exe"})
    hint = kernel.agent._routing_hint()
    assert "steam" in hint and "notepad" in hint
    assert "apps.launch" in hint and "web.open" in hint


async def test_list_reports_installed_status(kernel):
    ctx = ToolContext(config=kernel.config, db=kernel.db, bus=kernel.bus)
    result = await kernel.registry.call("apps.list", ctx)
    assert result.ok
    assert "installed" in result.data
    assert set(result.data["installed"]) == set(result.data["apps"])


def test_app_intent_matches_only_installed_apps(kernel, monkeypatch):
    monkeypatch.setattr(apps, "_installed", lambda target: True)
    kernel.db.kv_set(apps.ALLOWLIST_KEY, {"steam": "steam.exe", "notepad": "notepad.exe"})
    assert kernel.agent._app_intent("open steam") == "steam"
    assert kernel.agent._app_intent("please launch the steam app") == "steam"
    assert kernel.agent._app_intent("start notepad") == "notepad"
    assert kernel.agent._app_intent("open https://steam.com") is None
    assert kernel.agent._app_intent("open steam.com") is None
    assert kernel.agent._app_intent("open a file") is None
    assert kernel.agent._app_intent("what is steam") is None


async def test_open_app_routes_without_calling_the_model(kernel, monkeypatch):
    monkeypatch.setattr(apps, "_installed", lambda target: True)
    kernel.db.kv_set(apps.ALLOWLIST_KEY, {"notepad": "notepad.exe"})
    seen: dict = {}

    async def fake_exec(task, name, args, **kwargs):
        from jarvis.tools.base import ToolResult
        seen["name"] = name
        seen["args"] = args
        return ToolResult(ok=True, data={"launched": True}), {"verified": False}

    async def boom(*args, **kwargs):
        raise AssertionError("the model must not be called for an unambiguous app open")
        yield  # pragma: no cover

    monkeypatch.setattr(kernel.controller, "execute_tool", fake_exec)
    monkeypatch.setattr(kernel.router, "chat", boom)
    events = [ev async for ev in kernel.agent.run_turn(
        "open notepad", session=kernel.session("app-route"))]
    assert seen == {"name": "apps.launch", "args": {"app": "notepad"}}
    assert any(e.get("kind") == "tool_call" and e.get("tool") == "apps.launch" for e in events)
    assert events[-1]["kind"] == "final" and events[-1]["status"] == "succeeded"


async def test_unknown_app_is_refused_without_an_approval(kernel):
    task = kernel.controller.create("open madeupapp")
    result, evidence = await kernel.controller.execute_tool(
        task, "apps.launch", {"app": "madeupapp"})
    assert result.ok is False
    assert "allowlist" in (result.error or "")
    assert evidence.get("method") == "preflight"
    assert kernel.db.query_one(
        "SELECT COUNT(*) AS n FROM approvals WHERE tool='apps.launch'")["n"] == 0


async def test_missing_target_is_refused_without_an_approval(kernel):
    kernel.db.kv_set(apps.ALLOWLIST_KEY, {"ghost": r"C:\definitely\not\here.exe"})
    task = kernel.controller.create("open ghost")
    result, evidence = await kernel.controller.execute_tool(
        task, "apps.launch", {"app": "ghost"})
    assert result.ok is False
    assert "not installed" in (result.error or "")
    assert evidence.get("method") == "preflight"
    assert kernel.db.query_one(
        "SELECT COUNT(*) AS n FROM approvals WHERE tool='apps.launch'")["n"] == 0


async def test_extra_args_are_refused_before_an_approval(kernel, monkeypatch):
    """The allowlist must not become an arbitrary-execute primitive.

    ``explorer.exe <path>`` executes the file it is handed, so an allowlisted
    app plus model-supplied args would escape the list. Arguments stay opt-in.
    """
    monkeypatch.setattr(apps, "_installed", lambda target: True)
    kernel.db.kv_set(apps.ALLOWLIST_KEY, {"explorer": "explorer.exe"})
    task = kernel.controller.create("open explorer with a file")
    result, evidence = await kernel.controller.execute_tool(
        task, "apps.launch",
        {"app": "explorer", "args": [r"C:\Users\<USER>\Downloads\evil.exe"]})
    assert result.ok is False
    assert "does not accept extra arguments" in (result.error or "")
    assert evidence.get("method") == "preflight"
    assert kernel.db.query_one(
        "SELECT COUNT(*) AS n FROM approvals WHERE tool='apps.launch'")["n"] == 0


def test_owner_can_opt_into_arguments_per_app(cfg, db):
    ctx = ToolContext(config=cfg, db=db, bus=None)
    db.kv_set(apps.ALLOWLIST_KEY, {
        "vlc": {"target": "vlc.exe", "allow_args": True},
        "plain": "notepad.exe",
    })
    entries = apps._allowlist(ctx)
    assert entries["vlc"]["allow_args"] is True
    assert entries["plain"]["allow_args"] is False


def test_args_opt_in_reaches_preflight(cfg, db, monkeypatch):
    ctx = ToolContext(config=cfg, db=db, bus=None)
    db.kv_set(apps.ALLOWLIST_KEY, {"vlc": {"target": "vlc.exe", "allow_args": True}})
    monkeypatch.setattr(apps, "_installed", lambda target: True)
    tool = apps.AppsLaunchTool()
    assert tool.preflight(ctx, app="vlc", args=["--play-and-exit"]) is None


async def test_a_working_app_still_goes_to_approval(kernel, monkeypatch):
    """Preflight must not bypass the gate: a valid, installed app still asks."""
    import subprocess

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    kernel.config.policy.approval_timeout_s = 0.1
    kernel.db.kv_set(apps.ALLOWLIST_KEY, {"notepad": "notepad.exe"})
    monkeypatch.setattr(apps, "_installed", lambda target: True)
    task = kernel.controller.create("open notepad")
    result, _ = await kernel.controller.execute_tool(
        task, "apps.launch", {"app": "notepad"})
    assert result.ok is False                      # approval timed out
    assert kernel.db.query_one(
        "SELECT COUNT(*) AS n FROM approvals WHERE tool='apps.launch'")["n"] == 1
