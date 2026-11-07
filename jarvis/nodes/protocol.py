"""Shared wire contract for the JARVIS node (satellite) subsystem.

Both halves of the Core <-> satellite link import this module so they cannot
drift apart:

  * ``jarvis.nodes.service`` (Core side) dials remote satellites.
  * ``jarvis.nodes.agent``   (Node side) answers them on the ThinkPad.

It defines the shared-secret header name, the two HTTP paths, the task kinds,
the request/response shapes and the error codes. It has *no* third-party
dependencies so the Node can import it with nothing but the stdlib installed.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, TypedDict

__all__ = [
    "HEADER",
    "ROLE_NODE",
    "DEFAULT_NODE_PORT",
    "HEALTH_TIMEOUT_S",
    "TASK_TIMEOUT_S",
    "DEFAULT_TIMEOUT_S",
    "HEARTBEAT_INTERVAL_S",
    "HEALTH_PATH",
    "TASK_PATH",
    "KIND_EXEC",
    "KIND_SYSINFO",
    "KIND_VOICE_SPEAK",
    "KIND_VOICE_PLAY",
    "KIND_POWER",
    "KIND_METRICS",
    "KIND_FILES_READ",
    "KIND_PING",
    "KIND_TOOL",
    "SUPPORTED_KINDS",
    "ErrorCode",
    "TaskRequest",
    "TaskResponse",
    "HealthResponse",
    "make_task_request",
    "ok_response",
    "error_response",
    "DESTRUCTIVE_PATTERNS",
    "is_destructive_command",
]

# ---------------------------------------------------------------------------
# transport constants
# ---------------------------------------------------------------------------

#: Shared-secret header carried on *every* Core -> Node and Node -> Core request.
HEADER = "X-Jarvis-Node-Token"

#: Value reported by the Node in its health payload.
ROLE_NODE = "node"

#: Node agent listens here on the laptop; kept separate from SSH (2225).
DEFAULT_NODE_PORT = 2226

HEALTH_PATH = "/node/health"
TASK_PATH = "/node/task"

#: Wall-clock budgets used by the Core side.
HEALTH_TIMEOUT_S = 5.0
TASK_TIMEOUT_S = 60.0
DEFAULT_TIMEOUT_S = HEALTH_TIMEOUT_S

#: Heartbeat cadence for the background reachability loop.
HEARTBEAT_INTERVAL_S = 20.0

# ---------------------------------------------------------------------------
# task kinds
# ---------------------------------------------------------------------------

KIND_EXEC = "exec"
KIND_SYSINFO = "sysinfo"
KIND_VOICE_SPEAK = "voice.speak"
#: Play a pre-synthesised WAV (base64) on the node's local speakers. This lets
#: the Core synthesise once with its own high-quality voice and broadcast the
#: same audio to any host, instead of each host using its own different TTS.
KIND_VOICE_PLAY = "voice.play"
KIND_FILES_READ = "files.read"
KIND_PING = "ping"
#: Run an allowlisted local built-in JARVIS tool on the host. The node is the
#: authority for the local allowlist; the Core remains the policy/approval layer.
KIND_TOOL = "tool"
#: A controlled power action (lock/sleep/shutdown/restart/cancel). Kept separate
#: from ``exec`` so the exec denylist still refuses ``shutdown``/``reboot`` while
#: this narrow, allowlisted surface permits exactly the power verbs the Core has
#: already policy-approved.
KIND_POWER = "power"
#: Live host telemetry snapshot (cpu/mem/load/disk/uptime) for the Core's
#: per-node monitoring loop. Read-only and cheap; the node reports each field it
#: cannot measure as ``None`` rather than inventing a number.
KIND_METRICS = "metrics"

#: Every kind the Node agent implements. Order is presentation-friendly.
SUPPORTED_KINDS: tuple[str, ...] = (
    KIND_EXEC,
    KIND_SYSINFO,
    KIND_VOICE_SPEAK,
    KIND_VOICE_PLAY,
    KIND_POWER,
    KIND_METRICS,
    KIND_FILES_READ,
    KIND_PING,
    KIND_TOOL,
)

# ---------------------------------------------------------------------------
# error codes
# ---------------------------------------------------------------------------


class ErrorCode(str, Enum):
    """Stable machine-readable codes shared by both ends."""

    OK = "ok"
    UNAUTHORIZED = "unauthorized"
    BAD_REQUEST = "bad_request"
    UNSUPPORTED_KIND = "unsupported_kind"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    UNREACHABLE = "unreachable"
    INTERNAL = "internal_error"


# ---------------------------------------------------------------------------
# wire schemas
# ---------------------------------------------------------------------------


class TaskRequest(TypedDict):
    """Body POSTed by the Core to ``{base_url}/node/task``."""

    kind: str
    payload: dict[str, Any]
    task_id: str


class TaskResponse(TypedDict, total=False):
    """JSON returned by the Node for a task (``ok`` is always present)."""

    ok: bool
    kind: str
    task_id: str
    code: str
    error: str
    data: Any
    supported: list[str]


class HealthResponse(TypedDict, total=False):
    """JSON returned by ``{base_url}/node/health``."""

    ok: bool
    name: str
    role: str
    capabilities: list[str]
    uptime_s: float
    load: list[float]
    version: str


def make_task_request(kind: str, payload: dict[str, Any] | None, task_id: str) -> TaskRequest:
    """Build a well-formed :class:`TaskRequest`."""
    return {"kind": str(kind), "payload": dict(payload or {}), "task_id": str(task_id)}


def ok_response(kind: str = "", task_id: str = "", **data: Any) -> TaskResponse:
    """Success envelope: ``{"ok": True, ...}``."""
    out: TaskResponse = {"ok": True}
    if kind:
        out["kind"] = kind
    if task_id:
        out["task_id"] = task_id
    if data:
        out["data"] = data
    return out


def error_response(error: str, *, code: ErrorCode | str = ErrorCode.INTERNAL,
                   kind: str = "", task_id: str = "", **extra: Any) -> TaskResponse:
    """Failure envelope: ``{"ok": False, "error": ..., "code": ...}``."""
    out: TaskResponse = {
        "ok": False,
        "error": str(error),
        "code": code.value if isinstance(code, ErrorCode) else str(code),
    }
    if kind:
        out["kind"] = kind
    if task_id:
        out["task_id"] = task_id
    if extra:
        out["data"] = extra
    return out


# ---------------------------------------------------------------------------
# destructive-command denylist (shared "idea" with the Core policy gate)
# ---------------------------------------------------------------------------

#: Command substrings/regexes the Node refuses to run even when authenticated.
#: This is a *defence in depth* guard rail, not a security boundary: the shared
#: token already gates the endpoint. It mirrors the Core's refusal to perform
#: irreversible effects without explicit owner authority.
DESTRUCTIVE_PATTERNS: tuple[str, ...] = (
    r"\brm\s+(-[a-z]*\s+)*-?[a-z]*r[a-z]*f?\s+/(\s|$)",   # rm -rf /
    r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+~/?(\s|$)",          # rm -rf ~
    r":\(\)\s*\{\s*:\|:&\s*\};:",                          # fork bomb
    r"\bmkfs(\.[a-z0-9]+)?\b",                             # format a filesystem
    r"\bwipefs\b",
    r"\bdd\b[^\n]*\bof=/dev/",
    r">\s*/dev/(sd|nvme|hd|vd)[a-z0-9]",
    r"\bchmod\s+-R\s+777\s+/(\s|$)",
    r"\bchown\s+-R\s+[^\s]+\s+/(\s|$)",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r"\binit\s+0\b",
    r"(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|d)?sh\b",  # pipe-to-shell
    r"\bhistory\s+-c\b",
    r"\b(shred|srm)\b",
)

_DESTRUCTIVE_RE = tuple(re.compile(p, re.IGNORECASE) for p in DESTRUCTIVE_PATTERNS)


def is_destructive_command(command: str) -> str | None:
    """Return the matching denylist pattern, or ``None`` when allowed.

    Matching is best-effort and intentionally conservative; the caller must
    still treat the Node as a convenience relay rather than a trust anchor.
    """
    text = str(command or "")
    for pattern, compiled in zip(DESTRUCTIVE_PATTERNS, _DESTRUCTIVE_RE):
        if compiled.search(text):
            return pattern
    return None
