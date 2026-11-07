"""JARVIS **satellite agent** -- the code that runs ON the ThinkPad.

This is the Node half of the link. It is deliberately tiny, dependency-light and
easy to audit: it exposes exactly two HTTP endpoints (``/node/health`` and
``/node/task``) and does nothing the Core could not already do itself. It grants
**no authority the Core did not already have**: every request must carry the
shared secret in the ``X-Jarvis-Node-Token`` header, and the Core only sends
work it has already decided (and, where policy requires, had approved) to send.

Design constraints (the ThinkPad is a 2-core Core 2 Duo with ~3.7 GB RAM):

  * stdlib first; FastAPI/uvicorn are used only if already present.
  * no numpy/torch/heavy frameworks.
  * a stdlib ``http.server`` fallback keeps the node usable on a bare install.

Security notes:

  * the token is compared with :func:`hmac.compare_digest`;
  * the token is never logged;
  * ``exec`` refuses a small denylist of irreversible commands (defence in
    depth, shared with the Core via :mod:`jarvis.nodes.protocol`);
  * ``files.read`` is confined to explicitly allowed roots.

This module is what ``python -m jarvis.nodes`` runs on the satellite.
"""

from __future__ import annotations

import base64
import asyncio
import hmac
import json
import os
import platform
import shutil
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ..logging_setup import get_logger
from ..version import VERSION
from .protocol import (
    DEFAULT_NODE_PORT,
    HEADER,
    HEALTH_PATH,
    KIND_EXEC,
    KIND_FILES_READ,
    KIND_PING,
    KIND_SYSINFO,
    KIND_TOOL,
    KIND_VOICE_SPEAK,
    KIND_VOICE_PLAY,
    KIND_POWER,
    KIND_METRICS,
    SUPPORTED_KINDS,
    TASK_PATH,
    ErrorCode,
    error_response,
    is_destructive_command,
    ok_response,
)

# FastAPI is optional (the stdlib http.server fallback runs without it), but the
# names must live at MODULE scope. This module uses `from __future__ import
# annotations`, so `request: Request` is a string that FastAPI resolves against
# module globals; if the import were local to the builder, FastAPI could not
# resolve it and would treat `request` as a required QUERY parameter (HTTP 422).
try:
    from fastapi import FastAPI, Request
except Exception:  # pragma: no cover - exercised only without FastAPI
    FastAPI = None  # type: ignore[assignment,misc]
    Request = None  # type: ignore[assignment,misc]

log = get_logger("jarvis.nodes.agent")

__all__ = [
    "NodeAgent",
    "build_fastapi_app",
    "build_http_server",
    "create_agent",
    "load_token",
    "run_agent",
]

#: Output is truncated so a runaway command cannot exhaust memory or the link.
MAX_OUTPUT_CHARS = 8000
MAX_READ_BYTES = 1_000_000
DEFAULT_EXEC_TIMEOUT_S = 30.0
MAX_EXEC_TIMEOUT_S = 300.0
#: Largest WAV the node will accept for local playback (base64 source, decoded).
MAX_PLAY_BYTES = 5_000_000

#: Controlled power verbs the node exposes via the ``power`` task kind.
_POWER_ACTIONS = ("lock", "sleep", "shutdown", "restart", "cancel")
_POWER_MIN_DELAY_S = 30
_POWER_MAX_DELAY_S = 86400

_TTS_CANDIDATES = ("spd-say", "espeak-ng", "espeak")
_PIPER_CANDIDATES = ("piper",)
#: Native WAV players tried in order on POSIX (PulseAudio/ALSA/SoX).
_PLAY_CANDIDATES = ("paplay", "aplay", "play")

# Local tools a host agent may execute on behalf of the Core. This is a
# deliberate allowlist: no shell, input injection, power, delete, or arbitrary
# exec. The Core still applies policy/approval before asking the node.
ALLOWED_HOST_TOOLS: frozenset[str] = frozenset({
    "apps.list", "apps.launch",
    "window.list", "window.focus", "window.minimize", "window.maximize",
    "screen.capture", "vision.screen", "vision.ocr",
    "system.info", "system.metrics", "system.processes",
    "files.read", "files.list", "files.search",
    "notes.list", "notes.read",
    "clipboard.get", "notify.toast",
    "media.volume_get", "media.volume_set", "media.mute",
})


# ---------------------------------------------------------------------------
# small platform helpers
# ---------------------------------------------------------------------------


def _load_avg() -> list[float]:
    try:
        return [round(float(x), 3) for x in os.getloadavg()]
    except (AttributeError, OSError):
        return [0.0, 0.0, 0.0]


def _memory_info() -> dict[str, Any]:
    """Parse /proc/meminfo (Linux); return MB values when available."""
    info: dict[str, Any] = {}
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info
    def kb(key: str) -> float | None:
        for line in text.splitlines():
            if line.startswith(key + ":"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return round(int(parts[1]) / 1024.0, 1)
        return None
    total = kb("MemTotal")
    avail = kb("MemAvailable")
    info["total_mb"] = total
    info["available_mb"] = avail
    if total and avail is not None:
        info["used_mb"] = round(total - avail, 1)
        info["used_pct"] = round((total - avail) / total * 100.0, 1)
    return info


def _uptime_s() -> float | None:
    try:
        raw = Path("/proc/uptime").read_text(encoding="ascii").split()[0]
        return round(float(raw), 1)
    except (OSError, ValueError, IndexError):
        return None


def _disk_info(path: str | None = None) -> dict[str, Any]:
    target = path or str(Path.home())
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return {}
    return {
        "path": target,
        "total_gb": round(usage.total / 1e9, 2),
        "used_gb": round(usage.used / 1e9, 2),
        "free_gb": round(usage.free / 1e9, 2),
        "used_pct": round(usage.used / usage.total * 100.0, 1) if usage.total else None,
    }


def _proc_cpu_percent(interval: float = 0.1) -> float | None:
    """CPU utilisation from /proc/stat (two samples). ``None`` when unavailable."""
    def read() -> tuple[int, int] | None:
        try:
            text = Path("/proc/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            if not line.startswith("cpu "):
                continue
            vals = [int(x) for x in line.split()[1:]]
            if len(vals) < 4:
                return None
            total = sum(vals)
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return total, idle
        return None

    first = read()
    if first is None:
        return None
    time.sleep(max(0.0, interval))
    second = read()
    if second is None:
        return None
    dt = second[0] - first[0]
    di = second[1] - first[1]
    if dt <= 0:
        return None
    return round((1.0 - di / dt) * 100.0, 1)


def _node_metrics_snapshot() -> dict[str, Any]:
    """Best-effort live metrics; a field the node cannot measure is absent/None.

    Prefers ``psutil`` when present (rich, cross-platform); on a bare Linux node
    it falls back to /proc + shutil. This is deliberately dependency-light so the
    node keeps running on a stdlib-only install.
    """
    out: dict[str, Any] = {"method": "partial"}
    try:
        import psutil  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        psutil = None

    if psutil is not None:
        out["method"] = "psutil"
        try:
            out["cpu_percent"] = float(psutil.cpu_percent(interval=0.1))
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            vm = psutil.virtual_memory()
            out["mem_percent"] = float(vm.percent)
            out["mem_used_bytes"] = int(vm.used)
            out["mem_total_bytes"] = int(vm.total)
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            out["load_1"] = float(psutil.getloadavg()[0])
        except Exception:  # pragma: no cover - no loadavg on some platforms
            pass
    else:
        load = _load_avg()
        if os.name == "posix" and load:
            out["load_1"] = load[0]
        mi = _memory_info()
        if mi.get("used_pct") is not None:
            out["mem_percent"] = mi["used_pct"]
            out["mem_used_bytes"] = int((mi.get("used_mb") or 0.0) * 1048576)
            out["mem_total_bytes"] = int((mi.get("total_mb") or 0.0) * 1048576)
        try:
            out["cpu_percent"] = _proc_cpu_percent()
        except Exception:  # pragma: no cover - defensive
            pass

    try:
        usage = shutil.disk_usage("/")
        out["disk_total_bytes"] = int(usage.total)
        out["disk_used_bytes"] = int(usage.used)
        out["disk_percent"] = round(usage.used / usage.total * 100.0, 1) if usage.total else None
    except OSError:  # pragma: no cover - defensive
        pass
    up = _uptime_s()
    if up is not None:
        out["uptime_s"] = up
    return out


def _which_first(candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None


def _short(exc: BaseException, limit: int = 300) -> str:
    return f"{type(exc).__name__}: {exc}"[:limit]


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _within_roots(target: Path, roots: list[Path]) -> bool:
    try:
        resolved = target.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            if resolved == root or resolved.is_relative_to(root):
                return True
        except (OSError, ValueError):
            continue
    return False


# ---------------------------------------------------------------------------
# token resolution
# ---------------------------------------------------------------------------


def load_token(cfg: Any) -> str:
    """Resolve the shared secret for this satellite.

    Order of precedence:

      1. ``NodesConfig.token`` (set by TOML or ``JARVIS_NODES__TOKEN``);
      2. the local ``kv`` row ``node_token`` if the Core's database is present;
      3. a ``0600`` file ``data/node_token``, generated once if absent.

    The secret is never logged.
    """
    configured = str(getattr(cfg.nodes, "token", "") or "").strip()
    if configured:
        return configured

    try:  # prefer the same store the Core uses when it is available locally
        from ..db import Database
        db = Database(cfg.db_path)
        existing = db.kv_get("node_token")
        if existing:
            db.close()
            return str(existing)
        from ..ids import new_id
        generated = new_id("nt")
        db.kv_set("node_token", generated)
        db.close()
        return generated
    except Exception as exc:  # pragma: no cover - bare node without the db
        log.debug("kv token lookup unavailable (%s); using token file", type(exc).__name__)

    from ..ids import new_id
    token_file = Path(cfg.data_path) / "node_token"
    try:
        if token_file.is_file():
            stored = token_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        token_file.parent.mkdir(parents=True, exist_ok=True)
        generated = new_id("nt")
        token_file.write_text(generated, encoding="utf-8")
        try:
            os.chmod(token_file, 0o600)
        except OSError:
            pass
        return generated
    except OSError as exc:
        log.warning("could not read or create token file: %s", exc)
        return new_id("nt")


# ---------------------------------------------------------------------------
# the agent
# ---------------------------------------------------------------------------


class NodeAgent:
    """Pure logic for the satellite; transport is added by the builders below."""

    def __init__(self, token: str, *, name: str | None = None,
                 capabilities: list[str] | None = None,
                 allowed_roots: list[str | os.PathLike[str]] | None = None,
                 config: Any | None = None) -> None:
        self._token = str(token or "")
        self.name = name or socket.gethostname()
        self.config = config
        self._started = time.time()
        roots = allowed_roots if allowed_roots is not None else self._default_roots()
        self.allowed_roots = [Path(r).expanduser().resolve() for r in roots]
        self.capabilities = (list(capabilities) if capabilities is not None
                             else self._detect_capabilities())
        from .capabilities import CapabilityManager, NodeCapability
        self.caps = CapabilityManager()
        self._handlers: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {
            KIND_EXEC: self._task_exec,
            KIND_SYSINFO: self._task_sysinfo,
            KIND_VOICE_SPEAK: self._task_voice_speak,
            KIND_VOICE_PLAY: self._task_voice_play,
            KIND_POWER: self._task_power,
            KIND_METRICS: self._task_metrics,
            KIND_FILES_READ: self._task_files_read,
            KIND_PING: self._task_ping,
            KIND_TOOL: self._task_tool,
        }

    # -- auth ------------------------------------------------------------
    def authorized(self, provided: str | None) -> bool:
        if not self._token or not provided:
            return False
        return hmac.compare_digest(str(provided), self._token)

    # -- introspection ---------------------------------------------------
    @staticmethod
    def _default_roots() -> list[str]:
        return [str(Path.home())]

    def _detect_capabilities(self) -> list[str]:
        caps = [KIND_EXEC, KIND_SYSINFO, KIND_FILES_READ, KIND_PING, KIND_TOOL,
                KIND_POWER, KIND_METRICS]
        if os.name == "nt" or _which_first(_TTS_CANDIDATES):
            caps.append(KIND_VOICE_SPEAK)
        # voice.play needs a native WAV player: SoundPlayer on Windows, or a
        # PulseAudio/ALSA/SoX player on POSIX.
        if os.name == "nt" or _which_first(_PLAY_CANDIDATES):
            caps.append(KIND_VOICE_PLAY)
        return caps

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "name": self.name,
            "role": "node",
            "capabilities": list(self.capabilities),
            "uptime_s": round(time.time() - self._started, 1),
            "load": _load_avg(),
            "version": VERSION,
        }

    # -- dispatch --------------------------------------------------------
    def dispatch(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            return error_response("request body must be a JSON object",
                                  code=ErrorCode.BAD_REQUEST)
        kind = str(body.get("kind") or "")
        task_id = str(body.get("task_id") or "")
        payload = body.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        # Capability check for dangerous operations
        try:
            from .capabilities import NodeCapability, CapabilityManager
            cap_map = {
                KIND_EXEC: NodeCapability.EXEC,
                KIND_POWER: NodeCapability.POWER,
            }
            if kind in cap_map:
                if not hasattr(self, "caps") or not self.caps.check(cap_map[kind]):
                    return error_response(f"capability '{cap_map[kind].value}' not granted",
                                          code=ErrorCode.DENIED, kind=kind, task_id=task_id)
        except Exception:
            pass

        handler = self._handlers.get(kind)
        if handler is None:
            resp = error_response(f"unsupported kind '{kind}'",
                                  code=ErrorCode.UNSUPPORTED_KIND,
                                  kind=kind, task_id=task_id)
            resp["supported"] = list(SUPPORTED_KINDS)
            return resp
        try:
            return handler(payload, task_id)
        except Exception as exc:  # a bad handler must not take the agent down
            return error_response(_short(exc), code=ErrorCode.INTERNAL,
                                  kind=kind, task_id=task_id)

    # -- task kinds ------------------------------------------------------
    def _task_ping(self, payload: dict[str, Any], task_id: str) -> dict[str, Any]:
        echo = payload.get("echo")
        data: dict[str, Any] = {"pong": True, "name": self.name,
                                "ts": round(time.time(), 3)}
        if echo is not None:
            data["echo"] = echo
        return ok_response(KIND_PING, task_id, **data)

    def _task_tool(self, payload: dict[str, Any], task_id: str) -> dict[str, Any]:
        tool_name = str(payload.get("tool") or "").strip()
        args = payload.get("args") or {}
        if tool_name not in ALLOWED_HOST_TOOLS:
            return error_response(
                f"tool '{tool_name}' is not in the host allowlist",
                code=ErrorCode.DENIED, kind=KIND_TOOL, task_id=task_id)
        if not isinstance(args, dict):
            return error_response("'args' must be an object",
                                  code=ErrorCode.BAD_REQUEST, kind=KIND_TOOL,
                                  task_id=task_id)
        if self.config is None:
            return error_response("node has no local config for tools",
                                  code=ErrorCode.UNAVAILABLE, kind=KIND_TOOL,
                                  task_id=task_id)
        try:
            from ..db import Database
            from ..tools import REGISTRY, load_builtin_tools
            from ..tools.base import ToolContext

            load_builtin_tools()
            tool = REGISTRY.get(tool_name)
            if tool is None:
                return error_response(f"unknown local tool '{tool_name}'",
                                      code=ErrorCode.NOT_FOUND, kind=KIND_TOOL,
                                      task_id=task_id)
            db = Database(self.config.db_path)
            try:
                ctx = ToolContext(config=self.config, db=db, bus=None,
                                  tool_name=tool_name, task_id=task_id,
                                  user="owner")
                result = asyncio.run(tool.run(ctx, **args))
            finally:
                db.close()
            data = result.data
            if result.ok:
                return ok_response(KIND_TOOL, task_id, tool=tool_name,
                                   data=data, brief=result.brief(),
                                   certainty=result.certainty)
            return error_response(result.error or "tool failed",
                                  code=ErrorCode.INTERNAL, kind=KIND_TOOL,
                                  task_id=task_id, tool=tool_name, data=data)
        except Exception as exc:  # a broken tool must not take the agent down
            log.warning("host tool %s failed: %s", tool_name, exc)
            return error_response(_short(exc), code=ErrorCode.INTERNAL,
                                  kind=KIND_TOOL, task_id=task_id,
                                  tool=tool_name)

    def _task_sysinfo(self, payload: dict[str, Any], task_id: str) -> dict[str, Any]:
        uname = platform.uname()
        uptime = _uptime_s()
        data = {
            "hostname": self.name,
            "uname": {
                "system": uname.system,
                "node": uname.node,
                "release": uname.release,
                "version": uname.version,
                "machine": uname.machine,
                "processor": uname.processor,
            },
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "load": _load_avg(),
            "memory": _memory_info(),
            "disk": _disk_info(),
            "uptime_s": uptime if uptime is not None else round(time.time() - self._started, 1),
            "agent_uptime_s": round(time.time() - self._started, 1),
        }
        return ok_response(KIND_SYSINFO, task_id, **data)

    def _task_exec(self, payload: dict[str, Any], task_id: str) -> dict[str, Any]:
        command = str(payload.get("command") or "").strip()
        if not command:
            return error_response("'command' is required", code=ErrorCode.BAD_REQUEST,
                                  kind=KIND_EXEC, task_id=task_id)

        # Extra capability check
        try:
            from .capabilities import NodeCapability
            if hasattr(self, "caps") and not self.caps.check(NodeCapability.EXEC):
                return error_response("exec capability not granted",
                                      code=ErrorCode.DENIED, kind=KIND_EXEC,
                                      task_id=task_id)
        except Exception:
            pass

        matched = is_destructive_command(command)
        if matched:
            log.warning("exec refused a destructive command (pattern matched)")
            return error_response(
                "command refused: matches the destructive-pattern denylist",
                code=ErrorCode.DENIED, kind=KIND_EXEC, task_id=task_id,
                pattern=matched)

        try:
            timeout_s = float(payload.get("timeout_s", DEFAULT_EXEC_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout_s = DEFAULT_EXEC_TIMEOUT_S
        timeout_s = max(0.1, min(timeout_s, MAX_EXEC_TIMEOUT_S))

        cwd_raw = payload.get("cwd")
        cwd: str | None = None
        if cwd_raw:
            candidate = Path(str(cwd_raw)).expanduser()
            if not _within_roots(candidate, self.allowed_roots):
                return error_response("'cwd' is outside the allowed roots",
                                      code=ErrorCode.DENIED, kind=KIND_EXEC,
                                      task_id=task_id)
            cwd = str(candidate)

        started = time.perf_counter()
        try:
            proc = subprocess.run(
                ["/bin/sh", "-lc", command],
                capture_output=True, text=True, timeout=timeout_s, cwd=cwd,
                env={**os.environ, "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8")},
            )
        except subprocess.TimeoutExpired as exc:
            out, trunc_out = _truncate(_decode(exc.stdout))
            err, trunc_err = _truncate(_decode(exc.stderr))
            return error_response(
                f"command timed out after {timeout_s:g}s",
                code=ErrorCode.TIMEOUT, kind=KIND_EXEC, task_id=task_id,
                returncode=None, stdout=out, stderr=err, timed_out=True,
                truncated=trunc_out or trunc_err)
        except OSError as exc:
            return error_response(_short(exc), code=ErrorCode.INTERNAL,
                                  kind=KIND_EXEC, task_id=task_id)

        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        out, trunc_out = _truncate(proc.stdout or "")
        err, trunc_err = _truncate(proc.stderr or "")
        data = {
            "returncode": proc.returncode,
            "stdout": out,
            "stderr": err,
            "truncated": trunc_out or trunc_err,
            "duration_ms": duration_ms,
        }
        if proc.returncode == 0:
            return ok_response(KIND_EXEC, task_id, **data)
        resp = error_response(f"command exited with code {proc.returncode}",
                              code=ErrorCode.INTERNAL, kind=KIND_EXEC, task_id=task_id)
        resp["data"] = data
        return resp

    def _task_files_read(self, payload: dict[str, Any], task_id: str) -> dict[str, Any]:
        raw_path = payload.get("path")
        if not raw_path:
            return error_response("'path' is required", code=ErrorCode.BAD_REQUEST,
                                  kind=KIND_FILES_READ, task_id=task_id)
        target = Path(str(raw_path)).expanduser()
        if not _within_roots(target, self.allowed_roots):
            return error_response("path is outside the allowed roots",
                                  code=ErrorCode.DENIED, kind=KIND_FILES_READ,
                                  task_id=task_id)
        try:
            resolved = target.resolve()
        except OSError as exc:
            return error_response(_short(exc), code=ErrorCode.NOT_FOUND,
                                  kind=KIND_FILES_READ, task_id=task_id)
        if not resolved.is_file():
            return error_response("file not found", code=ErrorCode.NOT_FOUND,
                                  kind=KIND_FILES_READ, task_id=task_id)

        try:
            max_bytes = int(payload.get("max_bytes", MAX_READ_BYTES))
        except (TypeError, ValueError):
            max_bytes = MAX_READ_BYTES
        max_bytes = max(1, min(max_bytes, MAX_READ_BYTES))

        try:
            size = resolved.stat().st_size
            raw = resolved.read_bytes()[:max_bytes]
        except OSError as exc:
            return error_response(_short(exc), code=ErrorCode.INTERNAL,
                                  kind=KIND_FILES_READ, task_id=task_id)

        truncated = size > len(raw)
        try:
            content = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(raw).decode("ascii")
            encoding = "base64"
        return ok_response(KIND_FILES_READ, task_id, path=str(resolved),
                           size_bytes=size, returned_bytes=len(raw),
                           truncated=truncated, encoding=encoding, content=content)

    def _task_voice_speak(self, payload: dict[str, Any], task_id: str) -> dict[str, Any]:
        text = str(payload.get("text") or "").strip()
        if not text:
            return error_response("'text' is required", code=ErrorCode.BAD_REQUEST,
                                  kind=KIND_VOICE_SPEAK, task_id=task_id)

        if os.name == "nt":
            # Use the built-in Windows voice on the PC agent. This is a local
            # SAPI call, not a shell; the child inherits the console-free node.
            quoted = text.replace("'", "''")
            script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.Rate = 0; $s.Speak('" + quoted + "')"
            )
            try:
                timeout_s = float(payload.get("timeout_s", 60.0))
                timeout_s = max(0.1, min(timeout_s, MAX_EXEC_TIMEOUT_S))
                proc = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive",
                     "-Command", script],
                    capture_output=True, text=True, timeout=timeout_s,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                return error_response("speech synthesis timed out",
                                      code=ErrorCode.TIMEOUT,
                                      kind=KIND_VOICE_SPEAK, task_id=task_id)
            except Exception as exc:
                return error_response(_short(exc), code=ErrorCode.INTERNAL,
                                      kind=KIND_VOICE_SPEAK, task_id=task_id)
            data = {"engine": "windows-sapi", "chars": len(text),
                    "returncode": proc.returncode,
                    "stderr": (proc.stderr or "")[:500]}
            if proc.returncode == 0:
                return ok_response(KIND_VOICE_SPEAK, task_id, **data)
            return error_response(proc.stderr or "Windows speech failed",
                                  code=ErrorCode.INTERNAL,
                                  kind=KIND_VOICE_SPEAK, task_id=task_id,
                                  **data)

        binary = _which_first(_TTS_CANDIDATES)
        if not binary:
            piper = _which_first(_PIPER_CANDIDATES)
            reason = ("no TTS binary found (looked for "
                      + ", ".join(_TTS_CANDIDATES) + ")")
            if piper:
                reason += "; 'piper' needs an explicit voice model and is not wired up"
            return error_response(reason, code=ErrorCode.UNAVAILABLE,
                                  kind=KIND_VOICE_SPEAK, task_id=task_id,
                                  available=False)
        try:
            timeout_s = float(payload.get("timeout_s", 30.0))
        except (TypeError, ValueError):
            timeout_s = 30.0
        timeout_s = max(0.1, min(timeout_s, MAX_EXEC_TIMEOUT_S))

        argv = [binary]
        rate = payload.get("rate")
        voice = payload.get("voice")
        if binary.endswith("spd-say"):
            if rate is not None:
                argv += ["-r", str(int(rate))]
            if voice:
                argv += ["-y", str(voice)]
        else:  # espeak / espeak-ng
            if "espeak" in os.path.basename(binary) and voice:
                argv += ["-v", str(voice)]
            if rate is not None:
                argv += ["-s", str(int(rate))]
        argv.append(text)

        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return error_response("speech synthesis timed out",
                                  code=ErrorCode.TIMEOUT, kind=KIND_VOICE_SPEAK,
                                  task_id=task_id)
        except OSError as exc:
            return error_response(_short(exc), code=ErrorCode.INTERNAL,
                                  kind=KIND_VOICE_SPEAK, task_id=task_id)
        if proc.returncode != 0:
            return error_response(f"TTS exited with code {proc.returncode}",
                                  code=ErrorCode.INTERNAL, kind=KIND_VOICE_SPEAK,
                                  task_id=task_id, stderr=_truncate(proc.stderr or "")[0])
        return ok_response(KIND_VOICE_SPEAK, task_id, spoken=True,
                           engine=os.path.basename(binary), chars=len(text))

    def _task_voice_play(self, payload: dict[str, Any], task_id: str) -> dict[str, Any]:
        """Play a pre-synthesised WAV (base64) on the node's local speakers.

        The Core synthesises once with its own high-quality voice and broadcasts
        the same audio to any host; this handler just plays it. Audio is size
        capped and never kept (written to a private temp file, deleted after).
        """
        audio_b64 = payload.get("audio_b64") or payload.get("audio")
        if not audio_b64:
            return error_response("'audio_b64' (base64 WAV) is required",
                                  code=ErrorCode.BAD_REQUEST,
                                  kind=KIND_VOICE_PLAY, task_id=task_id)
        try:
            raw = base64.b64decode(str(audio_b64), validate=True)
        except Exception:
            return error_response("'audio_b64' is not valid base64",
                                  code=ErrorCode.BAD_REQUEST,
                                  kind=KIND_VOICE_PLAY, task_id=task_id)
        if not raw:
            return error_response("empty audio payload", code=ErrorCode.BAD_REQUEST,
                                  kind=KIND_VOICE_PLAY, task_id=task_id)
        if len(raw) > MAX_PLAY_BYTES:
            return error_response(
                f"audio too large ({len(raw)} bytes; max {MAX_PLAY_BYTES})",
                code=ErrorCode.BAD_REQUEST, kind=KIND_VOICE_PLAY, task_id=task_id)

        try:
            timeout_s = float(payload.get("timeout_s", 30.0))
        except (TypeError, ValueError):
            timeout_s = 30.0
        timeout_s = max(0.1, min(timeout_s, MAX_EXEC_TIMEOUT_S))

        import tempfile
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="jarvis-voice-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            played = self._play_wav(path, timeout_s)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        if not played.get("ok"):
            return error_response(played.get("reason", "playback failed"),
                                  code=ErrorCode.INTERNAL,
                                  kind=KIND_VOICE_PLAY, task_id=task_id,
                                  engine=played.get("engine"))
        data: dict[str, Any] = {"played": True, "engine": played.get("engine"),
                                "bytes": len(raw)}
        if payload.get("sample_rate") is not None:
            data["sample_rate"] = payload.get("sample_rate")
        return ok_response(KIND_VOICE_PLAY, task_id, **data)

    def _play_wav(self, path: str, timeout_s: float) -> dict[str, Any]:
        """Play a WAV file through the platform's native player."""
        if os.name == "nt":
            p = str(path).replace("'", "''")
            script = ("$p = New-Object System.Media.SoundPlayer '" + p + "'; "
                      "$p.Load(); $p.PlaySync()")
            try:
                proc = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive",
                     "-Command", script],
                    capture_output=True, text=True, timeout=timeout_s,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "reason": "playback timed out"}
            except Exception as exc:  # pragma: no cover - defensive
                return {"ok": False, "reason": _short(exc)}
            if proc.returncode != 0:
                return {"ok": False,
                        "reason": (proc.stderr or "SoundPlayer failed").strip()[:300]}
            return {"ok": True, "engine": "windows-soundplayer"}

        binary = _which_first(_PLAY_CANDIDATES)
        if not binary:
            return {"ok": False,
                    "reason": "no WAV player found (looked for "
                              + ", ".join(_PLAY_CANDIDATES) + ")"}
        try:
            proc = subprocess.run([binary, str(path)], capture_output=True,
                                  text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "playback timed out"}
        except OSError as exc:
            return {"ok": False, "reason": _short(exc)}
        if proc.returncode != 0:
            return {"ok": False,
                    "reason": f"{binary} exited {proc.returncode}: "
                              f"{_truncate(proc.stderr or '')[0]}"}
        return {"ok": True, "engine": os.path.basename(binary)}

    def _task_power(self, payload: dict[str, Any], task_id: str) -> dict[str, Any]:
        """Run one allowlisted power verb (lock/sleep/shutdown/restart/cancel)."""
        action = str(payload.get("action") or "").strip().lower()
        if action not in _POWER_ACTIONS:
            return error_response(
                f"action must be one of {', '.join(_POWER_ACTIONS)}",
                code=ErrorCode.BAD_REQUEST, kind=KIND_POWER, task_id=task_id)
        delay: int | None = None
        if action in ("shutdown", "restart"):
            try:
                delay = int(payload.get("delay_s", 60))
            except (TypeError, ValueError):
                return error_response("delay_s must be an integer",
                                      code=ErrorCode.BAD_REQUEST, kind=KIND_POWER,
                                      task_id=task_id)
            if not _POWER_MIN_DELAY_S <= delay <= _POWER_MAX_DELAY_S:
                return error_response(
                    f"delay_s must be between {_POWER_MIN_DELAY_S} and {_POWER_MAX_DELAY_S}",
                    code=ErrorCode.BAD_REQUEST, kind=KIND_POWER, task_id=task_id)
        reason = str(payload.get("reason") or "")[:120]
        res = _run_power_action(action, delay, reason)
        if not res.get("ok"):
            return error_response(res.get("error") or "power action failed",
                                  code=ErrorCode.INTERNAL, kind=KIND_POWER,
                                  task_id=task_id,
                                  argv=res.get("argv"),
                                  returncode=res.get("returncode"))
        return ok_response(KIND_POWER, task_id, action=action,
                           argv=res.get("argv"), returncode=res.get("returncode"))

    def _task_metrics(self, payload: dict[str, Any], task_id: str) -> dict[str, Any]:
        """Return a best-effort live telemetry snapshot for the Core's monitor loop."""
        try:
            data = _node_metrics_snapshot()
        except Exception as exc:  # pragma: no cover - a bad probe must not take the node down
            return error_response(_short(exc), code=ErrorCode.INTERNAL,
                                  kind=KIND_METRICS, task_id=task_id)
        return ok_response(KIND_METRICS, task_id, **data)


def _run_power_action(action: str, delay: int | None, reason: str) -> dict[str, Any]:
    """Execute one allowlisted power verb; return an honest result dict."""
    if os.name == "nt":
        if action == "lock":
            argv = ["rundll32.exe", "user32.dll,LockWorkStation"]
        elif action == "sleep":
            argv = ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
        elif action == "shutdown":
            argv = ["shutdown.exe", "/s", "/t", str(delay)]
        elif action == "restart":
            argv = ["shutdown.exe", "/r", "/t", str(delay)]
        else:  # cancel
            argv = ["shutdown.exe", "/a"]
    else:
        if action == "lock":
            argv = ["loginctl", "lock-session"]
        elif action == "sleep":
            argv = ["systemctl", "suspend"]
        elif action == "shutdown":
            argv = ["shutdown", "-h", f"+{max(1, (int(delay or 60) + 59) // 60)}"]
        elif action == "restart":
            argv = ["shutdown", "-r", f"+{max(1, (int(delay or 60) + 59) // 60)}"]
        else:  # cancel
            argv = ["shutdown", "-c"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "power command timed out", "argv": argv}
    except OSError as exc:
        return {"ok": False, "error": _short(exc), "argv": argv}
    return {"ok": proc.returncode == 0, "argv": argv, "returncode": proc.returncode,
            "stderr": (proc.stderr or "")[:300]}


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# ---------------------------------------------------------------------------
# transport: FastAPI (preferred) or stdlib http.server (fallback)
# ---------------------------------------------------------------------------


def build_fastapi_app(agent: NodeAgent) -> Any:
    """Build a FastAPI app, or return ``None`` when FastAPI is unavailable."""
    try:
        from fastapi import FastAPI as _FastAPI  # noqa: F401  (re-check availability)
        from fastapi.concurrency import run_in_threadpool
        from fastapi.responses import JSONResponse
    except Exception:
        return None
    if FastAPI is None:  # pragma: no cover - defensive
        return None

    app = FastAPI(title="jarvis-node", version=VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def _unauthorized() -> Any:
        return JSONResponse(
            error_response("unauthorized", code=ErrorCode.UNAUTHORIZED),
            status_code=401)

    @app.get(HEALTH_PATH)
    def health(request: Request) -> Any:
        if not agent.authorized(request.headers.get(HEADER)):
            return _unauthorized()
        return agent.health()

    @app.post(TASK_PATH)
    async def task(request: Request) -> Any:
        if not agent.authorized(request.headers.get(HEADER)):
            return _unauthorized()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                error_response("malformed JSON body", code=ErrorCode.BAD_REQUEST),
                status_code=400)
        # dispatch is blocking (subprocess / disk); keep the event loop free.
        return await run_in_threadpool(agent.dispatch, body)

    return app


def build_http_server(agent: NodeAgent, host: str, port: int) -> ThreadingHTTPServer:
    """Build the stdlib fallback server (used when FastAPI/uvicorn absent)."""

    class _Handler(BaseHTTPRequestHandler):
        server_version = f"jarvis-node/{VERSION}"
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            return agent.authorized(self.headers.get(HEADER))

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = urlsplit(self.path).path
            if path != HEALTH_PATH:
                self._send(404, error_response("not found", code=ErrorCode.NOT_FOUND))
                return
            if not self._authorized():
                self._send(401, error_response("unauthorized",
                                               code=ErrorCode.UNAUTHORIZED))
                return
            self._send(200, agent.health())

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            path = urlsplit(self.path).path
            if path != TASK_PATH:
                self._send(404, error_response("not found", code=ErrorCode.NOT_FOUND))
                return
            if not self._authorized():
                self._send(401, error_response("unauthorized",
                                               code=ErrorCode.UNAUTHORIZED))
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._send(400, error_response("malformed JSON body",
                                               code=ErrorCode.BAD_REQUEST))
                return
            self._send(200, agent.dispatch(body))

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter default
            log.debug("node http %s - %s", self.address_string(), fmt % args)

    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server


def create_agent(cfg: Any, *, token: str | None = None) -> NodeAgent:
    """Construct a :class:`NodeAgent` from config (token resolved if omitted)."""
    roots = [str(Path.home()), str(Path(cfg.data_path))]
    return NodeAgent(token if token is not None else load_token(cfg),
                     name=socket.gethostname(), allowed_roots=roots, config=cfg)


def run_agent(cfg: Any, host: str = "0.0.0.0", port: int | None = None) -> None:
    """Run the satellite agent until interrupted.

    Uses uvicorn/FastAPI when available, otherwise the stdlib server. The
    resolved bind address is logged (never the token).
    """
    port = int(port if port is not None else getattr(cfg.server, "node_port", DEFAULT_NODE_PORT))
    agent = create_agent(cfg)
    log.info("jarvis node '%s' starting on %s:%d (capabilities: %s)",
             agent.name, host, port, ",".join(agent.capabilities))
    if host in ("0.0.0.0", "::"):
        log.warning("node agent is listening on every interface without TLS; the shared "
                    "token crosses the LAN in clear text. Prefer 127.0.0.1 over an SSH "
                    "tunnel, or isolate the node network.")

    app = build_fastapi_app(agent)
    if app is not None:
        try:
            import uvicorn
        except Exception:
            uvicorn = None  # type: ignore[assignment]
        if uvicorn is not None:
            log.info("transport: fastapi + uvicorn")
            uvicorn.run(app, host=host, port=port, log_level="warning")
            return
        log.warning("fastapi present but uvicorn missing; using stdlib server")

    log.info("transport: stdlib http.server")
    server = build_http_server(agent, host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        server.server_close()
