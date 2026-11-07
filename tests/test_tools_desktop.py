"""window.* and input.* tools.

Every effect is exercised against a FAKE backend: these tests must never move
real windows or inject real input (the owner may be using the machine). A
module-wide guard replaces the real SendInput with a function that fails the
test if it is ever reached.
"""

from __future__ import annotations

import ctypes

import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import SideEffect, ToolContext
from jarvis.tools.builtin import _win, input_tool, window_tool


@pytest.fixture(autouse=True)
def _no_real_desktop(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("real SendInput reached from a test")
    monkeypatch.setattr(_win, "send_inputs", _boom)
    yield
    window_tool.set_backend(None)
    input_tool.set_backend(None)


@pytest.fixture
def ctx(cfg):
    return ToolContext(config=cfg, db=None, bus=None)


# ---------------------------------------------------------------- window ---

class FakeWindows:
    def __init__(self):
        self.w = {
            100: {"hwnd": 100, "title": "Untitled - Notepad", "class": "Notepad", "pid": 11,
                  "process": "notepad.exe", "minimized": False, "maximized": False},
            200: {"hwnd": 200, "title": "Notes.txt - Notepad", "class": "Notepad", "pid": 12,
                  "process": "notepad.exe", "minimized": False, "maximized": False},
            300: {"hwnd": 300, "title": "Calculator", "class": "ApplicationFrameWindow",
                  "pid": 13, "process": "ApplicationFrameHost.exe", "minimized": False,
                  "maximized": False},
            900: {"hwnd": 900, "title": "Program Manager", "class": "Progman", "pid": 4,
                  "process": "explorer.exe", "minimized": False, "maximized": False},
        }
        self.fg = 300
        self.calls = []
        self.ignore_close = False

    def info(self, hwnd):
        item = self.w.get(hwnd)
        if item is None:
            return None
        return dict(item, foreground=(hwnd == self.fg), visible=True)

    def list_windows(self):
        return [self.info(h) for h in self.w]

    def foreground(self):
        return self.fg

    def is_window(self, hwnd):
        return hwnd in self.w

    def is_minimized(self, hwnd):
        return self.w[hwnd]["minimized"]

    def is_maximized(self, hwnd):
        return self.w[hwnd]["maximized"]

    def focus(self, hwnd):
        self.calls.append(("focus", hwnd))
        self.fg = hwnd
        self.w[hwnd]["minimized"] = False
        return True

    def show(self, hwnd, cmd):
        self.calls.append(("show", hwnd, cmd))
        if cmd == window_tool.SW_MINIMIZE:
            self.w[hwnd]["minimized"] = True
        elif cmd == window_tool.SW_MAXIMIZE:
            self.w[hwnd]["maximized"] = True
        return True

    def close(self, hwnd):
        self.calls.append(("close", hwnd))
        if not self.ignore_close:
            del self.w[hwnd]
        return True


@pytest.fixture
def fw():
    fake = FakeWindows()
    window_tool.set_backend(fake)
    return fake


def test_window_specs_side_effects():
    expect = {"window.list": SideEffect.NONE, "window.focus": SideEffect.LOCAL_EXEC,
              "window.minimize": SideEffect.LOCAL_EXEC, "window.maximize": SideEffect.LOCAL_EXEC,
              "window.close": SideEffect.DESTRUCTIVE}
    for name, side in expect.items():
        tool = REGISTRY.get(name)
        assert tool is not None, name
        assert tool.spec.side_effect == side
        assert "name" not in [p.name for p in tool.spec.params]


async def test_window_list_filters(ctx, fw):
    r = await REGISTRY.call("window.list", ctx)
    assert r.ok and r.data["count"] == 4 and r.data["foreground"] == 300
    r = await REGISTRY.call("window.list", ctx, title="notepad")
    assert r.ok and {w["hwnd"] for w in r.data["windows"]} == {100, 200}
    r = await REGISTRY.call("window.list", ctx, process="APPLICATIONFRAMEHOST.EXE")
    assert r.ok and r.data["count"] == 1


async def test_window_focus_by_unique_title_and_verify(ctx, fw):
    tool = REGISTRY.get("window.focus")
    r = await tool.run(ctx, title="Untitled")
    assert r.ok and r.data["hwnd"] == 100
    assert fw.calls == [("focus", 100)]
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["method"] == "GetForegroundWindow"


async def test_window_focus_verify_false_when_focus_refused(ctx, fw):
    fw.focus = lambda hwnd: False          # Windows foreground lock: nothing changes
    tool = REGISTRY.get("window.focus")
    r = await tool.run(ctx, hwnd=100)
    assert r.ok and r.certainty == "unverified"
    v = await tool.verify(ctx, r)
    assert v["verified"] is False and v["foreground"] == 300


async def test_window_ambiguous_title_refused(ctx, fw):
    r = await REGISTRY.get("window.minimize").run(ctx, title="notepad")
    assert not r.ok and "2 windows match" in r.error and "100" in r.error
    assert fw.calls == []


async def test_window_process_filter_disambiguates(ctx, fw):
    r = await REGISTRY.get("window.minimize").run(ctx, title="notes", process="notepad.exe")
    assert r.ok and r.data["hwnd"] == 200


async def test_window_requires_target_and_validates_hwnd(ctx, fw):
    r = await REGISTRY.get("window.maximize").run(ctx)
    assert not r.ok and "hwnd or title" in r.error
    r = await REGISTRY.get("window.maximize").run(ctx, hwnd="abc")
    assert not r.ok and "integer" in r.error
    r = await REGISTRY.get("window.maximize").run(ctx, hwnd=12345)
    assert not r.ok and "no window" in r.error
    r = await REGISTRY.get("window.maximize").run(ctx, title="nothing like this")
    assert not r.ok and "no visible window" in r.error


async def test_window_minimize_maximize_verify(ctx, fw):
    mn = REGISTRY.get("window.minimize")
    r = await mn.run(ctx, hwnd=300)
    assert r.ok
    assert (await mn.verify(ctx, r))["verified"] is True
    mx = REGISTRY.get("window.maximize")
    r = await mx.run(ctx, hwnd=100)
    assert r.ok and fw.w[100]["maximized"]
    assert (await mx.verify(ctx, r))["verified"] is True


async def test_window_close_verify_and_unverified_when_app_prompts(ctx, fw):
    tool = REGISTRY.get("window.close")
    r = await tool.run(ctx, hwnd=100)
    assert r.ok
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["exists"] is False

    fw.ignore_close = True   # e.g. app shows "save changes?"
    r = await tool.run(ctx, hwnd=200)
    assert r.ok
    v = await tool.verify(ctx, r)
    assert v["verified"] is False and "still open" in v["note"]


async def test_window_protected_shell_window_refused(ctx, fw):
    r = await REGISTRY.get("window.close").run(ctx, title="Program Manager")
    assert not r.ok and "protected" in r.error
    assert fw.calls == []


async def test_window_failed_run_verify_is_none(ctx, fw):
    from jarvis.tools.base import ToolResult
    assert await REGISTRY.get("window.close").verify(ctx, ToolResult(ok=False)) is None


async def test_window_backend_exception_is_captured(ctx, fw):
    def boom():
        raise OSError("EnumWindows failed")
    fw.list_windows = boom
    r = await REGISTRY.call("window.list", ctx)
    assert not r.ok and "EnumWindows failed" in r.error


async def test_window_unsupported_off_windows(ctx, monkeypatch):
    window_tool.set_backend(None)
    monkeypatch.setattr(_win, "IS_WINDOWS", False)
    r = await REGISTRY.call("window.list", ctx)
    assert not r.ok and "unsupported" in r.error
    r = await REGISTRY.get("window.focus").run(ctx, hwnd=1)
    assert not r.ok and "unsupported" in r.error


# ----------------------------------------------------------------- input ---

class FakeInput:
    def __init__(self):
        self.batches = []
        self.cursor = (0, 0)
        self.drop = False

    def send(self, inputs):
        self.batches.append(list(inputs))
        # emulate a cursor move for absolute mouse moves
        for i in inputs:
            if i.type == _win.INPUT_MOUSE and i.mi.dwFlags & _win.MOUSEEVENTF_ABSOLUTE:
                self.cursor = self._target
        return 0 if self.drop else len(inputs)

    def virtual_screen(self):
        return (0, 0, 1920, 1080)

    def cursor_pos(self):
        return self.cursor


@pytest.fixture
def fi():
    fake = FakeInput()
    input_tool.set_backend(fake)
    return fake


def test_input_struct_matches_win32_abi():
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(_win.INPUT) == expected


def test_input_specs():
    for name in ("input.type_text", "input.hotkey", "input.click"):
        tool = REGISTRY.get(name)
        assert tool.spec.side_effect == SideEffect.LOCAL_EXEC
        if name != "input.click":
            assert tool.verify(None, None) is None


def test_parse_hotkey():
    assert input_tool.parse_hotkey("ctrl+s") == [0x11, 0x53]
    assert input_tool.parse_hotkey("Ctrl + Shift + T") == [0x11, 0x10, 0x54]
    assert input_tool.parse_hotkey("f5") == [0x74]
    assert input_tool.parse_hotkey("ctrl++") == [0x11, 0xBB]
    for bad in ("", "ctrl+", "ctrl+nosuchkey", "a+b", "ctrl+ctrl+s",
                "ctrl+alt+shift+win+a+b"):
        with pytest.raises(ValueError):
            input_tool.parse_hotkey(bad)
    for refused in ("alt+f4", "win+l", "ctrl+alt+delete", "ctrl+alt+del"):
        with pytest.raises(PermissionError):
            input_tool.parse_hotkey(refused)


def test_hotkey_events_press_then_release_in_reverse():
    events = input_tool.hotkey_to_inputs([0x11, 0x10, 0x25])
    vks = [(e.ki.wVk, bool(e.ki.dwFlags & _win.KEYEVENTF_KEYUP)) for e in events]
    assert vks == [(0x11, False), (0x10, False), (0x25, False),
                   (0x25, True), (0x10, True), (0x11, True)]
    assert events[2].ki.dwFlags & _win.KEYEVENTF_EXTENDEDKEY   # arrow key is extended


def test_text_to_inputs_unicode_newline_and_surrogates():
    events = input_tool.text_to_inputs("a\n\U0001F600")
    # 'a' -> 2 events, '\n' -> Enter down/up, emoji -> 2 UTF-16 units x down/up
    assert len(events) == 2 + 2 + 4
    assert events[0].ki.wScan == ord("a") and events[0].ki.dwFlags == _win.KEYEVENTF_UNICODE
    assert events[2].ki.wVk == 0x0D
    assert events[4].ki.wScan == 0xD83D and events[6].ki.wScan == 0xDE00


async def test_type_text_sends_and_is_unverified(ctx, fi):
    tool = REGISTRY.get("input.type_text")
    r = await tool.run(ctx, text="hello")
    assert r.ok and r.data["events_sent"] == 10 and r.certainty == "unverified"
    assert tool.verify(ctx, r) is None


async def test_type_text_chunks_long_text(ctx, fi):
    r = await REGISTRY.get("input.type_text").run(ctx, text="x" * 100)
    assert r.ok and len(fi.batches) == 4          # 32+32+32+4


async def test_type_text_limits_and_secret_refusal(ctx, fi):
    tool = REGISTRY.get("input.type_text")
    r = await tool.run(ctx, text="y" * (input_tool.MAX_TEXT_CHARS + 1))
    assert not r.ok and "cap" in r.error
    r = await tool.run(ctx, text="")
    assert not r.ok
    ctx.secrets = lambda key: "hunter2-supersecret" if key == "smtp" else None
    r = await tool.run(ctx, text="my password is hunter2-supersecret ok")
    assert not r.ok and "secret" in r.error and r.evidence.get("refused")
    assert fi.batches == []


async def test_type_text_reports_blocked_injection(ctx, fi):
    fi.drop = True
    r = await REGISTRY.get("input.type_text").run(ctx, text="abc")
    assert not r.ok and "0/" in r.error


async def test_hotkey_tool(ctx, fi):
    r = await REGISTRY.get("input.hotkey").run(ctx, keys="ctrl+s")
    assert r.ok and r.data["vks"] == [0x11, 0x53] and r.data["events_sent"] == 4
    r = await REGISTRY.get("input.hotkey").run(ctx, keys="alt+f4")
    assert not r.ok and "window.close" in r.error
    assert len(fi.batches) == 1


async def test_click_bounds_and_verify(ctx, fi):
    tool = REGISTRY.get("input.click")
    fi._target = (960, 540)
    r = await tool.run(ctx, x=960, y=540, clicks=2, button="right")
    assert r.ok and r.data["events_sent"] == 5
    move = fi.batches[0][0]
    assert move.mi.dwFlags & _win.MOUSEEVENTF_ABSOLUTE
    assert abs(move.mi.dx - round(960 * 65535 / 1919)) <= 1
    v = tool.verify(ctx, r)
    assert v["cursor_at_target"] is True and v["verified"] is False

    for bad in ({"x": -1, "y": 5}, {"x": 1920, "y": 5}, {"x": 5, "y": 5000}):
        r = await tool.run(ctx, **bad)
        assert not r.ok and "outside" in r.error
    r = await tool.run(ctx, x=1, y=1, clicks=3)
    assert not r.ok and "clicks" in r.error
    r = await tool.run(ctx, x="a", y=1)
    assert not r.ok and "integer" in r.error


async def test_input_unsupported_off_windows(ctx, monkeypatch):
    input_tool.set_backend(None)
    monkeypatch.setattr(_win, "IS_WINDOWS", False)
    for name, kw in (("input.type_text", {"text": "a"}), ("input.hotkey", {"keys": "ctrl+s"}),
                     ("input.click", {"x": 1, "y": 1})):
        r = await REGISTRY.get(name).run(ctx, **kw)
        assert not r.ok and "unsupported" in r.error


# --------------------------------------------------------------- dry run ---

def test_state_changing_desktop_tools_accept_dry_run():
    for name in ("window.focus", "window.minimize", "window.maximize", "window.close",
                 "input.type_text", "input.hotkey", "input.click"):
        assert "dry_run" in [p.name for p in REGISTRY.get(name).spec.params], name


async def test_window_close_dry_run_previews_without_acting(ctx, fw):
    tool = REGISTRY.get("window.close")
    r = await tool.run(ctx, hwnd=100, dry_run=True)
    assert r.ok and r.data["dry_run"] is True and fw.calls == []
    assert r.data["target"]["title"] == "Untitled - Notepad"
    assert r.data["reversible"] is False and r.data["rollback"]
    v = await tool.verify(ctx, r)
    assert v["verified"] is False and v["method"] == "dry-run"


async def test_input_dry_run_sends_nothing(ctx, fi, fw):
    r = await REGISTRY.get("input.type_text").run(ctx, text="hi", dry_run=True)
    assert r.ok and r.data["dry_run"] and fi.batches == []
    assert r.data["target"]["focused_window"]["hwnd"] == 300
    r = await REGISTRY.get("input.hotkey").run(ctx, keys="ctrl+s", dry_run=True)
    assert r.ok and fi.batches == []
    r = await REGISTRY.get("input.click").run(ctx, x=5, y=5, dry_run=True)
    assert r.ok and fi.batches == []
    # refusals still apply in dry-run mode
    r = await REGISTRY.get("input.hotkey").run(ctx, keys="alt+f4", dry_run=True)
    assert not r.ok
