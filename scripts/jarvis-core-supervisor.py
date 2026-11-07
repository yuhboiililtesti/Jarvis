"""Console-free JARVIS Core supervisor for Windows Task Scheduler.

Runs under ``pythonw.exe``. It starts the real Core with
``python.exe -m jarvis serve`` using CREATE_NO_WINDOW, sends stdout/stderr to
``data/logs/core-supervisor.log`` and restarts on failure. The child and all
of its descendants are assigned to a kill-on-close job object, so stopping
this supervisor cannot leave an orphan Core process.

Why not launch ``pythonw.exe -m jarvis serve`` directly? On this build the Core
uses console-oriented startup paths and exits under pythonw before serving.
A detached python.exe child keeps the server semantics while remaining
windowless.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "data" / "logs" / "core-supervisor.log"

CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _install_kill_on_close_job() -> None:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            return
        kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
    except Exception:
        return


def _log(message: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def main() -> int:
    _install_kill_on_close_job()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    while True:
        _log("starting core")
        try:
            with LOG.open("ab") as output:
                child = subprocess.Popen(
                    [str(PYTHON), "-m", "jarvis", "serve"],
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                    close_fds=True,
                )
                rc = child.wait()
            _log(f"core exited rc={rc}; restarting in 5s")
        except Exception as exc:
            _log(f"core supervisor error: {type(exc).__name__}: {exc}; restarting in 5s")
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
