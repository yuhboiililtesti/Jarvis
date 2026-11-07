"""Console-free PC-001 JARVIS node supervisor.

Runs under pythonw.exe. Launches ``python.exe -m jarvis.nodes`` on the
configured NIC with CREATE_NO_WINDOW, captures
output to data/logs/pc-node-supervisor.log, writes a non-secret status JSON,
and restarts on failure. A kill-on-close job object prevents orphan children.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "data" / "logs" / "pc-node-supervisor.log"
STATUS = ROOT / "data" / "pc-node-status.json"
# Bind address comes from the environment — no real endpoint is committed.
HOST = os.environ.get("JARVIS_NODE_HOST", "127.0.0.1")
PORT = int(os.environ.get("JARVIS_NODE_PORT", "2228"))
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


def _job() -> None:
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        job = k.CreateJobObjectW(None, None)
        if not job:
            return
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                         ctypes.byref(info), ctypes.sizeof(info)):
            return
        k.AssignProcessToJobObject(job, k.GetCurrentProcess())
    except Exception:
        return


def _log(message: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _status(**kw: object) -> None:
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        payload = {"host": HOST, "port": PORT,
                   "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), **kw}
        STATUS.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    _job()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    while True:
        _log("starting PC node")
        _status(state="starting", supervisor_pid=os.getpid())
        try:
            with LOG.open("ab") as out:
                child = subprocess.Popen(
                    [str(PYTHON), "-m", "jarvis.nodes", "--host", HOST,
                     "--port", str(PORT)],
                    cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=out,
                    stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW,
                    close_fds=True,
                )
            _status(state="running", child_pid=child.pid,
                    supervisor_pid=os.getpid())
            rc = child.wait()
            _log(f"PC node exited rc={rc}; restarting")
            _status(state="exited", child_pid=child.pid,
                    supervisor_pid=os.getpid(), returncode=rc)
        except Exception as exc:
            _log(f"supervisor error: {type(exc).__name__}: {exc}")
            _status(state="error", supervisor_pid=os.getpid(),
                    error=type(exc).__name__)
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
