"""Single console-free PC-001 agent supervisor.

Runs under ``pythonw.exe`` and owns both background helper scripts:

  * jarvis-tunnels.py        : two SSH connections (core + laptop)
  * jarvis-node-supervisor.py: local PC host agent on the configured NIC

One Task Scheduler entry, no console windows, no orphan child trees (kill-on-
close job object). The node and tunnel helpers already contain their own
restart logic; this parent only restarts a helper if it exits.
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
PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
LOGDIR = ROOT / "data" / "logs"
STATUS = ROOT / "data" / "jarvis-agent-status.json"
CHILDREN = (
    ("tunnels", ROOT / "scripts" / "jarvis-tunnels.py"),
    ("node", ROOT / "scripts" / "jarvis-node-supervisor.py"),
    ("voice", ROOT / "scripts" / "jarvis-voice-listener.py"),
)
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class _Io(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _Basic(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _Extended(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _job() -> None:
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        job = k.CreateJobObjectW(None, None)
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if job and k.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                             ctypes.byref(info), ctypes.sizeof(info)):
            k.AssignProcessToJobObject(job, k.GetCurrentProcess())
    except Exception:
        pass


def _status(**kw: object) -> None:
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps({"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), **kw},
                                     indent=2), encoding="utf-8")
    except OSError:
        pass


def _start(name: str, script: Path) -> subprocess.Popen[bytes]:
    LOGDIR.mkdir(parents=True, exist_ok=True)
    log = LOGDIR / f"jarvis-agent-{name}.log"
    with log.open("ab") as out:
        return subprocess.Popen(
            [str(PYW), str(script)], cwd=str(ROOT), stdin=subprocess.DEVNULL,
            stdout=out, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, close_fds=True)


def main() -> int:
    _job()
    procs: dict[str, subprocess.Popen[bytes]] = {}
    while True:
        for name, script in CHILDREN:
            proc = procs.get(name)
            if proc is None or proc.poll() is not None:
                procs[name] = _start(name, script)
                _status(state="running", supervisor_pid=os.getpid(),
                        children={k: v.pid for k, v in procs.items()})
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
