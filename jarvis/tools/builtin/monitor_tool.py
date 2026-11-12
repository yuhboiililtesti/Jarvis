"""One-shot system health monitoring: CPU, memory, disks, load, uptime and
the busiest processes.

This is the "monitor the computer" capability: a single call returns the
state an operator needs to decide whether the machine is healthy, without
starting a long-lived watcher. The runtime's own ``MonitoringConfig`` watcher
(see ``jarvis.monitoring``) remains the continuous sampler; this tool is the
on-demand snapshot.

psutil is imported lazily so the Core still starts without it.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import time
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register

__all__ = ["MonitorProbeTool"]


def health() -> dict[str, Any]:
    try:
        import psutil  # noqa: F401
        return {"available": True, "reason": "ok"}
    except ImportError:
        return {"available": False, "reason": "psutil is not installed"}


@register(["monitor.probe", "system.snapshot"])
class MonitorProbeTool(BaseTool):
    spec = ToolSpec(
        name="monitor.probe",
        summary="Snapshot system health: CPU, memory, disks, load, uptime and busiest processes.",
        category="monitoring",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("interval_s", "number",
                      "CPU sampling window in seconds (0-2).", default=0.3),
            ToolParam("top", "integer", "How many busiest processes to list.",
                      default=8),
            ToolParam("disks", "array",
                      "Paths to report disk usage for (defaults to home/cwd).",
                      default=None),
        ),
        returns="object with cpu, memory, swap, disks, load, uptime, net and top processes",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            import psutil
        except ImportError:
            return ToolResult(ok=False, error="unavailable: psutil is not installed")
        try:
            interval = max(0.0, min(float(kwargs.get("interval_s") or 0.3), 2.0))
            top = max(1, min(int(kwargs.get("top") or 8), 50))
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="interval_s/top have invalid values")
        disks = kwargs.get("disks") or [os.path.expanduser("~"), os.getcwd()]
        cpu_percent = psutil.cpu_percent(interval=interval)
        per_core = psutil.cpu_percent(interval=0, percpu=True)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk_rows = []
        for path in disks:
            try:
                usage = psutil.disk_usage(str(path))
                disk_rows.append({"path": str(path),
                                  "total_gb": round(usage.total / 1e9, 1),
                                  "used_percent": usage.percent,
                                  "free_gb": round(usage.free / 1e9, 1)})
            except OSError:
                continue
        procs = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                info = proc.info
                procs.append({"pid": info["pid"], "name": info.get("name"),
                              "cpu_percent": info.get("cpu_percent") or 0.0,
                              "memory_percent": round(
                                  info.get("memory_percent") or 0.0, 1)})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        top_cpu = sorted(procs, key=lambda p: p["cpu_percent"], reverse=True)[:top]
        top_mem = sorted(procs, key=lambda p: p["memory_percent"], reverse=True)[:top]
        try:
            load = list(os.getloadavg())
        except (AttributeError, OSError):
            load = None
        net = psutil.net_io_counters()
        data = {
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "uptime_s": round(time.time() - psutil.boot_time(), 1),
            "cpu": {"percent": cpu_percent,
                    "per_core": per_core,
                    "count": psutil.cpu_count(logical=True)},
            "memory": {"total_gb": round(mem.total / 1e9, 1),
                       "available_gb": round(mem.available / 1e9, 1),
                       "used_percent": mem.percent},
            "swap": {"used_percent": swap.percent},
            "disks": disk_rows,
            "load_avg": load,
            "net": {"sent_mb": round(net.bytes_sent / 1e6, 1),
                    "recv_mb": round(net.bytes_recv / 1e6, 1)},
            "top_cpu": top_cpu,
            "top_memory": top_mem,
            "process_count": len(procs),
        }
        health = "ok"
        if mem.percent >= 90 or cpu_percent >= 95:
            health = "critical"
        elif mem.percent >= 80 or cpu_percent >= 85:
            health = "warning"
        data["health"] = health
        return ToolResult(ok=True, data=data,
                          evidence={"method": "psutil-snapshot", "health": health})
