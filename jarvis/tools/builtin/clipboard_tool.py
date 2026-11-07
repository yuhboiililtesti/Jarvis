"""``clipboard.get`` / ``clipboard.set`` - clipboard access without extra deps.

Windows uses PowerShell's ``Get-Clipboard`` / ``Set-Clipboard``. On POSIX we use
``wl-paste``/``wl-copy`` under Wayland and fall back to ``xclip``; if none is
present we do not pretend to have succeeded - we say exactly what is missing.

``clipboard.set`` verifies itself by reading the value back and comparing it,
so the effect is independently confirmed rather than merely asserted.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _win

log = get_logger("jarvis.tools.clipboard")

__all__ = ["ClipboardGetTool", "ClipboardSetTool"]

_TIMEOUT_S = 10

_MISSING_POSIX_MSG = (
    "no clipboard backend found: install 'wl-clipboard' (Wayland) or 'xclip' (X11); "
    "nothing was read or written"
)


def _ps_argv(script: str) -> list[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def _read_clipboard() -> tuple[bool, str, str | None]:
    """Return (ok, text, error). Never raises. Runs through the sandbox."""
    if os.name == "nt":
        argv = _ps_argv("Get-Clipboard -Raw")
    elif shutil.which("wl-paste"):
        argv = ["wl-paste", "--no-newline"]
    elif shutil.which("xclip"):
        argv = ["xclip", "-selection", "clipboard", "-o"]
    else:
        return False, "", _MISSING_POSIX_MSG
    res = _win.run_cmd(argv, timeout=_TIMEOUT_S)
    if not res["ok"]:
        detail = (res["stderr"] or "").strip() or res["error"] or "clipboard read failed"
        return False, "", detail
    return True, res["stdout"] or "", None


def _write_clipboard(text: str) -> tuple[bool, str | None]:
    """Return (ok, error). Never raises. Runs through the sandbox."""
    if os.name == "nt":
        argv = _ps_argv("$t=[Console]::In.ReadToEnd(); Set-Clipboard -Value $t")
    elif shutil.which("wl-copy"):
        argv = ["wl-copy"]
    elif shutil.which("xclip"):
        argv = ["xclip", "-selection", "clipboard", "-i"]
    else:
        return False, _MISSING_POSIX_MSG
    res = _win.run_cmd(argv, timeout=_TIMEOUT_S, input_text=text)
    if not res["ok"]:
        return False, ((res["stderr"] or "").strip() or res["error"]
                       or "clipboard write failed")
    return True, None


def _backend() -> str:
    if os.name == "nt":
        return "powershell"
    if shutil.which("wl-paste") or shutil.which("wl-copy"):
        return "wl-clipboard"
    if shutil.which("xclip"):
        return "xclip"
    return "none"


@register(["clipboard.get", "clip.get"])
class ClipboardGetTool(BaseTool):
    spec = ToolSpec(
        name="clipboard.get",
        summary="Read the current clipboard text.",
        version="1.0",
        category="system",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object: {text, length, backend}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            ok, text, err = await asyncio.to_thread(_read_clipboard)
            if not ok:
                return ToolResult(ok=False, error=err or "clipboard read failed")
            return ToolResult(
                ok=True,
                data={"text": text, "length": len(text), "backend": _backend()},
                evidence={"method": "backend", "backend": _backend()},
            )
        except Exception as exc:  # never raise out of run()
            log.exception("clipboard.get failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["clipboard.set", "clip.set"])
class ClipboardSetTool(BaseTool):
    spec = ToolSpec(
        name="clipboard.set",
        summary="Replace the clipboard contents with the given text.",
        version="1.0",
        category="system",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("text", "string", "Text to place on the clipboard.", required=True),
        ),
        returns="object: {length, backend, written}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            text = kwargs.get("text")
            if text is None:
                return ToolResult(ok=False, error="text is required")
            text = str(text)

            ok, err = await asyncio.to_thread(_write_clipboard, text)
            if not ok:
                return ToolResult(ok=False, error=err or "clipboard write failed",
                                  certainty="unverified")
            return ToolResult(
                ok=True,
                data={"length": len(text), "backend": _backend(), "written": True,
                      "text": text,
                      "duration_ms": round((time.perf_counter() - t0) * 1000.0, 1)},
                evidence={"method": "backend", "backend": _backend()},
                certainty="settled",
            )
        except Exception as exc:  # never raise out of run()
            log.exception("clipboard.set failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")

    async def verify(self, ctx: ToolContext, result: ToolResult):
        """Read the clipboard back and compare (trailing newlines normalised)."""
        if not result.ok or not isinstance(result.data, dict):
            return None
        want = result.data.get("text")
        if want is None:
            return None
        ok, got, _err = await asyncio.to_thread(_read_clipboard)
        if not ok:
            return None
        matched = got.rstrip("\r\n") == str(want).rstrip("\r\n")
        return {"verified": matched, "method": "read-back", "matched": matched}
