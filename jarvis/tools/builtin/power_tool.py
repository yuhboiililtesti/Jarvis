"""Power tools: ``power.lock`` / ``power.sleep`` / ``power.shutdown`` / ``power.restart`` /
``power.cancel``.

Safety model:
  * ``sleep``, ``shutdown`` and ``restart`` are DESTRUCTIVE: they interrupt
    everything running (games, downloads, unsaved work), so policy always asks
    and autonomous mode never runs them unprompted.
  * ``shutdown`` / ``restart`` always carry a cancellable delay
    (:data:`DEFAULT_DELAY_S`, never below :data:`MIN_DELAY_S`); ``power.cancel``
    (or ``shutdown /a`` on Windows, ``shutdown -c`` on Linux) aborts it.
    On Windows a delay > 0 implies ``/f``: applications are force-closed when
    the timer expires. The preview says so.
  * No power action is ever retried automatically: one OS call, then an honest
    result. A failure is reported, not repeated.

Read-backs: ``lock`` polls for the lock screen (LogonUI.exe on Windows,
``LockedHint`` via loginctl on Linux). Sleep/shutdown/restart cannot be read
back by the process they stop, and Windows offers no query for a pending
shutdown timer, so their ``verify`` returns ``None`` (unverified) and the
evidence is the OS command's exit status.
"""

from __future__ import annotations

import asyncio
import ctypes
import math
import os
import re
import shutil
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common, _win

log = get_logger("jarvis.tools.power")

__all__ = [
    "PowerLockTool", "PowerSleepTool", "PowerShutdownTool", "PowerRestartTool",
    "PowerCancelTool", "SystemPowerBackend", "DEFAULT_DELAY_S", "MIN_DELAY_S", "MAX_DELAY_S",
    "set_backend", "health",
]

DEFAULT_DELAY_S = 60
MIN_DELAY_S = 30
MAX_DELAY_S = 86400
_NO_PENDING_WIN = 1116          # ERROR_NO_SHUTDOWN_IN_PROGRESS
_ALREADY_PENDING_WIN = 1190     # ERROR_SHUTDOWN_IS_SCHEDULED


class SystemPowerBackend:
    """Real OS calls. Each method performs exactly one attempt."""

    def lock(self) -> dict[str, Any]:
        if _win.IS_WINDOWS:
            ok = bool(ctypes.windll.user32.LockWorkStation())
            return {"ok": ok, "code": 0 if ok else ctypes.GetLastError(), "method": "LockWorkStation"}
        res = _win.run_cmd(["loginctl", "lock-session"], timeout=10)
        return {"ok": res["ok"], "code": res["code"], "method": "loginctl lock-session",
                "error": res["error"] or res["stderr"].strip()}

    def is_locked(self) -> bool | None:
        if _win.IS_WINDOWS:
            import psutil
            return any((p.info.get("name") or "").lower() == "logonui.exe"
                       for p in psutil.process_iter(["name"]))
        sid = os.environ.get("XDG_SESSION_ID")
        if not sid or not shutil.which("loginctl"):
            return None
        res = _win.run_cmd(["loginctl", "show-session", sid, "-p", "LockedHint"], timeout=5)
        if not res["ok"]:
            return None
        return "lockedhint=yes" in res["stdout"].strip().lower()

    def sleep(self) -> dict[str, Any]:
        if _win.IS_WINDOWS:
            ok = bool(ctypes.windll.powrprof.SetSuspendState(0, 0, 0))
            return {"ok": ok, "code": 0 if ok else ctypes.GetLastError(),
                    "method": "SetSuspendState"}
        res = _win.run_cmd(["systemctl", "suspend"], timeout=15)
        return {"ok": res["ok"], "code": res["code"], "method": "systemctl suspend",
                "error": res["error"] or res["stderr"].strip()}

    def schedule(self, action: str, delay_s: int, message: str) -> dict[str, Any]:
        if _win.IS_WINDOWS:
            flag = "/s" if action == "shutdown" else "/r"
            argv = ["shutdown.exe", flag, "/t", str(delay_s), "/c", message]
        else:
            minutes = max(1, math.ceil(delay_s / 60))
            argv = ["shutdown", "-h" if action == "shutdown" else "-r", f"+{minutes}", message]
        res = _win.run_cmd(argv, timeout=15)
        return {"ok": res["ok"], "code": res["code"], "argv": argv,
                "error": res["error"] or res["stderr"].strip() or None}

    def cancel(self) -> dict[str, Any]:
        argv = ["shutdown.exe", "/a"] if _win.IS_WINDOWS else ["shutdown", "-c"]
        res = _win.run_cmd(argv, timeout=15)
        return {"ok": res["ok"], "code": res["code"], "argv": argv,
                "error": res["error"] or res["stderr"].strip() or None}


_BACKEND: Any = None


def set_backend(backend: Any) -> None:
    global _BACKEND
    _BACKEND = backend


def _backend() -> Any:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = SystemPowerBackend()
    return _BACKEND


def health() -> dict[str, Any]:
    if _BACKEND is not None:
        return _common.health_ok("backend injected")
    if _win.IS_WINDOWS:
        return _common.health_ok("user32/powrprof + shutdown.exe")
    if os.name == "posix" and shutil.which("shutdown"):
        return _common.health_ok("systemd/loginctl + shutdown",
                                 loginctl=bool(shutil.which("loginctl")))
    return _common.health_fail("no supported power interface on this platform")


def _cancel_hint() -> str:
    return ("power.cancel (or run 'shutdown /a' on Windows, 'shutdown -c' on Linux) "
            "before the timer expires")


_SAFE_MSG = re.compile(r"[^A-Za-z0-9 .,:;!?()'_-]+")


def _message(kwargs: dict[str, Any], action: str, delay: int) -> str:
    raw = str(kwargs.get("reason") or "").strip()
    text = _SAFE_MSG.sub(" ", raw)[:120].strip()
    cancel = "shutdown /a" if _win.IS_WINDOWS else "shutdown -c"
    base = f"JARVIS: {action} in {delay}s. Cancel with '{cancel}'."
    return f"{base} {text}".strip() if text else base


async def _poll(check, timeout: float = 3.0, interval: float = 0.25) -> bool | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: bool | None = None
    while True:
        try:
            last = await asyncio.to_thread(check)
        except Exception as exc:
            log.debug("lock poll failed: %s", exc)
            return None
        if last:
            return True
        if last is None or loop.time() >= deadline:
            return last
        await asyncio.sleep(interval)


@register(["lock", "lock_screen"])
class PowerLockTool(BaseTool):
    spec = ToolSpec(
        name="power.lock",
        summary="Lock the workstation (the session keeps running behind the lock screen).",
        category="power",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(_common.DRY_RUN_PARAM,),
        returns="object: {locked_requested, method}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _common.unavailable_result(health())
        if gate:
            return gate
        if _common.dry_run_requested(kwargs):
            return _common.preview(
                target="current interactive session", operation="power.lock",
                effect="the lock screen appears; running programs keep running",
                reversible=True, rollback="the owner signs back in")
        try:
            res = await asyncio.to_thread(lambda: _backend().lock())
            if not res.get("ok"):
                return ToolResult(ok=False, error=(f"lock failed via {res.get('method')} "
                                                   f"(code {res.get('code')}) {res.get('error') or ''}"
                                                   ).strip(), certainty="settled")
            return ToolResult(ok=True, data={"locked_requested": True, "method": res.get("method")},
                              evidence={"method": res.get("method")}, certainty="unverified")
        except Exception as exc:
            log.warning("power.lock failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok:
            return None
        locked = await _poll(lambda: _backend().is_locked())
        if locked is None:
            return None
        return {"verified": bool(locked), "method": "lock-screen-readback", "locked": locked}


@register(["suspend", "power.suspend"])
class PowerSleepTool(BaseTool):
    spec = ToolSpec(
        name="power.sleep",
        summary="Put the computer to sleep now (interrupts everything running).",
        category="power",
        side_effect=SideEffect.DESTRUCTIVE,
        params=(_common.DRY_RUN_PARAM,),
        returns="object: {sleep_requested, method}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _common.unavailable_result(health())
        if gate:
            return gate
        if _common.dry_run_requested(kwargs):
            return _common.preview(
                target="this computer", operation="power.sleep",
                effect=("the machine suspends immediately; games, calls, downloads and network "
                        "sessions are interrupted (memory state is kept)"),
                reversible="partially", rollback="wake the machine; interrupted sessions may not resume")
        try:
            res = await asyncio.to_thread(lambda: _backend().sleep())
            if not res.get("ok"):
                return ToolResult(ok=False, error=(f"sleep failed via {res.get('method')} "
                                                   f"(code {res.get('code')}) {res.get('error') or ''}"
                                                   ).strip(), certainty="settled")
            return ToolResult(ok=True, data={"sleep_requested": True, "method": res.get("method")},
                              evidence={"method": res.get("method")}, certainty="unverified")
        except Exception as exc:
            log.warning("power.sleep failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        return None  # the process that would read it back is asleep


class _ScheduledPower(BaseTool):
    action = ""

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _common.unavailable_result(health())
        if gate:
            return gate
        try:
            try:
                delay = _win.int_arg(kwargs, "delay_s", DEFAULT_DELAY_S)
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            if delay is None or not MIN_DELAY_S <= delay <= MAX_DELAY_S:
                return ToolResult(ok=False, error=(f"delay_s must be between {MIN_DELAY_S} and "
                                                   f"{MAX_DELAY_S} seconds so it stays cancellable"))
            message = _message(kwargs, self.action, delay)
            if _common.dry_run_requested(kwargs):
                effect = (f"{self.action} this computer in {delay}s"
                          + ("; when the timer expires Windows force-closes open applications "
                             "(unsaved work is lost)" if _win.IS_WINDOWS else
                             f" (Linux rounds up to {max(1, math.ceil(delay / 60))} min)"))
                return _common.preview(
                    target="this computer", operation=f"power.{self.action}",
                    effect=effect, reversible=f"yes, until the {delay}s timer expires",
                    rollback=_cancel_hint(), delay_s=delay, message=message)
            res = await asyncio.to_thread(lambda: _backend().schedule(self.action, delay, message))
            if not res.get("ok"):
                code = res.get("code")
                note = (" (a shutdown is already scheduled; power.cancel it first)"
                        if code == _ALREADY_PENDING_WIN else "")
                return ToolResult(ok=False, error=(f"{self.action} scheduling failed (code {code})"
                                                   f"{note}: {res.get('error') or ''}").strip(),
                                  data={"argv": res.get("argv")}, certainty="settled")
            return ToolResult(
                ok=True,
                data={"action": self.action, "delay_s": delay, "scheduled": True,
                      "cancel_with": _cancel_hint()},
                evidence={"method": "exit-status", "exit_code": res.get("code"),
                          "argv": res.get("argv")},
                certainty="unverified",
            )
        except Exception as exc:
            log.warning("power.%s failed: %s", self.action, exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        return None  # no OS query exists for a pending shutdown timer


def _sched_params() -> tuple[ToolParam, ...]:
    return (
        ToolParam("delay_s", "integer",
                  f"Seconds before it happens ({MIN_DELAY_S}-{MAX_DELAY_S}, default "
                  f"{DEFAULT_DELAY_S}); cancellable with power.cancel.",
                  default=DEFAULT_DELAY_S),
        ToolParam("reason", "string", "Short reason shown in the OS warning."),
        _common.DRY_RUN_PARAM,
    )


@register(["shutdown", "power.off"])
class PowerShutdownTool(_ScheduledPower):
    action = "shutdown"
    spec = ToolSpec(
        name="power.shutdown",
        summary="Schedule a shutdown after a cancellable delay (default 60 s).",
        category="power",
        side_effect=SideEffect.DESTRUCTIVE,
        params=_sched_params(),
        returns="object: {action, delay_s, scheduled, cancel_with}",
        cheap=False,
    )


@register(["restart", "reboot"])
class PowerRestartTool(_ScheduledPower):
    action = "restart"
    spec = ToolSpec(
        name="power.restart",
        summary="Schedule a restart after a cancellable delay (default 60 s).",
        category="power",
        side_effect=SideEffect.DESTRUCTIVE,
        params=_sched_params(),
        returns="object: {action, delay_s, scheduled, cancel_with}",
        cheap=False,
    )


@register(["abort_shutdown", "power.abort"])
class PowerCancelTool(BaseTool):
    spec = ToolSpec(
        name="power.cancel",
        summary="Cancel a pending scheduled shutdown or restart.",
        category="power",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(_common.DRY_RUN_PARAM,),
        returns="object: {cancelled}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _common.unavailable_result(health())
        if gate:
            return gate
        if _common.dry_run_requested(kwargs):
            return _common.preview(
                target="pending shutdown/restart timer", operation="power.cancel",
                effect="abort the pending shutdown or restart, if any", reversible=True,
                rollback="schedule it again with power.shutdown / power.restart")
        try:
            res = await asyncio.to_thread(lambda: _backend().cancel())
            if not res.get("ok"):
                if res.get("code") == _NO_PENDING_WIN:
                    return ToolResult(ok=False, error="no shutdown or restart was pending",
                                      data={"cancelled": False}, certainty="settled")
                return ToolResult(ok=False, error=(f"cancel failed (code {res.get('code')}): "
                                                   f"{res.get('error') or ''}").strip(),
                                  certainty="settled")
            return ToolResult(ok=True, data={"cancelled": True},
                              evidence={"method": "exit-status", "exit_code": res.get("code"),
                                        "argv": res.get("argv")},
                              certainty="settled")
        except Exception as exc:
            log.warning("power.cancel failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        return None  # the abort's exit status is the only evidence available
