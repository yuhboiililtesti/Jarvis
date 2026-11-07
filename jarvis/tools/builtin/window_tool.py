"""Desktop window tools: ``window.list`` / ``focus`` / ``minimize`` / ``maximize`` / ``close``.

Windows-only, implemented with stdlib ``ctypes`` against user32 (EnumWindows,
SetForegroundWindow, ShowWindow, PostMessageW). On other platforms every tool
returns an honest "unsupported on this platform" error.

Targeting: a call names a window either by ``hwnd`` (exact handle from
``window.list``) or by ``title`` (case-insensitive substring of a visible
top-level window title), optionally narrowed by ``process`` (exact image name,
e.g. ``notepad.exe``). A title that matches more than one window is refused with
the candidate list, never guessed.

Every action has an independent read-back in ``verify``:
  focus    -> GetForegroundWindow() == hwnd
  minimize -> IsIconic(hwnd)
  maximize -> IsZoomed(hwnd)
  close    -> IsWindow(hwnd) becomes false (polled; an app may legitimately
              keep the window open to ask "save changes?", which is reported
              as unverified, not as success)

The desktop/shell windows (Progman, WorkerW, Shell_TrayWnd, ...) are refused:
sending WM_CLOSE to Progman opens the Windows shutdown dialog.
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

log = get_logger("jarvis.tools.window")

__all__ = [
    "WindowListTool", "WindowFocusTool", "WindowMinimizeTool", "WindowMaximizeTool",
    "WindowCloseTool", "WinWindowBackend", "PROTECTED_CLASSES", "set_backend", "health",
]

SW_MAXIMIZE = 3
SW_MINIMIZE = 6
SW_RESTORE = 9
WM_CLOSE = 0x0010

PROTECTED_CLASSES = frozenset({
    "progman", "workerw", "shell_traywnd", "shell_secondarytraywnd",
    "windows.ui.core.corewindow", "#32769",
})


class WinWindowBackend:
    """Thin ctypes wrapper. Every method is synchronous and may raise OSError."""

    def __init__(self) -> None:
        if not _win.IS_WINDOWS:
            raise OSError(_win.UNSUPPORTED)
        u = ctypes.WinDLL("user32", use_last_error=True)
        self._enum_proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = (self._enum_proc_t, wintypes.LPARAM)
        u.EnumWindows.restype = wintypes.BOOL
        for fn in ("IsWindowVisible", "IsIconic", "IsZoomed", "IsWindow",
                   "SetForegroundWindow", "BringWindowToTop"):
            getattr(u, fn).argtypes = (wintypes.HWND,)
            getattr(u, fn).restype = wintypes.BOOL
        u.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetForegroundWindow.argtypes = ()
        u.GetForegroundWindow.restype = wintypes.HWND
        u.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        u.ShowWindow.restype = wintypes.BOOL
        u.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                   wintypes.LPARAM)
        u.PostMessageW.restype = wintypes.BOOL
        u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        u.GetWindowRect.restype = wintypes.BOOL
        u.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
        u.AttachThreadInput.restype = wintypes.BOOL
        self.u = u
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.GetCurrentThreadId.restype = wintypes.DWORD

    # -- reads -----------------------------------------------------------
    def _title(self, hwnd: int) -> str:
        n = self.u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        self.u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _class(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        self.u.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _pid(self, hwnd: int) -> int:
        pid = wintypes.DWORD(0)
        self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def info(self, hwnd: int) -> dict[str, Any] | None:
        if not hwnd or not self.u.IsWindow(hwnd):
            return None
        rect = wintypes.RECT()
        has_rect = bool(self.u.GetWindowRect(hwnd, ctypes.byref(rect)))
        pid = self._pid(hwnd)
        return {
            "hwnd": int(hwnd),
            "title": self._title(hwnd),
            "class": self._class(hwnd),
            "pid": pid,
            "process": _process_name(pid),
            "visible": bool(self.u.IsWindowVisible(hwnd)),
            "minimized": bool(self.u.IsIconic(hwnd)),
            "maximized": bool(self.u.IsZoomed(hwnd)),
            "foreground": int(self.u.GetForegroundWindow() or 0) == int(hwnd),
            "rect": ([rect.left, rect.top, rect.right, rect.bottom] if has_rect else None),
        }

    def list_windows(self) -> list[dict[str, Any]]:
        handles: list[int] = []

        def _cb(hwnd, _lparam):
            if self.u.IsWindowVisible(hwnd) and self.u.GetWindowTextLengthW(hwnd) > 0:
                handles.append(int(hwnd))
            return True

        cb = self._enum_proc_t(_cb)
        if not self.u.EnumWindows(cb, 0):
            raise OSError(f"EnumWindows failed (winerror {ctypes.get_last_error()})")
        out = []
        for h in handles:
            item = self.info(h)
            if item is not None:
                out.append(item)
        return out

    def foreground(self) -> int:
        return int(self.u.GetForegroundWindow() or 0)

    def is_window(self, hwnd: int) -> bool:
        return bool(self.u.IsWindow(hwnd))

    def is_minimized(self, hwnd: int) -> bool:
        return bool(self.u.IsIconic(hwnd))

    def is_maximized(self, hwnd: int) -> bool:
        return bool(self.u.IsZoomed(hwnd))

    # -- effects ---------------------------------------------------------
    def focus(self, hwnd: int) -> bool:
        if self.u.IsIconic(hwnd):
            self.u.ShowWindow(hwnd, SW_RESTORE)
        if self.u.SetForegroundWindow(hwnd):
            return True
        # Foreground lock: borrow the foreground thread's input state briefly.
        fg = self.u.GetForegroundWindow()
        fg_thread = self.u.GetWindowThreadProcessId(fg, None) if fg else 0
        me = self.k32.GetCurrentThreadId()
        attached = bool(fg_thread and fg_thread != me
                        and self.u.AttachThreadInput(me, fg_thread, True))
        try:
            self.u.BringWindowToTop(hwnd)
            ok = bool(self.u.SetForegroundWindow(hwnd))
        finally:
            if attached:
                self.u.AttachThreadInput(me, fg_thread, False)
        return ok

    def show(self, hwnd: int, cmd: int) -> bool:
        # ShowWindow returns the *previous* visibility, not success; callers
        # rely on verify() for the real outcome.
        self.u.ShowWindow(hwnd, cmd)
        return True

    def close(self, hwnd: int) -> bool:
        return bool(self.u.PostMessageW(hwnd, WM_CLOSE, 0, 0))


def _process_name(pid: int) -> str | None:
    if not pid:
        return None
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return None


_BACKEND: Any = None


def set_backend(backend: Any) -> None:
    """Inject a backend (tests use a fake; None restores lazy real backend)."""
    global _BACKEND
    _BACKEND = backend


def _backend() -> Any:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = WinWindowBackend()
    return _BACKEND


def health() -> dict[str, Any]:
    """Cheap availability check: Windows (user32) or an injected backend."""
    if _BACKEND is not None:
        return _common.health_ok("backend injected")
    if not _win.IS_WINDOWS:
        return _common.health_fail(_win.UNSUPPORTED)
    return _common.health_ok("user32 via ctypes")


# ---------------------------------------------------------------------------
# targeting
# ---------------------------------------------------------------------------

_TARGET_PARAMS = (
    ToolParam("hwnd", "integer", "Exact window handle from window.list."),
    ToolParam("title", "string",
              "Case-insensitive substring of the window title (must match exactly one)."),
    ToolParam("process", "string", "Optional exact process image name filter, e.g. notepad.exe."),
    _common.DRY_RUN_PARAM,
)


def _resolve(backend: Any, kwargs: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return (window_info, error)."""
    try:
        hwnd = _win.int_arg(kwargs, "hwnd")
    except ValueError as exc:
        return None, str(exc)
    title = str(kwargs.get("title") or "").strip()
    proc = str(kwargs.get("process") or "").strip().lower()
    if hwnd is None and not title:
        return None, "give either hwnd or title to choose a window"
    if hwnd is not None:
        info = backend.info(hwnd)
        if info is None:
            return None, f"no window with hwnd {hwnd}"
        if proc and (info.get("process") or "").lower() != proc:
            return None, f"window {hwnd} belongs to {info.get('process')!r}, not {proc!r}"
        return info, None
    needle = title.lower()
    matches = [w for w in backend.list_windows()
               if needle in (w.get("title") or "").lower()
               and (not proc or (w.get("process") or "").lower() == proc)]
    if not matches:
        return None, f"no visible window title contains {title!r}"
    if len(matches) > 1:
        cands = "; ".join(f"{w['hwnd']}: {w['title'][:60]!r} ({w.get('process')})"
                          for w in matches[:10])
        return None, (f"{len(matches)} windows match {title!r}; pass hwnd to choose one: "
                      f"{cands}")
    return matches[0], None


def _protected(info: dict[str, Any]) -> str | None:
    if (info.get("class") or "").lower() in PROTECTED_CLASSES:
        return (f"refused: window {info['hwnd']} is a protected shell/desktop window "
                f"(class {info.get('class')!r})")
    return None


class _WindowAction(BaseTool):
    """Shared run(): resolve -> guard -> act. Subclasses set `_act` and `_check`."""

    action = ""
    effect = ""
    reversible: bool | str = True
    rollback = ""

    def _act(self, backend: Any, hwnd: int) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        unavailable = _common.unavailable_result(health())
        if unavailable:
            return unavailable
        try:
            backend = _backend()
            info, err = await asyncio.to_thread(_resolve, backend, kwargs)
            if err:
                return ToolResult(ok=False, error=err)
            assert info is not None
            guard = _protected(info)
            if guard:
                return ToolResult(ok=False, error=guard, evidence={"refused": True})
            hwnd = int(info["hwnd"])
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target={"hwnd": hwnd, "title": info.get("title"),
                            "process": info.get("process"), "pid": info.get("pid")},
                    operation=f"window.{self.action}", effect=self.effect,
                    reversible=self.reversible, rollback=self.rollback,
                    current_state={k: info.get(k) for k in
                                   ("minimized", "maximized", "foreground")})
            accepted = await asyncio.to_thread(self._act, backend, hwnd)
            if not accepted:
                return ToolResult(ok=False, error=f"Windows rejected {self.action} for {hwnd}",
                                  data={"hwnd": hwnd, "title": info.get("title")},
                                  certainty="unverified")
            return ToolResult(
                ok=True,
                data={"hwnd": hwnd, "title": info.get("title"),
                      "process": info.get("process"), "action": self.action},
                evidence={"method": "user32", "action": self.action, "hwnd": hwnd},
                certainty="unverified",
            )
        except Exception as exc:  # never raise out of run()
            log.warning("%s failed: %s", self.spec.name, exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unknown")


@register(["windows.list", "window_list"])
class WindowListTool(BaseTool):
    spec = ToolSpec(
        name="window.list",
        summary="List visible top-level desktop windows (handle, title, process, state).",
        category="desktop",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("title", "string", "Optional case-insensitive title substring filter."),
            ToolParam("process", "string", "Optional exact process image name filter."),
        ),
        returns="object: {count, foreground, windows:[{hwnd,title,class,pid,process,...}]}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        unavailable = _common.unavailable_result(health())
        if unavailable:
            return unavailable
        try:
            backend = _backend()
            windows = await asyncio.to_thread(backend.list_windows)
            needle = str(kwargs.get("title") or "").strip().lower()
            proc = str(kwargs.get("process") or "").strip().lower()
            if needle:
                windows = [w for w in windows if needle in (w.get("title") or "").lower()]
            if proc:
                windows = [w for w in windows if (w.get("process") or "").lower() == proc]
            fg = next((w["hwnd"] for w in windows if w.get("foreground")), None)
            return ToolResult(ok=True, data={"count": len(windows), "foreground": fg,
                                             "windows": windows})
        except Exception as exc:
            log.warning("window.list failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["window_focus", "window.activate"])
class WindowFocusTool(_WindowAction):
    action = "focus"
    effect = "the window becomes the foreground window (restored first if minimised)"
    rollback = "focus the previously foreground window again with window.focus"
    spec = ToolSpec(
        name="window.focus",
        summary="Bring a window to the foreground (restoring it if minimised).",
        category="desktop",
        side_effect=SideEffect.LOCAL_EXEC,
        params=_TARGET_PARAMS,
        returns="object: {hwnd, title, process, action}",
    )

    def _act(self, backend: Any, hwnd: int) -> bool:
        backend.focus(hwnd)
        return True  # SetForegroundWindow may refuse; verify() reports the truth

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        hwnd = int(result.data["hwnd"])
        fg = await _poll(lambda: _backend().foreground() == hwnd)
        return {"verified": fg, "method": "GetForegroundWindow", "hwnd": hwnd,
                "foreground": _backend().foreground()}


@register(["window_minimize", "window.minimise"])
class WindowMinimizeTool(_WindowAction):
    action = "minimize"
    effect = "the window is minimised to the taskbar"
    rollback = "window.focus restores it"
    spec = ToolSpec(
        name="window.minimize",
        summary="Minimise a window.",
        category="desktop",
        side_effect=SideEffect.LOCAL_EXEC,
        params=_TARGET_PARAMS,
        returns="object: {hwnd, title, process, action}",
    )

    def _act(self, backend: Any, hwnd: int) -> bool:
        return backend.show(hwnd, SW_MINIMIZE)

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        hwnd = int(result.data["hwnd"])
        ok = await _poll(lambda: _backend().is_minimized(hwnd))
        return {"verified": ok, "method": "IsIconic", "hwnd": hwnd, "minimized": ok}


@register(["window_maximize", "window.maximise"])
class WindowMaximizeTool(_WindowAction):
    action = "maximize"
    effect = "the window is maximised to fill its monitor"
    rollback = "restore it from the window's title bar or with window.focus after minimising"
    spec = ToolSpec(
        name="window.maximize",
        summary="Maximise a window.",
        category="desktop",
        side_effect=SideEffect.LOCAL_EXEC,
        params=_TARGET_PARAMS,
        returns="object: {hwnd, title, process, action}",
    )

    def _act(self, backend: Any, hwnd: int) -> bool:
        return backend.show(hwnd, SW_MAXIMIZE)

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        hwnd = int(result.data["hwnd"])
        ok = await _poll(lambda: _backend().is_maximized(hwnd))
        return {"verified": ok, "method": "IsZoomed", "hwnd": hwnd, "maximized": ok}


@register(["window_close"])
class WindowCloseTool(_WindowAction):
    action = "close"
    effect = ("WM_CLOSE is posted; the app closes the window, or asks to save first; "
              "apps that do not prompt discard unsaved state")
    reversible = False
    rollback = "none; reopen the application (unsaved work in it may be lost)"
    spec = ToolSpec(
        name="window.close",
        summary=("Ask a window to close (WM_CLOSE). The app may prompt to save; "
                 "unsaved work in apps that do not prompt can be lost."),
        category="desktop",
        # Classed DESTRUCTIVE: a game or terminal closes without asking, so
        # this can lose state; autonomous mode must never do it unprompted.
        side_effect=SideEffect.DESTRUCTIVE,
        params=_TARGET_PARAMS,
        returns="object: {hwnd, title, process, action}",
        cheap=False,
    )

    def _act(self, backend: Any, hwnd: int) -> bool:
        return backend.close(hwnd)

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        hwnd = int(result.data["hwnd"])
        gone = await _poll(lambda: not _backend().is_window(hwnd), timeout=3.0)
        out = {"verified": gone, "method": "IsWindow", "hwnd": hwnd, "exists": not gone}
        if not gone:
            out["note"] = "window still open (the app may be asking to save changes)"
        return out


async def _poll(check, timeout: float = 1.5, interval: float = 0.1) -> bool:
    """Poll a synchronous read-back until true or timeout. Never raises."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            if await asyncio.to_thread(check):
                return True
        except Exception as exc:
            log.debug("poll check raised: %s", exc)
            return False
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)
