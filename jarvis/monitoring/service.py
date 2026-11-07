"""System monitoring: periodic sampling, metric history and threshold alerts.

Samples CPU, memory, disk, network, load and (best effort) GPU + temperature
into the ``metrics`` table, publishes bus events and raises alerts when a
threshold from :class:`~jarvis.config.MonitoringConfig` is exceeded. Threshold
alerts respect a per-condition cooldown and the configured quiet hours.

Honesty rules baked in here:

  * ``psutil`` is optional. If it is missing every sample reports what it could
    not read instead of inventing a number.
  * GPU and temperature are best-effort. An absent sensor is recorded as
    unavailable, never as zero.
  * ``create_alert`` records which delivery channels actually reached the user.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..ids import now_iso
from ..logging_setup import get_logger
from . import alerts as alerts_mod

try:  # psutil is a declared dependency but the Core must start without it
    import psutil  # type: ignore
except Exception:  # pragma: no cover - exercised only when psutil is absent
    psutil = None  # type: ignore

log = get_logger("jarvis.monitoring.service")

__all__ = ["MonitoringService"]

_GPU_QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu"


class MonitoringService:
    """Owner is always able to ask what the machine is doing right now."""

    def __init__(self, jarvis: Any) -> None:
        self.j = jarvis
        self.config = getattr(jarvis, "config", None)
        self.db = getattr(jarvis, "db", None)
        self.bus = getattr(jarvis, "bus", None)
        self.mon = getattr(getattr(jarvis, "config", None), "monitoring", None)

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._running = False
        self._last_sample: dict[str, Any] | None = None
        self._last_sample_at: str | None = None
        self._last_net: dict[str, float] = {}
        self._last_net_at: float | None = None
        self._primed = False

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if self.mon is None or not getattr(self.mon, "enabled", True):
            self.bus and self.bus.status("monitoring", "disabled")
            log.info("monitoring disabled by config")
            return
        if self._running:
            return
        # prime cpu_percent so the first real sample is meaningful
        self._prime()
        self._running = True
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="monitoring.sample")
        self.bus and self.bus.status("monitoring", "started",
                                     interval_s=getattr(self.mon, "interval_s", 15.0))
        log.info("monitoring started (interval %ss, psutil=%s)",
                 getattr(self.mon, "interval_s", 15.0), psutil is not None)

    async def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stop must not raise
                pass
            self._task = None
        self.bus and self.bus.status("monitoring", "stopped")

    # -- main loop -------------------------------------------------------
    async def _loop(self) -> None:
        interval = float(getattr(self.mon, "interval_s", 15.0) or 15.0)
        interval = max(1.0, interval)
        while not self._stop.is_set():
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a sample failure must never kill the loop
                log.warning("sample failed: %s", exc)
                self.bus and self.bus.degrade("monitoring", f"sample failed: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    def _prime(self) -> None:
        if psutil is not None and not self._primed:
            try:
                psutil.cpu_percent(interval=None)
                self._primed = True
            except Exception:  # pragma: no cover - defensive
                pass

    # -- sampling --------------------------------------------------------
    async def sample_once(self) -> dict[str, Any]:
        """Collect one round of metrics, persist them and evaluate thresholds."""
        ts = now_iso()
        samples: dict[str, float] = {}
        readings: dict[str, Any] = {}
        unavailable: list[str] = []

        if psutil is None:
            unavailable.append("psutil: not installed")
            result = {"ts": ts, "samples": samples, "raw": {"unavailable": unavailable},
                      "alerts": [], "suppressed": []}
            self._last_sample = result
            self._last_sample_at = ts
            return result

        # CPU -----------------------------------------------------------
        try:
            cpu = float(psutil.cpu_percent(interval=None))
            samples["cpu.percent"] = cpu
            readings["cpu_percent"] = cpu
        except Exception as exc:  # pragma: no cover - defensive
            unavailable.append(f"cpu: {exc}")

        # Memory --------------------------------------------------------
        try:
            vm = psutil.virtual_memory()
            samples["mem.percent"] = float(vm.percent)
            samples["mem.used_bytes"] = float(vm.used)
            samples["mem.total_bytes"] = float(vm.total)
            readings["mem_percent"] = float(vm.percent)
        except Exception as exc:  # pragma: no cover
            unavailable.append(f"mem: {exc}")

        # Disk (data drive + system drive) -----------------------------
        disk_percent: float | None = None
        for idx, path in enumerate(self._disk_targets()):
            try:
                usage = psutil.disk_usage(path)
            except Exception as exc:  # pragma: no cover
                unavailable.append(f"disk {path}: {exc}")
                continue
            samples[f"disk.percent.{idx}"] = float(usage.percent)
            readings.setdefault("disks", []).append(
                {"path": path, "percent": float(usage.percent),
                 "free_bytes": int(usage.free), "total_bytes": int(usage.total)})
            if disk_percent is None or float(usage.percent) > disk_percent:
                disk_percent = float(usage.percent)
        if disk_percent is not None:
            samples["disk.percent"] = disk_percent
            readings["disk_percent"] = disk_percent

        # Load ----------------------------------------------------------
        try:
            load = psutil.getloadavg()
            samples["load.1"], samples["load.5"], samples["load.15"] = (
                float(load[0]), float(load[1]), float(load[2]))
            readings["load"] = [float(x) for x in load]
        except Exception:
            unavailable.append("load: not available on this platform")

        # Network -------------------------------------------------------
        try:
            net = psutil.net_io_counters()
            samples["net.bytes_sent"] = float(net.bytes_sent)
            samples["net.bytes_recv"] = float(net.bytes_recv)
            now_mono = time.monotonic()
            if self._last_net and self._last_net_at is not None:
                dt = now_mono - self._last_net_at
                if dt > 0:
                    rate_sent = max(0.0, (net.bytes_sent - self._last_net.get("sent", 0)) / dt)
                    rate_recv = max(0.0, (net.bytes_recv - self._last_net.get("recv", 0)) / dt)
                    samples["net.rate_sent_bps"] = rate_sent
                    samples["net.rate_recv_bps"] = rate_recv
                    readings["net_rate"] = {"sent_bps": rate_sent, "recv_bps": rate_recv}
            self._last_net = {"sent": float(net.bytes_sent), "recv": float(net.bytes_recv)}
            self._last_net_at = now_mono
        except Exception as exc:  # pragma: no cover
            unavailable.append(f"net: {exc}")

        # GPU (best effort, subprocess) ---------------------------------
        gpu = await self._sample_gpu()
        if gpu:
            readings["gpus"] = gpu
            for i, g in enumerate(gpu):
                for key in ("utilization_percent", "memory_used_mib",
                            "memory_total_mib", "temperature_c"):
                    if g.get(key) is not None:
                        samples[f"gpu.{i}.{key}"] = float(g[key])
                if g.get("temperature_c") is not None:
                    readings.setdefault("temperatures", []).append(
                        {"sensor": f"gpu{i}", "current_c": float(g["temperature_c"])})
        else:
            unavailable.append("gpu: nvidia-smi unavailable or no GPU")

        # Temperature (best effort, psutil) ----------------------------
        temp_max: float | None = None
        try:
            sensors = getattr(psutil, "sensors_temperatures", None)
            if sensors:
                data = sensors()
                for name, entries in (data or {}).items():
                    for e in entries:
                        cur = getattr(e, "current", None)
                        if cur is None:
                            continue
                        samples[f"temp.{name}"] = float(cur)
                        readings.setdefault("temperatures", []).append(
                            {"sensor": name, "label": getattr(e, "label", ""),
                             "current_c": float(cur)})
                        if temp_max is None or float(cur) > temp_max:
                            temp_max = float(cur)
                if not data:
                    unavailable.append("temperature: no sensors exposed")
            else:
                unavailable.append("temperature: psutil sensors unavailable on this platform")
        except Exception as exc:
            unavailable.append(f"temperature: {exc}")
        if temp_max is not None:
            readings["temp_max_c"] = temp_max

        # Persist -------------------------------------------------------
        self._write_metrics(ts, samples, readings)

        # Per-node telemetry (best effort; a down node is skipped, never fatal).
        try:
            await self._sample_nodes()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("node sampling failed: %s", exc)

        # Threshold alerts ---------------------------------------------
        raised: list[dict[str, Any]] = []
        suppressed: list[dict[str, Any]] = []
        checks = [
            ("cpu", "cpu.percent", readings.get("cpu_percent"),
             getattr(self.mon, "cpu_warn", 90.0), "CPU", "%"),
            ("mem", "mem.percent", readings.get("mem_percent"),
             getattr(self.mon, "mem_warn", 90.0), "Memory", "%"),
            ("disk", "disk.percent", readings.get("disk_percent"),
             getattr(self.mon, "disk_warn", 90.0), "Disk", "%"),
            ("temp", "temp.max", temp_max,
             getattr(self.mon, "temp_warn_c", 85.0), "Temperature", "C"),
        ]
        for key, metric, value, warn, label, unit in checks:
            if value is None or float(value) < float(warn):
                continue
            entry = {"key": key, "metric": metric, "value": value, "threshold": warn}
            allowed, reason = alerts_mod.should_raise(
                self.db,
                key=alerts_mod.alert_key("monitoring", key),
                cooldown_s=float(getattr(self.mon, "alert_cooldown_s", 600.0) or 600.0),
                quiet_hours=getattr(self.mon, "quiet_hours", None),
            )
            if not allowed:
                entry["reason"] = reason
                suppressed.append(entry)
                continue
            severity = "critical" if float(value) >= float(warn) + 5 else "warn"
            body = f"{label} at {float(value):.1f}{unit} (threshold {float(warn):.1f}{unit})."
            self.create_alert(severity, "monitoring",
                              f"{label} {'critical' if severity == 'critical' else 'high'}",
                              body,
                              {"metric": metric, "value": float(value),
                               "threshold": float(warn), "ts": ts})
            alerts_mod.mark_cooldown(self.db, alerts_mod.alert_key("monitoring", key))
            raised.append(entry)

        result = {
            "ts": ts,
            "samples": samples,
            "raw": readings,
            "unavailable": unavailable,
            "alerts": raised,
            "suppressed": suppressed,
        }
        self._last_sample = result
        self._last_sample_at = ts
        if self.bus is not None:
            try:
                self.bus.publish("metrics", ts=ts, samples=samples,
                                 unavailable=unavailable,
                                 alerts=[a["key"] for a in raised])
            except Exception:  # pragma: no cover - a subscriber must not break sampling
                pass
        return result

    # -- alerts ----------------------------------------------------------
    def create_alert(self, severity: str, source: str, title: str, body: str,
                     evidence: Any = None, **kwargs: Any) -> str:
        """Insert + publish an alert and record honest delivery status."""
        return alerts_mod.create_alert(self.db, self.bus, severity, source, title,
                                       body, evidence, **kwargs)

    # -- reads -----------------------------------------------------------
    async def metrics_history(self, name: str | None = None,
                              limit: int = 200) -> list[dict[str, Any]]:
        """Recent metric rows, newest last, for the UI charts."""
        limit = max(1, min(int(limit or 200), 5000))
        if name:
            rows = self.db.query(
                "SELECT ts,name,value,label FROM metrics WHERE name=? "
                "ORDER BY id DESC LIMIT ?", (name, limit))
        else:
            rows = self.db.query(
                "SELECT ts,name,value,label FROM metrics "
                "ORDER BY id DESC LIMIT ?", (limit,))
        out = [{"ts": r["ts"], "name": r["name"], "value": r["value"],
                "label": r["label"]} for r in rows]
        out.reverse()
        return out

    def latest(self) -> dict[str, Any] | None:
        return self._last_sample

    def alerts(self, limit: int = 50, *, only_unack: bool = False) -> list[dict[str, Any]]:
        return alerts_mod.recent_alerts(self.db, limit, only_unack=only_unack)

    def acknowledge(self, alert_id: str) -> bool:
        return alerts_mod.acknowledge(self.db, alert_id)

    # -- status ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        unack = 0
        try:
            unack = alerts_mod.unacknowledged_count(self.db)
        except Exception:  # pragma: no cover
            pass
        quiet = False
        try:
            quiet = alerts_mod.in_quiet_hours(getattr(self.mon, "quiet_hours", None))
        except Exception:  # pragma: no cover
            pass
        return {
            "component": "monitoring",
            "enabled": bool(getattr(self.mon, "enabled", True)),
            "running": self._running,
            "psutil": psutil is not None,
            "interval_s": float(getattr(self.mon, "interval_s", 15.0) or 15.0),
            "last_sample_at": self._last_sample_at,
            "last_sample": self._last_sample,
            "unacknowledged_alerts": unack,
            "quiet_hours_active": quiet,
            "thresholds": {
                "cpu_warn": getattr(self.mon, "cpu_warn", 90.0),
                "mem_warn": getattr(self.mon, "mem_warn", 90.0),
                "disk_warn": getattr(self.mon, "disk_warn", 90.0),
                "temp_warn_c": getattr(self.mon, "temp_warn_c", 85.0),
                "alert_cooldown_s": getattr(self.mon, "alert_cooldown_s", 600.0),
                "quiet_hours": list(getattr(self.mon, "quiet_hours", []) or []),
            },
        }

    # -- internals -------------------------------------------------------
    def _write_metrics(self, ts: str, samples: dict[str, float],
                       readings: dict[str, Any]) -> None:
        if not samples:
            return
        rows: list[tuple[str, str, float, str | None]] = []
        for name, value in samples.items():
            label = self._label_for(name, readings)
            rows.append((ts, name, float(value), label))
        try:
            with self.db.tx() as conn:
                conn.executemany(
                    "INSERT INTO metrics(ts,name,value,label) VALUES(?,?,?,?)", rows)
        except Exception as exc:  # pragma: no cover - persistence must not crash loop
            log.warning("metric write failed: %s", exc)

    async def _sample_nodes(self) -> int:
        """Poll enrolled nodes' live metrics and persist node.<id>.<metric> rows.

        Uses the ``metrics`` node task. Each poll is guarded by a short timeout so
        a hung node cannot stall the sample loop; a node that lacks the task or is
        down is skipped (honest absence, not a fake zero).
        """
        if not bool(getattr(self.mon, "poll_nodes", True)):
            return 0
        nodes_svc = (getattr(self.j, "services", None) or {}).get("nodes")
        if nodes_svc is None or not hasattr(nodes_svc, "send_task"):
            return 0
        known = getattr(getattr(self.config, "nodes", None), "known", {}) or {}
        if not known:
            return 0

        ts = now_iso()
        rows: list[tuple[str, str, float, str | None]] = []
        for node_id in list(known):
            try:
                resp = await asyncio.wait_for(
                    nodes_svc.send_task(node_id, "metrics", {}), timeout=5.0)
            except Exception as exc:
                log.debug("node %s metrics poll failed: %s", node_id, exc)
                continue
            if not isinstance(resp, dict) or not resp.get("ok"):
                continue
            data = resp.get("data") or {}
            for key in ("cpu_percent", "mem_percent", "load_1", "disk_percent",
                        "mem_used_bytes", "mem_total_bytes"):
                val = data.get(key)
                if val is None:
                    continue
                try:
                    rows.append((ts, f"node.{node_id}.{key}", float(val), node_id))
                except (TypeError, ValueError):
                    continue
        if rows:
            try:
                with self.db.tx() as conn:
                    conn.executemany(
                        "INSERT INTO metrics(ts,name,value,label) VALUES(?,?,?,?)", rows)
            except Exception as exc:  # pragma: no cover - must not crash the loop
                log.warning("node metric write failed: %s", exc)
        return len(rows)

    @staticmethod
    def _label_for(name: str, readings: dict[str, Any]) -> str | None:
        if name.startswith("disk.percent."):
            try:
                idx = int(name.rsplit(".", 1)[1])
                disks = readings.get("disks") or []
                if idx < len(disks):
                    return disks[idx]["path"]
            except (ValueError, IndexError, KeyError):
                return None
        if name.startswith("gpu."):
            return "gpu"
        if name.startswith("net."):
            return "total"
        return None

    def _disk_targets(self) -> list[str]:
        targets: list[str] = []
        data_dir = None
        try:
            data_dir = str(self.config.data_path)
        except Exception:
            data_dir = None
        for path in (data_dir, str(getattr(self.db, "path", "")) or None):
            if path and path not in targets:
                targets.append(path)
        return targets or ["/"]

    async def _sample_gpu(self) -> list[dict[str, Any]]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(_gpu_via_nvidia_smi), timeout=3.0)
        except Exception as exc:
            log.debug("gpu sample skipped: %s", exc)
            return []


def _gpu_via_nvidia_smi() -> list[dict[str, Any]]:
    """Best-effort GPU read via nvidia-smi. Returns [] when unavailable."""
    import shutil
    import subprocess

    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2.5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[dict[str, Any]] = []
    for line in (proc.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        out.append({
            "utilization_percent": _maybe_float(parts[0]),
            "memory_used_mib": _maybe_float(parts[1]),
            "memory_total_mib": _maybe_float(parts[2]),
            "temperature_c": _maybe_float(parts[3]),
        })
    return out


def _maybe_float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None
