"""``process.kill`` - terminate a process by pid or exact image name (DESTRUCTIVE).

Guards that hold regardless of policy:
  * protected system processes are refused by name (csrss, lsass, winlogon,
    svchost, explorer, systemd, sshd, ...) and by pid (0, 4, 1);
  * the JARVIS process itself and every ancestor of it are refused (killing the
    parent shell/service would take JARVIS down with it);
  * a name that matches several processes is refused unless ``all=true``, and
    even then at most :data:`MAX_MATCHES` processes are touched;
  * one attempt only: ``terminate`` (or ``kill`` with ``force=true``), then
    wait. A process that survives is reported as still running; the tool never
    escalates or retries on its own (the owner decides whether to force).

Read-back: ``verify`` checks each target pid is gone *and* that a pid still
present is not the same process (create_time comparison guards pid reuse).

``process.start`` is intentionally absent: ``apps.launch`` (allowlisted) covers it.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common, _win

log = get_logger("jarvis.tools.process")

__all__ = ["ProcessKillTool", "PsutilProcessBackend", "PROTECTED_NAMES", "PROTECTED_PIDS",
           "MAX_MATCHES", "set_backend", "health"]

MAX_MATCHES = 20
PROTECTED_PIDS = frozenset({0, 1, 4})
PROTECTED_NAMES = frozenset({
    # Windows
    "system", "system idle process", "registry", "memory compression", "secure system",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "lsaiso.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe", "sihost.exe",
    "logonui.exe", "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe", "taskhostw.exe",
    "ctfmon.exe", "spoolsv.exe", "wudfhost.exe", "audiodg.exe",
    # Linux
    "systemd", "init", "kthreadd", "sshd", "dbus-daemon", "dbus-broker", "systemd-logind",
    "systemd-journald", "systemd-udevd", "xorg", "xwayland", "gdm", "sddm", "lightdm",
})


class PsutilProcessBackend:
    def __init__(self) -> None:
        import psutil  # core dependency, but keep the import local
        self.ps = psutil

    def self_and_ancestors(self) -> set[int]:
        me = self.ps.Process(os.getpid())
        out = {me.pid}
        try:
            out.update(p.pid for p in me.parents())
        except Exception:  # pragma: no cover - platform quirk
            pass
        return out

    def info(self, pid: int) -> dict[str, Any] | None:
        try:
            p = self.ps.Process(pid)
            with p.oneshot():
                return {"pid": p.pid, "name": p.name(), "create_time": p.create_time(),
                        "username": _safe(p.username), "exe": _safe(p.exe)}
        except self.ps.NoSuchProcess:
            return None

    def by_name(self, image: str) -> list[dict[str, Any]]:
        want = image.lower()
        out = []
        for p in self.ps.process_iter(["pid", "name", "create_time", "username"]):
            if (p.info.get("name") or "").lower() == want:
                out.append({"pid": p.info["pid"], "name": p.info["name"],
                            "create_time": p.info.get("create_time"),
                            "username": p.info.get("username")})
        return out

    def stop(self, targets: list[dict[str, Any]], force: bool, timeout: float
             ) -> tuple[list[int], list[int], dict[int, str]]:
        procs, errors = [], {}
        for t in targets:
            try:
                p = self.ps.Process(t["pid"])
                if p.create_time() != t["create_time"]:
                    errors[t["pid"]] = "pid was reused by another process; skipped"
                    continue
                (p.kill if force else p.terminate)()
                procs.append(p)
            except self.ps.NoSuchProcess:
                errors[t["pid"]] = "already exited"
            except self.ps.AccessDenied:
                errors[t["pid"]] = "access denied (needs elevation or belongs to another user)"
        gone, alive = self.ps.wait_procs(procs, timeout=timeout)
        return [p.pid for p in gone], [p.pid for p in alive], errors

    def alive(self, pid: int, create_time: float | None) -> bool:
        try:
            p = self.ps.Process(pid)
            return create_time is None or p.create_time() == create_time
        except self.ps.NoSuchProcess:
            return False


def _safe(fn) -> Any:
    try:
        return fn()
    except Exception:
        return None


_BACKEND: Any = None


def set_backend(backend: Any) -> None:
    global _BACKEND
    _BACKEND = backend


def _backend() -> Any:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = PsutilProcessBackend()
    return _BACKEND


def health() -> dict[str, Any]:
    if _BACKEND is not None:
        return _common.health_ok("backend injected")
    try:
        import psutil  # noqa: F401
    except ImportError:
        return _common.health_fail("psutil is not installed (pip install psutil)")
    return _common.health_ok("psutil")


def _refusal(t: dict[str, Any], protected_pids: set[int]) -> str | None:
    if t["pid"] in PROTECTED_PIDS:
        return f"pid {t['pid']} is a protected system process"
    if (t.get("name") or "").lower() in PROTECTED_NAMES:
        return f"{t.get('name')} (pid {t['pid']}) is a protected system process"
    if t["pid"] in protected_pids:
        return f"pid {t['pid']} ({t.get('name')}) is JARVIS itself or one of its parents"
    return None


@register(["kill", "process_kill", "taskkill"])
class ProcessKillTool(BaseTool):
    spec = ToolSpec(
        name="process.kill",
        summary="Terminate a process by pid or exact image name (refuses system/JARVIS processes).",
        category="system",
        side_effect=SideEffect.DESTRUCTIVE,
        params=(
            ToolParam("pid", "integer", "Process id to terminate."),
            ToolParam("process", "string", "Exact image name, e.g. notepad.exe (case-insensitive)."),
            ToolParam("all", "boolean",
                      f"Allow terminating every match of `process` (max {MAX_MATCHES}).",
                      default=False),
            ToolParam("force", "boolean",
                      "Hard kill (SIGKILL) instead of terminate; same call on Windows.",
                      default=False),
            ToolParam("timeout_s", "number", "Seconds to wait for exit (1-30).", default=5),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {targets, terminated, still_running, errors}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _common.unavailable_result(health())
        if gate:
            return gate
        try:
            try:
                pid = _win.int_arg(kwargs, "pid")
                allow_all = bool(_win.bool_arg(kwargs, "all", False))
                force = bool(_win.bool_arg(kwargs, "force", False))
            except ValueError as exc:
                return ToolResult(ok=False, error=str(exc))
            try:
                timeout = max(1.0, min(float(kwargs.get("timeout_s") or 5), 30.0))
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="timeout_s must be a number")
            image = str(kwargs.get("process") or "").strip()
            if (pid is None) == (not image):
                return ToolResult(ok=False, error="give exactly one of pid or process")
            if image and (os.sep in image or "/" in image):
                return ToolResult(ok=False, error="process must be an image name, not a path")

            backend = _backend()
            if pid is not None:
                info = await asyncio.to_thread(backend.info, pid)
                if info is None:
                    return ToolResult(ok=False, error=f"no process with pid {pid}")
                targets = [info]
            else:
                targets = await asyncio.to_thread(backend.by_name, image)
                if not targets:
                    return ToolResult(ok=False, error=f"no running process named {image!r}")
                if len(targets) > 1 and not allow_all:
                    pids = ", ".join(str(t["pid"]) for t in targets[:MAX_MATCHES])
                    return ToolResult(ok=False, error=(
                        f"{len(targets)} processes are named {image!r} (pids {pids}); pass a pid, "
                        "or all=true to terminate every one"))
                if len(targets) > MAX_MATCHES:
                    return ToolResult(ok=False, error=(f"{len(targets)} matches exceeds the cap of "
                                                       f"{MAX_MATCHES}; refusing"))

            protected = await asyncio.to_thread(backend.self_and_ancestors)
            for t in targets:
                why = _refusal(t, protected)
                if why:
                    return ToolResult(ok=False, error=f"refused: {why}",
                                      evidence={"refused": True, "pid": t["pid"]})

            summary = [{k: t.get(k) for k in ("pid", "name", "username")} for t in targets]
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target=summary, operation="process.kill",
                    effect=(f"{'kill' if force else 'terminate'} {len(targets)} process(es); "
                            "unsaved data in them is lost"),
                    reversible=False,
                    rollback="none; restart the program (e.g. apps.launch)")

            gone, alive, errors = await asyncio.to_thread(backend.stop, targets, force, timeout)
            ok = bool(gone) and not alive and not errors
            err = None
            if not ok:
                parts = []
                if alive:
                    parts.append(f"still running after {timeout:g}s: {alive} "
                                 "(not retried; call again with force=true if intended)")
                if errors:
                    parts.append("; ".join(f"pid {k}: {v}" for k, v in errors.items()))
                err = "; ".join(parts) or "nothing was terminated"
            return ToolResult(
                ok=ok,
                data={"targets": [dict(s, create_time=t.get("create_time"))
                                  for s, t in zip(summary, targets, strict=True)],
                      "terminated": gone, "still_running": alive,
                      "errors": {str(k): v for k, v in errors.items()}, "force": force},
                error=err,
                evidence={"method": "psutil", "terminated": gone},
                certainty="unverified",
            )
        except Exception as exc:
            log.warning("process.kill failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        backend = _backend()
        survivors = []
        for t in result.data.get("targets", []):
            if await asyncio.to_thread(backend.alive, t["pid"], t.get("create_time")):
                survivors.append(t["pid"])
        return {"verified": not survivors, "method": "pid+create_time-readback",
                "still_running": survivors}
