"""Synthetic input: ``input.type_text`` / ``input.hotkey`` / ``input.click``.

Windows-only, via ``user32.SendInput`` (stdlib ctypes). All three are
LOCAL_EXEC so the policy broker gates them behind owner approval in ``ask``
mode: injected input lands in *whatever window has focus*, which the tool
cannot see. Safety rails that do not depend on policy:

  * ``type_text`` is capped at :data:`MAX_TEXT_CHARS` and refuses text that
    contains a configured secret value (a password field is not detectable,
    so the next best thing is never typing a known secret at all);
  * ``hotkey`` refuses combinations that would bypass a stronger gate:
    ``alt+f4`` (use ``window.close``, DESTRUCTIVE), ``win+l`` (use
    ``power.lock``) and ``ctrl+alt+delete`` (the secure attention sequence
    cannot be injected at all);
  * ``click`` refuses coordinates outside the virtual desktop.

Read-back: typed text and hotkeys have no independent read-back (the target
app owns the effect), so ``verify`` returns ``None`` and the attempt is
recorded as unverified. ``click`` reads the cursor position back, which proves
the pointer reached the target but not what the click did, so it is reported
with ``verified: False`` plus that partial evidence.

Known OS limits (reported, not hidden): UIPI blocks injection into windows of
a higher integrity level (e.g. an elevated app) and SendInput may still report
the events as inserted; coordinates are in this process's DPI context.
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common, _win

log = get_logger("jarvis.tools.input")

__all__ = [
    "InputTypeTextTool", "InputHotkeyTool", "InputClickTool", "WinInputBackend",
    "parse_hotkey", "text_to_inputs", "MAX_TEXT_CHARS", "set_backend", "health",
]

MAX_TEXT_CHARS = 1000
MAX_COMBO_KEYS = 5
_CHUNK = 32                      # characters per SendInput burst

# Secret names whose values must never be typed. Values are looked up via
# ctx.secrets at call time; they are never logged or returned.
_SECRET_NAMES = ("smtp", "home_assistant", "searx_url", "node_token", "api_key",
                 "password", "token")

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN = 0x10, 0x11, 0x12, 0x5B
VK_RETURN, VK_TAB = 0x0D, 0x09

_MODIFIERS = {
    "ctrl": VK_CONTROL, "control": VK_CONTROL,
    "alt": VK_MENU, "menu": VK_MENU,
    "shift": VK_SHIFT,
    "win": VK_LWIN, "windows": VK_LWIN, "super": VK_LWIN, "meta": VK_LWIN, "cmd": VK_LWIN,
}

_NAMED_KEYS = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "ins": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "printscreen": 0x2C, "prtsc": 0x2C, "capslock": 0x14,
    "minus": 0xBD, "-": 0xBD, "plus": 0xBB, "equals": 0xBB, "=": 0xBB,
    "comma": 0xBC, ",": 0xBC, "period": 0xBE, ".": 0xBE, "slash": 0xBF, "/": 0xBF,
    "semicolon": 0xBA, ";": 0xBA, "quote": 0xDE, "'": 0xDE, "[": 0xDB,
    "bracketleft": 0xDB, "]": 0xDD, "bracketright": 0xDD, "backslash": 0xDC,
    "\\": 0xDC, "backtick": 0xC0, "`": 0xC0, "apps": 0x5D,
}
for _i in range(1, 25):
    _NAMED_KEYS[f"f{_i}"] = 0x6F + _i          # F1 = 0x70
for _c in "abcdefghijklmnopqrstuvwxyz":
    _NAMED_KEYS[_c] = ord(_c.upper())
for _d in "0123456789":
    _NAMED_KEYS[_d] = ord(_d)

# Keys that need KEYEVENTF_EXTENDEDKEY to be delivered as the navigation key
# rather than its numpad twin.
_EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x2C,
             VK_LWIN, 0x5D}

# Combos refused because a dedicated, more strictly gated tool exists (or the
# OS forbids injecting them).
_REFUSED_COMBOS = {
    frozenset({VK_MENU, 0x73}): "alt+f4 closes windows; use window.close (DESTRUCTIVE gate)",
    frozenset({VK_LWIN, ord("L")}): "win+l locks the session; use power.lock",
    frozenset({VK_CONTROL, VK_MENU, 0x2E}): ("ctrl+alt+delete is the secure attention "
                                             "sequence and cannot be injected"),
}


def parse_hotkey(combo: str) -> list[int]:
    """'ctrl+shift+s' -> [VK_CONTROL, VK_SHIFT, 0x53]. Raises ValueError."""
    text = str(combo or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("keys is required, e.g. 'ctrl+s'")
    # Allow a literal '+' key written as 'ctrl++' -> ['ctrl', '+'].
    parts = text.split("+")
    if text.endswith("++"):
        parts = parts[:-2] + ["plus"]
    if any(p == "" for p in parts):
        raise ValueError(f"malformed hotkey {combo!r}")
    if len(parts) > MAX_COMBO_KEYS:
        raise ValueError(f"at most {MAX_COMBO_KEYS} keys per hotkey")
    vks: list[int] = []
    for part in parts:
        vk = _MODIFIERS.get(part, _NAMED_KEYS.get(part))
        if vk is None:
            raise ValueError(f"unknown key {part!r} in {combo!r}")
        if vk in vks:
            raise ValueError(f"key {part!r} repeated in {combo!r}")
        vks.append(vk)
    non_mod = [v for v in vks if v not in _MODIFIERS.values()]
    if len(non_mod) > 1:
        raise ValueError("a hotkey combines modifiers with at most one key, e.g. 'ctrl+s'")
    refused = _REFUSED_COMBOS.get(frozenset(vks))
    if refused:
        raise PermissionError(f"refused: {refused}")
    return vks


def hotkey_to_inputs(vks: list[int]) -> list[_win.INPUT]:
    """Press keys in order, release in reverse order."""
    down = [_win.key_input(vk=v, flags=_win.KEYEVENTF_EXTENDEDKEY if v in _EXTENDED else 0)
            for v in vks]
    up = [_win.key_input(vk=v, flags=_win.KEYEVENTF_KEYUP
                         | (_win.KEYEVENTF_EXTENDEDKEY if v in _EXTENDED else 0))
          for v in reversed(vks)]
    return down + up


def text_to_inputs(text: str) -> list[_win.INPUT]:
    """Unicode key events; newline/tab become real Enter/Tab presses."""
    out: list[_win.INPUT] = []
    for ch in text:
        if ch == "\r":
            continue
        if ch in ("\n", "\t"):
            vk = VK_RETURN if ch == "\n" else VK_TAB
            out.append(_win.key_input(vk=vk))
            out.append(_win.key_input(vk=vk, flags=_win.KEYEVENTF_KEYUP))
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):           # surrogate pairs -> 2 units
            code = int.from_bytes(units[i:i + 2], "little")
            out.append(_win.key_input(scan=code, flags=_win.KEYEVENTF_UNICODE))
            out.append(_win.key_input(scan=code,
                                      flags=_win.KEYEVENTF_UNICODE | _win.KEYEVENTF_KEYUP))
    return out


class WinInputBackend:
    def __init__(self) -> None:
        if not _win.IS_WINDOWS:
            raise OSError(_win.UNSUPPORTED)
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.u.GetSystemMetrics.argtypes = (ctypes.c_int,)
        self.u.GetSystemMetrics.restype = ctypes.c_int
        self.u.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        self.u.GetCursorPos.restype = wintypes.BOOL

    def send(self, inputs: list[_win.INPUT]) -> int:
        return _win.send_inputs(inputs)

    def virtual_screen(self) -> tuple[int, int, int, int]:
        g = self.u.GetSystemMetrics
        return g(76), g(77), g(78), g(79)    # SM_X/Y/CX/CYVIRTUALSCREEN

    def cursor_pos(self) -> tuple[int, int]:
        pt = wintypes.POINT()
        if not self.u.GetCursorPos(ctypes.byref(pt)):
            raise OSError(f"GetCursorPos failed (winerror {ctypes.get_last_error()})")
        return int(pt.x), int(pt.y)


_BACKEND: Any = None


def set_backend(backend: Any) -> None:
    global _BACKEND
    _BACKEND = backend


def _backend() -> Any:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = WinInputBackend()
    return _BACKEND


def health() -> dict[str, Any]:
    if _BACKEND is not None:
        return _common.health_ok("backend injected")
    if not _win.IS_WINDOWS:
        return _common.health_fail(_win.UNSUPPORTED)
    return _common.health_ok("user32.SendInput via ctypes")


def _unsupported() -> ToolResult | None:
    return _common.unavailable_result(health())


_NO_ROLLBACK = ("none automatic; input is delivered to whichever window has focus, "
                "undo inside that app (e.g. ctrl+z) if needed")


def _contains_secret(ctx: ToolContext, text: str) -> bool:
    values: list[str] = []
    lookup = getattr(ctx, "secrets", None)
    if callable(lookup):
        for key in _SECRET_NAMES:
            try:
                v = lookup(key)
            except Exception:
                v = None
            if v:
                values.append(str(v))
    token = getattr(getattr(getattr(ctx, "config", None), "nodes", None), "token", "")
    if token:
        values.append(str(token))
    return any(len(v) >= 6 and v in text for v in values)


def _focused_target() -> dict[str, Any]:
    """Best-effort description of the window that would receive input (read-only)."""
    try:
        from . import window_tool
        wb = window_tool._backend()
        info = wb.info(wb.foreground())
        if info:
            return {"focused_window": {k: info.get(k) for k in ("hwnd", "title", "process")}}
    except Exception as exc:
        return {"focused_window": None, "note": f"could not read focus: {exc}"}
    return {"focused_window": None}


@register(["type_text", "keyboard.type"])
class InputTypeTextTool(BaseTool):
    spec = ToolSpec(
        name="input.type_text",
        summary=(f"Type text into the focused window (max {MAX_TEXT_CHARS} chars). "
                 "Never pass passwords or secrets."),
        category="input",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(
            ToolParam("text", "string", "Text to type into the currently focused window.",
                      required=True),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {chars, events_sent, events_expected}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        unsupported = _unsupported()
        if unsupported:
            return unsupported
        try:
            text = kwargs.get("text")
            if not isinstance(text, str) or text == "":
                return ToolResult(ok=False, error="text is required")
            if len(text) > MAX_TEXT_CHARS:
                return ToolResult(ok=False, error=(f"text is {len(text)} chars; the cap is "
                                                   f"{MAX_TEXT_CHARS} (split it or use files.write)"))
            if _contains_secret(ctx, text):
                return ToolResult(ok=False, evidence={"refused": True},
                                  error="refused: text contains a configured secret value")
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target=_focused_target(), operation="input.type_text",
                    effect=(f"type {len(text)} characters ({len(text_to_inputs(text))} key "
                            "events) into the focused window"),
                    reversible=False, rollback=_NO_ROLLBACK, chars=len(text))
            backend = _backend()
            sent = expected = 0
            for i in range(0, len(text), _CHUNK):
                events = text_to_inputs(text[i:i + _CHUNK])
                expected += len(events)
                got = await asyncio.to_thread(backend.send, events)
                sent += got
                if got != len(events):
                    break
                await asyncio.sleep(0.01)
            ok = sent == expected
            return ToolResult(
                ok=ok,
                data={"chars": len(text), "events_sent": sent, "events_expected": expected},
                error=None if ok else (f"Windows inserted {sent}/{expected} input events "
                                       "(blocked by UIPI or a secure desktop?)"),
                evidence={"method": "SendInput", "events_sent": sent},
                certainty="unverified",
            )
        except Exception as exc:
            log.warning("input.type_text failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unknown")

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        return None  # the receiving app owns the effect; no independent read-back


@register(["hotkey", "keyboard.hotkey"])
class InputHotkeyTool(BaseTool):
    spec = ToolSpec(
        name="input.hotkey",
        summary="Press a key combination in the focused window, e.g. 'ctrl+s' or 'alt+tab'.",
        category="input",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(
            ToolParam("keys", "string", "Combination joined by '+', e.g. 'ctrl+shift+t'.",
                      required=True),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {keys, vks, events_sent}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        unsupported = _unsupported()
        if unsupported:
            return unsupported
        try:
            combo = str(kwargs.get("keys") or "")
            try:
                vks = parse_hotkey(combo)
            except PermissionError as exc:
                return ToolResult(ok=False, error=str(exc), evidence={"refused": True})
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            events = hotkey_to_inputs(vks)
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target=_focused_target(), operation="input.hotkey",
                    effect=f"press {combo.strip().lower()} ({len(events)} key events) in the "
                           "focused window", reversible=False, rollback=_NO_ROLLBACK, vks=vks)
            sent = await asyncio.to_thread(_backend().send, events)
            ok = sent == len(events)
            return ToolResult(
                ok=ok,
                data={"keys": combo.strip().lower(), "vks": vks, "events_sent": sent},
                error=None if ok else f"Windows inserted {sent}/{len(events)} input events",
                evidence={"method": "SendInput", "events_sent": sent},
                certainty="unverified",
            )
        except Exception as exc:
            log.warning("input.hotkey failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unknown")

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        return None


_BUTTONS = {
    "left": (_win.MOUSEEVENTF_LEFTDOWN, _win.MOUSEEVENTF_LEFTUP),
    "right": (_win.MOUSEEVENTF_RIGHTDOWN, _win.MOUSEEVENTF_RIGHTUP),
    "middle": (_win.MOUSEEVENTF_MIDDLEDOWN, _win.MOUSEEVENTF_MIDDLEUP),
}


def click_inputs(x: int, y: int, screen: tuple[int, int, int, int], button: str,
                 clicks: int) -> list[_win.INPUT]:
    vx, vy, vw, vh = screen
    nx = round((x - vx) * 65535 / max(1, vw - 1))
    ny = round((y - vy) * 65535 / max(1, vh - 1))
    move = _win.mouse_input(nx, ny, _win.MOUSEEVENTF_MOVE | _win.MOUSEEVENTF_ABSOLUTE
                            | _win.MOUSEEVENTF_VIRTUALDESK)
    down_f, up_f = _BUTTONS[button]
    seq = [move]
    for _ in range(clicks):
        seq.append(_win.mouse_input(0, 0, down_f))
        seq.append(_win.mouse_input(0, 0, up_f))
    return seq


@register(["click", "mouse.click"])
class InputClickTool(BaseTool):
    spec = ToolSpec(
        name="input.click",
        summary="Move the pointer to screen pixel (x, y) and click.",
        category="input",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(
            ToolParam("x", "integer", "Virtual-desktop X pixel coordinate.", required=True),
            ToolParam("y", "integer", "Virtual-desktop Y pixel coordinate.", required=True),
            ToolParam("button", "string", "Mouse button.", default="left",
                      enum=("left", "right", "middle")),
            ToolParam("clicks", "integer", "1 = single click, 2 = double click.", default=1),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {x, y, button, clicks, events_sent}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        unsupported = _unsupported()
        if unsupported:
            return unsupported
        try:
            try:
                x = _win.int_arg(kwargs, "x")
                y = _win.int_arg(kwargs, "y")
                clicks = _win.int_arg(kwargs, "clicks", 1)
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            if x is None or y is None:
                return ToolResult(ok=False, error="x and y are required")
            if clicks not in (1, 2):
                return ToolResult(ok=False, error="clicks must be 1 or 2")
            button = str(kwargs.get("button") or "left").lower()
            if button not in _BUTTONS:
                return ToolResult(ok=False, error="button must be left, right or middle")
            backend = _backend()
            screen = await asyncio.to_thread(backend.virtual_screen)
            vx, vy, vw, vh = screen
            if not (vx <= x < vx + vw and vy <= y < vy + vh):
                return ToolResult(ok=False, error=(f"({x}, {y}) is outside the virtual desktop "
                                                   f"x[{vx}, {vx + vw}) y[{vy}, {vy + vh})"))
            events = click_inputs(x, y, screen, button, clicks)
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target={"x": x, "y": y, "virtual_screen": list(screen)},
                    operation="input.click",
                    effect=f"move the pointer to ({x}, {y}) and {button}-click {clicks}x",
                    reversible=False, rollback=_NO_ROLLBACK)
            sent = await asyncio.to_thread(backend.send, events)
            ok = sent == len(events)
            return ToolResult(
                ok=ok,
                data={"x": x, "y": y, "button": button, "clicks": clicks, "events_sent": sent},
                error=None if ok else f"Windows inserted {sent}/{len(events)} input events",
                evidence={"method": "SendInput", "events_sent": sent},
                certainty="unverified",
            )
        except Exception as exc:
            log.warning("input.click failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unknown")

    def verify(self, ctx: ToolContext, result: ToolResult):
        """Cursor read-back proves the pointer arrived, not what the click did."""
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        try:
            cx, cy = _backend().cursor_pos()
        except Exception as exc:
            return {"verified": False, "method": "GetCursorPos", "note": str(exc)}
        at = abs(cx - result.data["x"]) <= 1 and abs(cy - result.data["y"]) <= 1
        return {"verified": False, "method": "GetCursorPos", "cursor": [cx, cy],
                "cursor_at_target": at,
                "note": "pointer position confirmed only; the click's effect is unobservable"}
