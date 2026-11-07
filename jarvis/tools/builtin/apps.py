"""``apps.launch`` / ``apps.list`` - controlled application launching.

Launching is gated by an allowlist stored in the ``app_allowlist`` kv entry: a
mapping of friendly name -> executable command or absolute path. Unknown names
are refused, so the model can never conjure a program that the owner has not
pre-vetted. ``apps.list`` exposes exactly that map so the UI can show what is
reachable.

**The one deliberate sandbox exception.** An application the owner asked to
launch is *meant* to outlive the tool call, so it is started detached
(``subprocess.Popen`` / ``os.startfile``) and is **not** placed in the process
job — putting it in a job would kill the app the moment the tool returned. This
is the only tool that starts a process outside the sandbox; every other tool
routes through ``jarvis.sandbox`` (see `docs/SANDBOX.md`). The launched program
is still limited to the owner-approved allowlist, which is what bounds it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from ...sandbox.env import sanitized_env

log = get_logger("jarvis.tools.apps")

__all__ = ["AppsLaunchTool", "AppsListTool", "ALLOWLIST_KEY", "DEFAULT_ALLOWLIST",
           "catalog"]

ALLOWLIST_KEY = "app_allowlist"

# A small, reviewable default so a fresh install can actually open common apps.
# The kv entry, when present, replaces this map entirely (owner control).
DEFAULT_ALLOWLIST: dict[str, str] = {
    "steam": r"C:\Program Files (x86)\Steam\steam.exe",
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "settings": "ms-settings:",
    "task manager": "taskmgr.exe",
} if os.name == "nt" else {
    "files": "xdg-open",
}


def _installed(target: str) -> bool:
    """Whether a configured target can actually be resolved on this machine."""
    text = str(target or "").strip()
    if not text:
        return False
    path = Path(text)
    if path.is_absolute() or "/" in text or "\\" in text:
        return path.is_file()
    if re.match(r"^[a-z][a-z0-9+.-]*:", text, re.IGNORECASE):
        return True                      # a URI handler (ms-settings:, mailto:)
    return shutil.which(text) is not None


def _entry(value: Any) -> dict[str, Any] | None:
    """Normalise an allowlist value.

    A plain string is the historical form and means "launch with no arguments".
    An owner who genuinely needs arguments can opt in explicitly::

        [app_allowlist]
        vlc = { target = "C:\\...\\vlc.exe", allow_args = true }

    Arguments are denied by default because an allowlisted shell-capable binary
    (``explorer.exe`` in the default list) can execute an arbitrary file when
    handed one, which would turn the list into a general escape hatch.
    """
    if isinstance(value, dict):
        target = str(value.get("target") or "").strip()
        allow_args = bool(value.get("allow_args"))
    else:
        target = str(value or "").strip()
        allow_args = False
    if not target:
        return None
    return {"target": target, "allow_args": allow_args}


def _allowlist(ctx: ToolContext) -> dict[str, dict[str, Any]]:
    db = getattr(ctx, "db", None)
    if db is None:
        return {}
    raw = db.kv_get(ALLOWLIST_KEY, None)
    if raw is None:
        # No owner override: use the reviewable defaults.
        raw = DEFAULT_ALLOWLIST
    if not isinstance(raw, dict):
        log.warning("app_allowlist is not a mapping; ignoring")
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        entry = _entry(value)
        if entry is not None:
            out[str(name)] = entry
    return out


def _extra_args(raw: Any) -> list[str] | None:
    """Coerce the optional args parameter; None means malformed input."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(a) for a in raw]
    return None


def catalog(ctx: ToolContext) -> dict[str, dict[str, Any]]:
    """The allowlisted apps and whether each target resolves on this machine."""
    return {name: {"target": entry["target"],
                   "installed": _installed(entry["target"]),
                   "allow_args": entry["allow_args"]}
            for name, entry in _allowlist(ctx).items()}


@register(["apps.list"])
class AppsListTool(BaseTool):
    spec = ToolSpec(
        name="apps.list",
        summary="List the applications JARVIS is allowed to launch, and whether each is installed.",
        version="1.0",
        category="system",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object: {apps: {name: target}, installed: {name: bool}, count}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            entries = catalog(ctx)
            return ToolResult(
                ok=True,
                data={"apps": {n: e["target"] for n, e in entries.items()},
                      "installed": {n: e["installed"] for n, e in entries.items()},
                      "allow_args": {n: e["allow_args"] for n, e in entries.items()},
                      "count": len(entries)},
                evidence={"method": "kv", "key": ALLOWLIST_KEY},
            )
        except Exception as exc:  # never raise out of run()
            log.exception("apps.list failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["apps.launch", "app.launch"])
class AppsLaunchTool(BaseTool):
    spec = ToolSpec(
        name="apps.launch",
        summary="Launch an installed desktop application by name (e.g. 'steam'). Use this to open an app; use web.open for a URL.",
        version="1.0",
        category="system",
        side_effect=SideEffect.LOCAL_EXEC,
        params=(
            # NOTE: the parameter is `app`, not `name`, because
            # ToolRegistry.call(name, ctx, **kwargs) already binds `name`; a
            # kwarg called `name` would raise "multiple values for argument".
            ToolParam("app", "string", "Friendly app name from the allowlist.", required=True),
            ToolParam("args", "array", "Optional extra arguments (POSIX / subprocess path)."),
        ),
        returns="object: {name, target, launched, pid?}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            name = str(kwargs.get("app") or "").strip()
            if not name:
                return ToolResult(ok=False, error="app is required")

            allow = _allowlist(ctx)
            if name not in allow:
                return ToolResult(
                    ok=False,
                    error=(f"app '{name}' is not in the allowlist "
                           f"({len(allow)} configured); add it to '{ALLOWLIST_KEY}' first"),
                )
            entry = allow[name]
            target = entry["target"]
            if not _installed(target):
                return ToolResult(
                    ok=False,
                    error=f"app '{name}' is allowlisted but not installed at '{target}'",
                    certainty="settled")

            extra = _extra_args(kwargs.get("args"))
            if extra is None:
                return ToolResult(ok=False, error="args must be a list of strings")
            if extra and not entry["allow_args"]:
                return ToolResult(
                    ok=False,
                    error=(f"app '{name}' does not accept extra arguments; "
                           f"set allow_args = true for that entry in '{ALLOWLIST_KEY}' "
                           f"if the owner intends to pass arguments"),
                    certainty="settled")

            # The app is not sandboxed (it must outlive the call) but it must not
            # inherit JARVIS secrets either. ``os.startfile`` uses ShellExecute and
            # takes no environment, so that path cannot be scrubbed — documented.
            env = sanitized_env()
            if os.name == "nt":
                if extra:
                    proc = subprocess.Popen([target, *extra], env=env)
                    pid = proc.pid
                else:
                    starter = getattr(os, "startfile", None)
                    if starter is None:  # pragma: no cover - non-Windows guard
                        return ToolResult(ok=False, error="os.startfile unavailable on this platform")
                    starter(target)
                    pid = None
            else:
                proc = subprocess.Popen(
                    [target, *extra],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                pid = proc.pid

            log.info("launched app %s -> %s", name, target)
            return ToolResult(
                ok=True,
                data={"name": name, "target": target, "launched": True, "pid": pid,
                      "duration_ms": round((time.perf_counter() - t0) * 1000.0, 1)},
                evidence={"method": "spawn", "name": name, "target": target},
                certainty="settled",
            )
        except Exception as exc:  # never raise out of run()
            log.exception("apps.launch failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")

    def preflight(self, ctx: ToolContext, **kwargs: Any) -> ToolResult | None:
        """Refuse an impossible or widened launch *before* asking for approval.

        Returns an error without acting (missing app, uninstalled target, or
        arguments the owner has not opted into); ``None`` lets the normal
        policy/approval path continue. This never performs the effect.
        """
        name = str(kwargs.get("app") or "").strip()
        if not name:
            return ToolResult(ok=False, error="app is required", certainty="settled")
        allow = _allowlist(ctx)
        if name not in allow:
            available = ", ".join(sorted(allow)) or "none"
            return ToolResult(
                ok=False,
                error=(f"app '{name}' is not in the allowlist; available: {available}. "
                       f"Add it to '{ALLOWLIST_KEY}' first."),
                certainty="settled")
        entry = allow[name]
        target = entry["target"]
        if not _installed(target):
            return ToolResult(
                ok=False,
                error=(f"app '{name}' is allowlisted but not installed at '{target}'; "
                       "nothing was launched"),
                certainty="settled")
        extra = _extra_args(kwargs.get("args"))
        if extra is None:
            return ToolResult(ok=False, error="args must be a list of strings",
                              certainty="settled")
        if extra and not entry["allow_args"]:
            return ToolResult(
                ok=False,
                error=(f"app '{name}' does not accept extra arguments; nothing was launched"),
                certainty="settled")
        return None

    def verify(self, ctx: ToolContext, result: ToolResult):
        """No reliable cross-platform read-back of 'an app window is up'."""
        return None
