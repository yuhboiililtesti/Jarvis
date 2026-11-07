"""System introspection tools: host summary, live metrics and process table.

Everything here is read-only (SideEffect.NONE) so policy may auto-run it. A
metric this platform cannot supply is reported as unavailable rather than
faked: `system.metrics` degrades honestly when psutil lacks a sensor, and the
GPU probe is best-effort (no new dependency is added just to read it).
"""

from __future__ import annotations

import asyncio
import platform
import socket
from datetime import datetime, timezone
from typing import Any

import psutil

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.system_info")

__all__ = ["SystemInfoTool", "SystemMetricsTool", "SystemProcessesTool"]

# Previous network counters, so `system.metrics` can report live bytes/sec.
_NET_LAST: dict[str, tuple[float, int, int]] = {}


def _cpu_snapshot(interval: float = 0.1) -> dict[str, Any]:
    freq = None
    try:
        f = psutil.cpu_freq()
        if f is not None:
            freq = {"current_mhz": round(f.current, 1), "min_mhz": f.min,
                    "max_mhz": f.max}
    except Exception:  # pragma: no cover - platform dependent
        freq = None
    return {
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "percent": psutil.cpu_percent(interval=interval),
        "frequency": freq,
        "load_avg": list(psutil.getloadavg()) if hasattr(psutil, "getloadavg") else None,
    }


def _memory_snapshot() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "total_bytes": vm.total,
        "available_bytes": vm.available,
        "used_bytes": vm.used,
        "percent": vm.percent,
        "swap_total_bytes": sw.total,
        "swap_used_bytes": sw.used,
        "swap_percent": sw.percent,
    }


def _disks() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception as exc:  # pragma: no cover - platform dependent
        log.debug("disk_partitions failed: %s", exc)
        return out
    for part in parts:
        try:
            usage = psutil.disk_usage(part.mountpoint)
            out.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent": usage.percent,
            })
        except (PermissionError, OSError) as exc:
            log.debug("disk_usage(%s) unavailable: %s", part.mountpoint, exc)
    return out


# nvidia-smi CSV query for a rich per-GPU snapshot. ``nounits`` returns bare
# numbers so a value is parsed back to float; a field a card cannot report is
# returned as N/A and parsed to None (never faked as zero).
_GPU_QUERY = (
    "index,name,memory.total,memory.used,memory.free,utilization.gpu,"
    "temperature.gpu,power.draw,power.limit,fan.speed,clocks.gr,clocks.mem"
)
_GPU_PROCESS_QUERY = "pid,process_name,used_memory"


def _maybe_float(text: str) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _parse_gpu_csv(lines: list[str]) -> list[dict[str, Any]]:
    """Parse ``nvidia-smi --query-gpu=... --format=csv,noheader,nounits`` output."""
    out: list[dict[str, Any]] = []
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 12:
            continue
        idx = int(parts[0]) if parts[0].lstrip("-").isdigit() else None
        out.append({
            "index": idx,
            "name": parts[1],
            "memory_total_mib": _maybe_float(parts[2]),
            "memory_used_mib": _maybe_float(parts[3]),
            "memory_free_mib": _maybe_float(parts[4]),
            "utilization_percent": _maybe_float(parts[5]),
            "temperature_c": _maybe_float(parts[6]),
            "power_watts": _maybe_float(parts[7]),
            "power_limit_watts": _maybe_float(parts[8]),
            "fan_percent": _maybe_float(parts[9]),
            "clock_graphics_mhz": _maybe_float(parts[10]),
            "clock_memory_mhz": _maybe_float(parts[11]),
        })
    return out


def _parse_gpu_process_csv(lines: list[str]) -> list[dict[str, Any]]:
    """Parse ``nvidia-smi --query-compute-apps=...`` (compute processes on GPU)."""
    out: list[dict[str, Any]] = []
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        out.append({
            "pid": int(parts[0]) if parts[0].lstrip("-").isdigit() else None,
            "name": parts[1],
            "memory_used_mib": _maybe_float(parts[2]),
        })
    return out


def _run_nvidia_smi(args: list[str]) -> list[str]:
    import shutil
    import subprocess

    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True,
                              timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return (proc.stdout or "").strip().splitlines()


def _gpu_via_nvidia_smi() -> list[dict[str, Any]]:
    lines = _run_nvidia_smi(
        [f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"])
    return _parse_gpu_csv(lines)


def _gpu_processes_via_nvidia_smi() -> list[dict[str, Any]]:
    lines = _run_nvidia_smi(
        [f"--query-compute-apps={_GPU_PROCESS_QUERY}", "--format=csv,noheader,nounits"])
    return _parse_gpu_process_csv(lines)


def _gpu_via_pynvml() -> list[dict[str, Any]]:
    """Fallback GPU reader (no nvidia-smi); each field is read defensively."""
    try:
        import pynvml  # type: ignore
    except Exception:
        return []
    try:
        pynvml.nvmlInit()
        try:
            gpus: list[dict[str, Any]] = []
            for i in range(pynvml.nvmlDeviceGetCount()):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                entry: dict[str, Any] = {
                    "index": i, "name": name,
                    "memory_total_mib": round(mem.total / 1048576),
                    "memory_used_mib": round(mem.used / 1048576),
                    "memory_free_mib": round(mem.free / 1048576),
                }
                try:
                    entry["utilization_percent"] = float(
                        pynvml.nvmlDeviceGetUtilizationRates(handle).gpu)
                except Exception:
                    pass
                try:
                    entry["temperature_c"] = float(
                        pynvml.nvmlDeviceGetTemperature(
                            handle, pynvml.NVML_TEMPERATURE_GPU))
                except Exception:
                    pass
                try:
                    entry["power_watts"] = round(
                        pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 1)
                except Exception:
                    pass
                try:
                    entry["fan_percent"] = float(pynvml.nvmlDeviceGetFanSpeed(handle))
                except Exception:
                    pass
                gpus.append(entry)
        finally:
            pynvml.nvmlShutdown()
        return gpus
    except Exception:
        return []


def _gpu_snapshot() -> dict[str, Any]:
    """GPU section for the system tools: nvidia-smi first, pynvml fallback.

    Reports ``available: False`` (with a reason) rather than pretending there is
    no GPU when no reader exists. Memory/clock/thermal fields a card cannot
    report are ``None``, never zero.
    """
    gpus = _gpu_via_nvidia_smi()
    if gpus:
        return {"available": True, "source": "nvidia-smi", "gpus": gpus,
                "processes": _gpu_processes_via_nvidia_smi()}
    gpus = _gpu_via_pynvml()
    if gpus:
        return {"available": True, "source": "pynvml", "gpus": gpus,
                "processes": []}
    return {"available": False,
            "reason": "no NVIDIA GPU reader available (nvidia-smi or pynvml)"}


def _battery_snapshot() -> dict[str, Any] | None:
    """Battery state, or None when the platform exposes no battery."""
    if not hasattr(psutil, "sensors_battery"):
        return None
    try:
        b = psutil.sensors_battery()
    except Exception:  # pragma: no cover - platform dependent
        return None
    if b is None:
        return None
    return {"percent": b.percent, "power_plugged": b.power_plugged,
            "seconds_left": b.secsleft}


@register(["sysinfo", "system_info", "sysinfo.get"])
class SystemInfoTool(BaseTool):
    spec = ToolSpec(
        name="system.info",
        summary="Summary of this host: OS, CPU, RAM, disks and GPU.",
        category="system",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object with os, cpu, memory, disks and gpu sections",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            uname = platform.uname()
            data: dict[str, Any] = {
                "hostname": socket.gethostname(),
                "os": {
                    "system": uname.system,
                    "release": uname.release,
                    "version": uname.version,
                    "machine": uname.machine,
                    "processor": uname.processor or platform.processor(),
                    "python": platform.python_version(),
                },
                "boot_time": datetime.fromtimestamp(
                    psutil.boot_time(), tz=timezone.utc).isoformat(),
                "cpu": _cpu_snapshot(),
                "memory": _memory_snapshot(),
                "disks": _disks(),
                "gpu": _gpu_snapshot(),
            }
            return ToolResult(ok=True, data=data)
        except Exception as exc:  # never raise out of run()
            log.warning("system.info failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["metrics", "sysmetrics"])
class SystemMetricsTool(BaseTool):
    spec = ToolSpec(
        name="system.metrics",
        summary="Live CPU/memory/disk/network/GPU/temperature/battery snapshot.",
        category="system",
        side_effect=SideEffect.NONE,
        params=(),
        returns="object with cpu, memory, disks, network, gpu, battery and temperatures",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            net = self._network()
            temps: dict[str, Any] | None = None
            if hasattr(psutil, "sensors_temperatures"):
                try:
                    raw = psutil.sensors_temperatures() or {}
                    temps = {
                        name: [{"label": t.label, "current_c": t.current,
                                "high_c": t.high, "critical_c": t.critical}
                               for t in entries]
                        for name, entries in raw.items()
                    }
                except Exception as exc:  # pragma: no cover - platform dependent
                    log.debug("sensors_temperatures failed: %s", exc)
                    temps = None
            data: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "cpu": _cpu_snapshot(),
                "memory": _memory_snapshot(),
                "disks": _disks(),
                "network": net,
                "gpu": _gpu_snapshot(),
                "battery": _battery_snapshot(),
                "temperatures": temps,
                "temperatures_available": temps is not None,
            }
            if temps is None:
                data["temperatures_note"] = "this platform exposes no temperature sensors"
            return ToolResult(ok=True, data=data)
        except Exception as exc:
            log.warning("system.metrics failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _network() -> dict[str, Any]:
        counters = psutil.net_io_counters()
        now = datetime.now(timezone.utc).timestamp()
        out: dict[str, Any] = {
            "bytes_sent": counters.bytes_sent,
            "bytes_recv": counters.bytes_recv,
            "packets_sent": counters.packets_sent,
            "packets_recv": counters.packets_recv,
        }
        prev = _NET_LAST.get("default")
        if prev is not None:
            dt = now - prev[0]
            if dt > 0:
                out["sent_bytes_per_s"] = round((counters.bytes_sent - prev[1]) / dt, 1)
                out["recv_bytes_per_s"] = round((counters.bytes_recv - prev[2]) / dt, 1)
        _NET_LAST["default"] = (now, counters.bytes_sent, counters.bytes_recv)
        return out


@register(["ps", "processes", "top"])
class SystemProcessesTool(BaseTool):
    spec = ToolSpec(
        name="system.processes",
        summary="Top N running processes by CPU or memory use.",
        category="system",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("sort", "string", "Ranking key.", default="cpu",
                      enum=("cpu", "memory")),
            ToolParam("limit", "integer", "How many processes to return.", default=10),
        ),
        returns="object with a list of processes (pid, name, user, cpu, memory)",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            sort = str(kwargs.get("sort") or "cpu").lower()
            if sort not in ("cpu", "memory"):
                return ToolResult(ok=False, error="sort must be 'cpu' or 'memory'")
            try:
                limit = int(kwargs.get("limit") or 10)
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="limit must be an integer")
            limit = max(1, min(limit, 200))

            attrs = ["pid", "name", "username", "memory_info", "create_time"]
            # Prime per-process CPU counters, then sample again for a real value.
            for proc in psutil.process_iter(["pid"]):
                try:
                    proc.cpu_percent(interval=None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            await asyncio.sleep(0.1)

            rows: list[dict[str, Any]] = []
            for proc in psutil.process_iter(attrs):
                try:
                    info = proc.info
                    mem = info.get("memory_info")
                    cpu = proc.cpu_percent(interval=None)
                    rows.append({
                        "pid": info.get("pid"),
                        "name": info.get("name") or "",
                        "user": info.get("username"),
                        "cpu_percent": round(cpu, 1),
                        "memory_rss_bytes": getattr(mem, "rss", None),
                        "memory_percent": round(getattr(mem, "rss", 0)
                                                / psutil.virtual_memory().total * 100, 2)
                        if mem else None,
                        "create_time": info.get("create_time"),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

            key = "cpu_percent" if sort == "cpu" else "memory_rss_bytes"
            rows.sort(key=lambda r: (r.get(key) or 0), reverse=True)
            selected = rows[:limit]
            return ToolResult(
                ok=True,
                data={"sort": sort, "count": len(selected),
                      "total_seen": len(rows), "processes": selected},
            )
        except Exception as exc:
            log.warning("system.processes failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
