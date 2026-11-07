"""Windows containment tier: a Job Object around the child process tree.

A Windows Job Object is the supported OS facility for this. Applied to a child,
it gives real, kernel-enforced guarantees:

  * ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` — when the handle closes, every
    process still in the job dies. No orphan is left behind (the failure mode
    that produced the stray ``python.exe`` processes this tier exists to stop).
  * ``JOB_OBJECT_LIMIT_ACTIVE_PROCESS`` — a cap on simultaneous processes, so a
    fork bomb cannot exhaust the machine.
  * ``JOB_OBJECT_LIMIT_PROCESS_MEMORY`` / ``JOB_OBJECT_LIMIT_JOB_MEMORY`` — a
    hard memory ceiling; an allocation past it fails instead of thrashing.
  * ``JOB_OBJECT_LIMIT_PROCESS_TIME`` — a per-process CPU-time cap.

The process is assigned to the job **immediately after it starts**. CPython's
``subprocess`` closes the primary-thread handle, so the cleaner "create
suspended, assign, resume" sequence is not available through ``Popen``; the
assignment therefore happens microseconds after ``CreateProcess`` returns. A
shell interpreter has not finished initialising in that window, so in practice
nothing escapes it — but it is stated rather than claimed away, and the safer
alternative (a ``PROC_THREAD_ATTRIBUTE_JOB_LIST`` STARTUPINFOEX) is noted in
``docs/SANDBOX.md`` as future work.

What this tier does **not** do: isolate the filesystem or the network. The child
still runs as the owner's account and can reach the same files and sockets. That
is stated plainly so it is not mistaken for a container or a VM.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Any

from ..logging_setup import get_logger
from .limits import Limits
from .result import SandboxResult, SandboxUnavailable

log = get_logger("jarvis.sandbox.winjob")

__all__ = ["available", "run_in_job", "capabilities"]

TIER = "jobobject"

# --- constants -------------------------------------------------------------
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

_JobObjectExtendedLimitInformation = 9
_JobObjectBasicAccountingInformation = 1

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

_HUNDRED_NS = 10_000_000  # 1 second in 100-ns units


# --- structures ------------------------------------------------------------
class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


_k32: Any = None


def _kernel32() -> Any:
    global _k32
    if _k32 is not None:
        return _k32
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k.TerminateJobObject.restype = wintypes.BOOL
    k.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD))
    k.QueryInformationJobObject.restype = wintypes.BOOL
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    k.CloseHandle.restype = wintypes.BOOL
    _k32 = k
    return k


def available() -> tuple[bool, str]:
    """Whether Job Object containment can be used on this interpreter/OS."""
    if os.name != "nt":
        return False, f"Windows Job Objects are Windows-only (this is {sys.platform})"
    try:
        _kernel32()
        _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()   # sanity: structs built
    except Exception as exc:  # pragma: no cover - only on a broken interpreter
        return False, f"kernel32 unavailable: {exc}"
    return True, "Windows Job Object: kill-tree on close, memory/process/CPU caps"


def capabilities() -> list[str]:
    return ["kill-tree", "memory-cap", "process-cap", "cpu-time-cap",
            "limits-at-start"]


def _configure(job: Any, limits: Limits) -> bool:
    """Apply the requested limits to the job. Returns True only if the OS accepted them.

    A failure is **not** silent: the caller degrades ``contained`` to False and adds a
    note, because an unconfigured job has no kill-on-close and no caps.
    """
    k = _kernel32()
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    basic = info.BasicLimitInformation
    basic.LimitFlags = JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    if limits.kill_tree:
        basic.LimitFlags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if limits.max_processes > 0:
        basic.LimitFlags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        basic.ActiveProcessLimit = min(int(limits.max_processes), 0xFFFFFFFF)
    if limits.max_memory_mb > 0:
        # Clamp to a sane ceiling: a value beyond c_size_t would be silently
        # truncated and then rejected by SetInformationJobObject (winerr 87).
        mb = min(int(limits.max_memory_mb), 1_000_000)          # <= 1 TiB
        bytes_ = mb * 1024 * 1024
        basic.LimitFlags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_JOB_MEMORY
        info.ProcessMemoryLimit = bytes_
        info.JobMemoryLimit = bytes_ * max(1, min(limits.max_processes or 1, 8))
    if limits.max_cpu_seconds > 0:
        basic.LimitFlags |= JOB_OBJECT_LIMIT_PROCESS_TIME
        basic.PerProcessUserTimeLimit = min(int(limits.max_cpu_seconds), 10 ** 6) * _HUNDRED_NS
    if not k.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                     ctypes.byref(info), ctypes.sizeof(info)):
        log.warning("SetInformationJobObject failed (winerr %s); the job has NO limits "
                    "and NO kill-on-close", ctypes.get_last_error())
        return False
    return True


def _total_terminated(job: Any) -> int:
    k = _kernel32()
    acct = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    returned = wintypes.DWORD(0)
    if k.QueryInformationJobObject(job, _JobObjectBasicAccountingInformation,
                                   ctypes.byref(acct), ctypes.sizeof(acct),
                                   ctypes.byref(returned)):
        return int(acct.TotalTerminatedProcesses)
    return 0


def run_in_job(argv: list[str], *, cwd: str | None = None,
               env: dict[str, str] | None = None, timeout: float = 30.0,
               limits: Limits | None = None, stdin_text: str | None = None,
               ) -> SandboxResult:
    """Run ``argv`` inside a fresh Job Object. Raises :class:`SandboxUnavailable`
    only if the job or process could not be created; a failure to *assign* the
    process is reported in the result (``contained=False``) rather than hidden."""
    limits = limits or Limits()
    ok, reason = available()
    if not ok:
        raise SandboxUnavailable(reason)
    k = _kernel32()
    job = k.CreateJobObjectW(None, None)
    if not job:  # pragma: no cover - depends on the host
        raise SandboxUnavailable(f"CreateJobObject failed (winerr {ctypes.get_last_error()})")

    flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    t0 = time.perf_counter()
    try:
        configured = _configure(job, limits)
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        except FileNotFoundError:
            raise SandboxUnavailable(f"executable not found: {argv[0]}")

        notes: list[str] = []
        if not configured:
            notes.append("job limits could not be applied (no kill-on-close and no caps); "
                         "containment is not guaranteed")
        try:
            handle = int(proc._handle)
        except (AttributeError, TypeError, ValueError):  # pragma: no cover - defensive
            handle = None
        assigned = bool(handle) and bool(k.AssignProcessToJobObject(job, handle))
        if not assigned:  # pragma: no cover - depends on the host's job rules
            winerr = ctypes.get_last_error()
            log.warning("AssignProcessToJobObject failed (winerr %s); process runs "
                        "without containment", winerr)
            notes.append(f"job assignment failed (winerr {winerr}); the command ran "
                         "without containment")
        contained = bool(assigned and configured)

        timed_out = False
        try:
            data = stdin_text.encode("utf-8") if stdin_text is not None else None
            out_b, err_b = proc.communicate(input=data, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if assigned:
                k.TerminateJobObject(job, 137)
            else:  # pragma: no cover
                proc.kill()
            try:
                out_b, err_b = proc.communicate(timeout=10.0)
            except Exception:  # pragma: no cover - defensive
                out_b, err_b = b"", b""
            try:
                proc.wait(timeout=5.0)
            except Exception:  # pragma: no cover
                pass

        duration = (time.perf_counter() - t0) * 1000.0
        code = proc.returncode
        stdout = (out_b or b"").decode("utf-8", errors="replace")
        stderr = (err_b or b"").decode("utf-8", errors="replace")
        terminated = _total_terminated(job) if (assigned and configured) else 0
        if terminated:
            notes.append(f"{terminated} process(es) terminated by the OS for exceeding "
                         "a sandbox limit")

        error: str | None = None
        if timed_out:
            error = f"command timed out after {timeout:g}s; the sandbox killed the process tree"
        elif code != 0:
            error = f"command exited with code {code}"

        return SandboxResult(
            ok=(code == 0 and not timed_out),
            exit_code=code, stdout=stdout, stderr=stderr, tier=TIER,
            contained=contained, timed_out=timed_out, limit_terminated=terminated,
            env_scrubbed=True, error=error, notes=notes, duration_ms=duration,
            argv=list(argv))
    finally:
        # KILL_ON_JOB_CLOSE: closing the handle reaps any process still alive.
        k.CloseHandle(job)
