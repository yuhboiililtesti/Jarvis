"""Console-free Windows supervisor for JARVIS SSH tunnels.

Runs under ``pythonw.exe``. It keeps localhost-only SSH forwards alive using
**one SSH connection per host**:

    APOS   127.0.0.1:2227  -> APOS 127.0.0.1:2226   (node)
           127.0.0.1:11435 -> APOS 127.0.0.1:11434  (Ollama)
           127.0.0.1:8765  -> APOS 127.0.0.1:8765   (Core)
    laptop 127.0.0.1:2226  -> laptop 127.0.0.1:2226 (node)

No token is stored here; node/Ollama auth travels inside SSH. Child SSH
processes are created with CREATE_NO_WINDOW and assigned (by inheritance) to a
kill-on-close job object, so Task Scheduler stop/restart does not leave orphan
tunnel processes behind.

This is a transport helper, not part of the JARVIS package API.
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
SSH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
# Credentials and endpoints come from the environment — nothing real is committed.
KEY = Path(os.environ.get("JARVIS_SSH_KEY") or (Path.home() / ".ssh" / "id_ed25519"))
KNOWN = Path(os.environ.get("JARVIS_SSH_KNOWN_HOSTS") or (Path.home() / ".ssh" / "known_hosts"))
SSH_USER = os.environ.get("JARVIS_SSH_USER") or os.environ.get("USER") or "user"
LOG = ROOT / "data" / "logs" / "tunnels.log"

SSH_OPTIONS = [
    "-N",
    "-i", str(KEY),
    "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", f"UserKnownHostsFile={KNOWN}",
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=yes",
]

# Tunnel targets come from the environment so the published repo carries no real
# endpoints. Set JARVIS_APOS_HOST / JARVIS_APOS_PORT / JARVIS_LAPTOP_HOST /
# JARVIS_LAPTOP_PORT before running (the placeholder values below are inert).
APOS_HOST = os.environ.get("JARVIS_APOS_HOST", "<APOS_HOST>")
APOS_PORT = int(os.environ.get("JARVIS_APOS_PORT", "2223"))
LAPTOP_HOST = os.environ.get("JARVIS_LAPTOP_HOST", "<LAPTOP_HOST>")
LAPTOP_PORT = int(os.environ.get("JARVIS_LAPTOP_PORT", "2225"))

# name, ssh host, ssh port, forwards [(label, local_port, remote_host, remote_port)]
GROUPS = (
    ("apos", APOS_HOST, APOS_PORT, (
        ("apos-node", 2227, "127.0.0.1", 2226),
        ("ollama-apos", 11435, "127.0.0.1", 11434),
        ("apos-core", 8765, "127.0.0.1", 8765),
        ("evolve-arena", 8766, "127.0.0.1", 8766),
    )),
    ("laptop", LAPTOP_HOST, LAPTOP_PORT, (
        ("laptop-node", 2226, "127.0.0.1", 2226),
    )),
)

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
    """Best-effort: make the supervisor's children die with it."""
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


def _start(group: tuple[str, str, int, tuple[tuple[str, int, str, int], ...]]) -> subprocess.Popen[bytes]:
    name, ssh_host, ssh_port, forwards = group
    args = [str(SSH), *SSH_OPTIONS]
    labels = []
    for label, local_port, remote_host, remote_port in forwards:
        forward = f"127.0.0.1:{local_port}:{remote_host}:{remote_port}"
        args += ["-L", forward]
        labels.append(label)
    args += ["-p", str(ssh_port), f"{SSH_USER}@{ssh_host}"]
    _log(f"starting {name} ({', '.join(labels)}) via {ssh_host}:{ssh_port}")
    return subprocess.Popen(
        args,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
    )


def _rotate_log() -> None:
    """Keep the retry log bounded (it grows fast while a host is unreachable)."""
    try:
        if LOG.exists() and LOG.stat().st_size > 5 * 1024 * 1024:
            backup = LOG.with_name(LOG.name + ".1")
            if backup.exists():
                backup.unlink()
            LOG.replace(backup)
    except OSError:
        pass


def main() -> int:
    _rotate_log()
    _install_kill_on_close_job()
    children: dict[str, subprocess.Popen[bytes]] = {}
    failures: dict[str, int] = {}
    started: dict[str, float] = {}
    next_try: dict[str, float] = {}
    try:
        while True:
            now = time.monotonic()
            for group in GROUPS:
                name = group[0]
                child = children.get(name)
                alive = child is not None and child.poll() is None
                if alive or now < next_try.get(name, 0.0):
                    continue
                if child is not None:
                    ran = time.monotonic() - started.get(name, 0.0)
                    failures[name] = 0 if ran > 60 else failures.get(name, 0) + 1
                    delay = min(60, 5 * max(1, failures[name]))
                    _log(f"{name} exited rc={child.returncode} after "
                         f"{ran:.0f}s; retrying in {delay}s")
                else:
                    failures[name] = 0
                delay = min(60, 5 * max(1, failures[name]))
                next_try[name] = time.monotonic() + delay
                children[name] = _start(group)
                started[name] = time.monotonic()
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children.values():
            try:
                child.terminate()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




