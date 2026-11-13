"""Workflow #3 â€” move a file in the real Windows File Explorer.

Runtime-varied: every run creates new random folders/filename, moves the window
to a random position, and drives Explorer with real keyboard input. Verification
is independent (filesystem), not Explorer's own feedback.

Run:  python scripts/explorer_workflow.py
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import random
import string
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.tools.builtin.input_tool import (  # noqa: E402
    _backend, hotkey_to_inputs, parse_hotkey, text_to_inputs)

LAB = ROOT / "data" / "lab" / "explorer"
WM_CLOSE = 0x0010
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010


def user32():
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
    u.EnumWindows.restype = wt.BOOL
    u.GetWindowTextLengthW.argtypes = [wt.HWND]
    u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.IsWindowVisible.argtypes = [wt.HWND]
    u.IsWindowVisible.restype = wt.BOOL
    u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    u.SetForegroundWindow.argtypes = [wt.HWND]
    u.GetForegroundWindow.restype = wt.HWND
    u.BringWindowToTop.argtypes = [wt.HWND]
    u.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wt.UINT]
    u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    return u


def windows(u):
    rows = []
    cb = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def collect(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, title, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        rows.append({"hwnd": int(hwnd), "title": title.value,
                     "class": cls.value})
        return True

    u.EnumWindows(cb(collect), 0)
    return rows


def focus(u, hwnd):
    k32 = ctypes.WinDLL("kernel32")
    u.ShowWindow(wt.HWND(hwnd), 9)
    fg = int(u.GetForegroundWindow() or 0)
    me = int(k32.GetCurrentThreadId())
    other = int(u.GetWindowThreadProcessId(wt.HWND(fg), None) or 0) if fg else 0
    attached = False
    try:
        if other and other != me:
            attached = bool(u.AttachThreadInput(me, other, True))
        # The ALT press unlocks SetForegroundWindow for a background process.
        u.keybd_event(0x12, 0, 0, 0)
        u.BringWindowToTop(wt.HWND(hwnd))
        u.SetForegroundWindow(wt.HWND(hwnd))
        u.keybd_event(0x12, 0, 2, 0)
        try:
            u.SwitchToThisWindow(wt.HWND(hwnd), True)
        except Exception:
            pass
        for _ in range(30):
            if int(u.GetForegroundWindow() or 0) == hwnd:
                return True
            time.sleep(0.05)
    finally:
        if attached:
            u.AttachThreadInput(me, other, False)
    return False


def click_at(u, hwnd, dx=300, dy=300):
    box = wt.RECT()
    u.GetWindowRect(wt.HWND(hwnd), ctypes.byref(box))
    u.SetCursorPos(box.left + dx, box.top + dy)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    u.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.3)

def send(text: str, delay: float = 0.35) -> None:
    _backend().send(text_to_inputs(text))
    time.sleep(delay)


def key(combo: str, delay: float = 0.6) -> None:
    _backend().send(hotkey_to_inputs(parse_hotkey(combo)))
    time.sleep(delay)


def wait_for(predicate, timeout=8.0, interval=0.15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def main() -> int:
    random.seed()
    tag = "".join(random.choice(string.ascii_lowercase) for _ in range(6))
    src = LAB / f"src_{tag}"
    dst = LAB / f"dst_{tag}"
    src.mkdir(parents=True, exist_ok=True)
    dst.mkdir(parents=True, exist_ok=True)
    name = f"move_{tag}.txt"
    content = f"explorer workflow {uuid.uuid4().hex}\n"
    (src / name).write_text(content, encoding="utf-8")

    u = user32()
    started = time.perf_counter()
    steps = []
    hwnd = None
    try:
        subprocess.Popen(["explorer.exe", str(src)], close_fds=True)
        steps.append("launched explorer")
        window = wait_for(lambda: next(
            (w for w in windows(u)
             if w["class"] == "CabinetWClass" and tag in w["title"]), None), 15)
        if not window:
            # Explorer may show a generic title; accept the first CabinetWClass.
            window = wait_for(lambda: next(
                (w for w in windows(u) if w["class"] == "CabinetWClass"), None), 5)
        if not window:
            return fail(steps, "no Explorer window appeared")
        hwnd = window["hwnd"]
        # Runtime variation: move the window somewhere random.
        u.SetWindowPos(wt.HWND(hwnd), wt.HWND(0), random.randint(80, 400),
                       random.randint(80, 300), 1100, 700,
                       SWP_NOZORDER | SWP_NOACTIVATE)
        if not focus(u, hwnd):
            return fail(steps, "could not focus Explorer")
        steps.append(f"focused hwnd={hwnd} title={window['title']!r}")

        key("ctrl+l")
        send(str(src))
        key("return", 1.5)
        click_at(u, hwnd)
        key("ctrl+a")
        key("ctrl+x")
        key("ctrl+l")
        send(str(dst))
        key("return", 1.5)
        click_at(u, hwnd)
        key("ctrl+v", 1.5)

        moved = wait_for(lambda: (dst / name).is_file() and
                         not (src / name).exists(), 8)
        verified = bool(moved) and (dst / name).read_text(
            encoding="utf-8") == content
        duration = round((time.perf_counter() - started) * 1000, 1)
        record = {"workflow": "explorer_move", "tag": tag,
                  "src": str(src), "dst": str(dst), "file": name,
                  "verified": verified, "duration_ms": duration,
                  "steps": steps,
                  "sha256": hashlib.sha256(
                      (dst / name).read_bytes()).hexdigest()
                  if (dst / name).is_file() else None}
        (LAB / "results.jsonl").open("a", encoding="utf-8").write(
            json.dumps(record) + "\n")
        print(json.dumps(record, indent=2))
        return 0 if verified else 1
    finally:
        if hwnd:
            try:
                u.PostMessageW(wt.HWND(hwnd), WM_CLOSE, 0, 0)
            except Exception:
                pass


def fail(steps, reason):
    print(json.dumps({"verified": False, "reason": reason, "steps": steps}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())