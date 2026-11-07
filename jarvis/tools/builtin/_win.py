"""Shared helpers for the desktop tool modules (window, input, media, power).

Private module (leading underscore): the builtin loader skips it, so importing
it never registers anything. Everything Windows-specific is created lazily so
the module imports cleanly on Linux, where callers get an honest
"unsupported on this platform" error instead of an ``AttributeError``.

Nothing in this module performs an effect at import time.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from typing import Any

from ...logging_setup import get_logger
from ...sandbox.env import sanitized_env

log = get_logger("jarvis.tools.win")

IS_WINDOWS = os.name == "nt"

UNSUPPORTED = "unsupported on this platform ({plat}); this tool is Windows-only".format(
    plat=sys.platform)

# ---------------------------------------------------------------------------
# SendInput structures (sizes match the Win32 ABI on both 32- and 64-bit)
# ---------------------------------------------------------------------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def mouse_input(dx: int = 0, dy: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def send_inputs(inputs: list[INPUT]) -> int:
    """Inject events with user32.SendInput. Returns the count actually inserted.

    A return smaller than ``len(inputs)`` means Windows blocked part of the
    sequence (UIPI against an elevated window, a secure desktop, ...). Note that
    UIPI may also block input *silently* while still returning the full count.
    """
    if not IS_WINDOWS:
        raise OSError(UNSUPPORTED)
    if not inputs:
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    arr = (INPUT * len(inputs))(*inputs)
    return int(user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)))


# ---------------------------------------------------------------------------
# subprocess helper (never shell=True; never raises)
# ---------------------------------------------------------------------------

_SANDBOX_SERVICE: Any = None


def _sandbox_service() -> Any:
    """A cached SandboxService bound to the on-disk config (or None if unavailable).

    Built once per process. Uses the owner's ``[sandbox]`` settings, so disabling
    the sandbox there disables it here too; the environment is still scrubbed by
    the fallback path below if the sandbox module cannot be imported.
    """
    global _SANDBOX_SERVICE
    if _SANDBOX_SERVICE is None:
        try:
            from ...config import load_config
            from ...sandbox import SandboxService
            _SANDBOX_SERVICE = SandboxService(config=load_config())
        except Exception as exc:  # pragma: no cover - sandbox module must exist
            log.warning("sandbox service unavailable for run_cmd: %s", exc)
            _SANDBOX_SERVICE = False
    return _SANDBOX_SERVICE or None


def run_cmd(argv: list[str], *, timeout: float = 15.0, env: dict[str, str] | None = None,
            input_text: str | None = None) -> dict[str, Any]:
    """Run ``argv`` and return ``{ok, code, stdout, stderr, error}``. Never raises.

    The child is run through the JARVIS sandbox (a Windows Job Object or POSIX
    rlimits): the whole process tree is contained, and the environment is scrubbed
    of ``JARVIS_SECRET_*`` (and any value matching a stored secret) before ``env``
    extras are merged, so a desktop helper can never read a secret.
    """
    svc = _sandbox_service()
    if svc is not None:
        try:
            res = svc.run(list(argv), timeout=timeout, extra_env=env, stdin_text=input_text)
            error = None
            if res.timed_out:
                error = res.error or f"{argv[0]} timed out after {timeout:g}s"
            elif res.exit_code is None:
                error = res.error or f"could not run {argv[0]}"
            return {"ok": bool(res.exit_code == 0 and not res.timed_out),
                    "code": res.exit_code, "stdout": res.stdout, "stderr": res.stderr,
                    "error": error}
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "code": None, "stdout": "", "stderr": "",
                    "error": f"{type(exc).__name__}: {exc}"}

    try:  # sandbox unavailable: still scrub the environment, never shell=True
        full_env = sanitized_env(extra=env)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=full_env, input=input_text, creationflags=flags,
        )
        return {"ok": proc.returncode == 0, "code": proc.returncode,
                "stdout": proc.stdout or "", "stderr": proc.stderr or "", "error": None}
    except FileNotFoundError:
        return {"ok": False, "code": None, "stdout": "", "stderr": "",
                "error": f"executable not found: {argv[0]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": None, "stdout": "", "stderr": "",
                "error": f"{argv[0]} timed out after {timeout:g}s"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "code": None, "stdout": "", "stderr": "",
                "error": f"{type(exc).__name__}: {exc}"}


def ps_argv(script: str) -> list[str]:
    """Windows PowerShell 5.1 invocation (WinRT projections need it, not pwsh)."""
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", script]


def int_arg(kwargs: dict[str, Any], key: str, default: int | None = None) -> int | None:
    """Parse an integer kwarg; raises ValueError with a clear message."""
    val = kwargs.get(key)
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        raise ValueError(f"{key} must be an integer")
    try:
        f = float(val)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer") from None
    if f != int(f):
        raise ValueError(f"{key} must be an integer")
    return int(f)


def bool_arg(kwargs: dict[str, Any], key: str, default: bool | None = None) -> bool | None:
    val = kwargs.get(key)
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    text = str(val).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"{key} must be a boolean")
