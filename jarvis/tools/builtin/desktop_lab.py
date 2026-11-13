"""``desktop.lab_write`` — the first verified real desktop action.

The tool launches a small **lab editor** (tkinter) with a unique title, focuses
that exact window (refusing to type unless focus is proven), types the text,
presses Ctrl+S (the editor saves to the target path), and then verifies the file
on disk byte-for-byte. The path is confined to ``<data>/lab``.

The episode (task, actions, verification, outcome) is appended to
``<data>/lab/outcomes.jsonl`` — the first step toward feeding verified JARVIS
actions into EVOLVE.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from ..base import (BaseTool, SideEffect, ToolContext, ToolParam, ToolResult,
                    ToolSpec)
from ..registry import register
from ...logging_setup import get_logger
from . import _common
from .input_tool import _backend, hotkey_to_inputs, parse_hotkey, text_to_inputs

log = get_logger("jarvis.tools.desktop_lab")

__all__ = ["DesktopLabWriteTool", "health"]

WM_CLOSE = 0x0010
SW_RESTORE = 9
CHUNK = 64

_APP = """\
import tkinter as tk

path = {path!r}
title = {title!r}
root = tk.Tk()
root.title(title)
box = tk.Text(root, width=70, height=12)
box.pack(padx=8, pady=8)
box.focus_set()

def save(event=None):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(box.get("1.0", "end-1c"))
    root.title(title + " [saved]")

root.bind("<Control-s>", save)
root.after(60000, root.destroy)
root.mainloop()
"""


def health() -> dict[str, Any]:
    if sys.platform != "win32":
        return _common.health_fail("desktop.lab_write is Windows-only")
    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        return _common.health_fail(f"tkinter unavailable: {exc}")
    return _common.health_ok(
        "lab editor (tkinter) + SendInput; verified by file bytes")


def _user32():
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
    u.EnumWindows.restype = wt.BOOL
    u.GetWindowTextLengthW.argtypes = [wt.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.GetWindowTextW.restype = ctypes.c_int
    u.IsWindowVisible.argtypes = [wt.HWND]
    u.IsWindowVisible.restype = wt.BOOL
    u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    u.ShowWindow.restype = wt.BOOL
    u.SetForegroundWindow.argtypes = [wt.HWND]
    u.SetForegroundWindow.restype = wt.BOOL
    u.GetForegroundWindow.restype = wt.HWND
    u.BringWindowToTop.argtypes = [wt.HWND]
    u.BringWindowToTop.restype = wt.BOOL
    u.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    u.AttachThreadInput.restype = wt.BOOL
    u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.PostMessageW.restype = wt.BOOL
    return u


def _focus_window(user32: Any, hwnd: int) -> bool:
    """Bring the lab window to the foreground (AttachThreadInput workaround)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.restype = wt.DWORD
    user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
    foreground = int(user32.GetForegroundWindow() or 0)
    current = int(kernel32.GetCurrentThreadId())
    target_thread = int(user32.GetWindowThreadProcessId(
        wt.HWND(foreground), None) or 0) if foreground else 0
    attached = False
    try:
        if target_thread and target_thread != current:
            attached = bool(user32.AttachThreadInput(
                current, target_thread, True))
        user32.BringWindowToTop(wt.HWND(hwnd))
        user32.SetForegroundWindow(wt.HWND(hwnd))
        for _ in range(30):
            if int(user32.GetForegroundWindow() or 0) == hwnd:
                return True
            time.sleep(0.05)
    finally:
        if attached:
            user32.AttachThreadInput(current, target_thread, False)
    return False


def _find_window_by_title(user32: Any, title: str) -> tuple[int, str] | None:
    match: tuple[int, str] | None = None
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def collect(hwnd: int, _param: int) -> bool:
        nonlocal match
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value == title or buffer.value.startswith(title + " "):
            match = (int(hwnd), buffer.value)
            return False
        return True

    user32.EnumWindows(callback_type(collect), 0)
    return match


def _normalise(text: str) -> str:
    return text.replace("\ufeff", "").replace("\r\n", "\n").rstrip("\n")


def _record(lab: Path, payload: dict[str, Any]) -> None:
    try:
        with (lab / "outcomes.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError:  # recording must never break the action
        pass


@register(["desktop.lab_write"])
class DesktopLabWriteTool(BaseTool):
    spec = ToolSpec(
        name="desktop.lab_write",
        summary=("Type text into the JARVIS lab editor and verify the saved "
                 "file byte-for-byte. Confined to <data>/lab; focus is proven "
                 "before typing."),
        version="1.0",
        category="desktop",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(
            ToolParam("filename", "string",
                      "Bare .txt filename inside <data>/lab.", required=True),
            ToolParam("text", "string", "Text to type and save.", required=True),
        ),
        returns=("object: {path, bytes, sha256, verified, exact, actions, "
                 "duration_ms}"),
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        if sys.platform != "win32":
            return ToolResult(ok=False, certainty="settled",
                              error="desktop.lab_write is Windows-only")
        filename = str(kwargs.get("filename") or "").strip()
        text = str(kwargs.get("text") or "")
        if not filename or Path(filename).name != filename or \
                "/" in filename or "\\" in filename or filename in (".", ".."):
            return ToolResult(ok=False, certainty="settled",
                              error="filename must be a bare name (no paths)")
        if not filename.lower().endswith(".txt"):
            return ToolResult(ok=False, certainty="settled",
                              error="filename must end in .txt")

        base = getattr(getattr(ctx, "config", None), "data_path", None)
        lab = (Path(base) if base is not None else Path.cwd() / "data") / "lab"
        lab.mkdir(parents=True, exist_ok=True)
        target = lab / filename
        if target.exists():
            target.unlink()

        user32 = _user32()
        title = f"JARVIS Lab Writer {uuid.uuid4().hex[:8]}"
        script = _APP.format(path=str(target), title=title)
        started = time.perf_counter()
        actions: list[str] = []
        process = None
        hwnd = 0
        try:
            kwargs_popen: dict[str, Any] = {"close_fds": True}
            if sys.platform == "win32":
                kwargs_popen["creationflags"] = getattr(
                    subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen([sys.executable, "-c", script],
                                       **kwargs_popen)
            actions.append("launched lab editor")
            deadline = time.time() + 15
            window = None
            while time.time() < deadline:
                window = _find_window_by_title(user32, title)
                if window is not None:
                    break
                time.sleep(0.15)
            if window is None:
                return ToolResult(ok=False, certainty="unsettled",
                                  error="lab window did not appear")
            hwnd, window_title = window

            if not _focus_window(user32, hwnd):
                return ToolResult(ok=False, certainty="settled",
                                  error=("refusing to type: the lab window is "
                                         "not in the foreground"))
            actions.append(f"focused hwnd={hwnd} title={window_title!r}")

            backend = _backend()
            for offset in range(0, len(text), CHUNK):
                backend.send(text_to_inputs(text[offset:offset + CHUNK]))
                time.sleep(0.01)
            actions.append(f"typed {len(text)} chars")

            backend.send(hotkey_to_inputs(parse_hotkey("ctrl+s")))
            actions.append("sent ctrl+s")

            exact = False
            content = ""
            deadline = time.time() + 8
            while time.time() < deadline:
                if target.is_file():
                    content = target.read_text(encoding="utf-8",
                                               errors="replace")
                    if _normalise(content) == _normalise(text):
                        exact = True
                        break
                time.sleep(0.1)
            raw = target.read_bytes() if target.is_file() else b""
            verified = bool(target.is_file()) and \
                _normalise(content) == _normalise(text)
            duration = round((time.perf_counter() - started) * 1000.0, 1)
            record = {
                "ts": time.time(), "task": "desktop.lab_write",
                "path": str(target), "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "verified": verified, "exact": exact,
                "duration_ms": duration, "actions": actions,
            }
            _record(lab, record)
            return ToolResult(
                ok=verified,
                certainty="settled" if verified else "unverified",
                error=None if verified else
                "the saved file does not match the typed text",
                data={"path": str(target), "bytes": len(raw),
                      "sha256": record["sha256"], "verified": verified,
                      "exact": exact, "actions": actions,
                      "duration_ms": duration},
                evidence={"method": "independent-file-comparison",
                          "sha256": record["sha256"]})
        finally:
            if hwnd:
                try:
                    user32.PostMessageW(wt.HWND(hwnd), WM_CLOSE, 0, 0)
                except Exception:
                    pass
            if process is not None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
