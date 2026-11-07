"""Self-diagnostics and narrow, audited repairs.

JARVIS should notice its own broken dependencies before the owner does. This
service runs a small set of checks on an interval and reports them honestly:

  * the model provider (Ollama) reachable;
  * the runtime loop and the goals service;
  * the audit chain verifies;
  * the sandbox tier;
  * database integrity;
  * disk headroom;
  * each enrolled node (the laptop, over its tunnel).

The only repair it performs automatically is restarting the **SSH tunnel** when a
local node is unreachable. That is a bounded, audited, cooldown-limited action;
it never starts the model server or changes a setting. Everything else is a
reported `repair` instruction for the owner.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..ids import now_iso
from ..logging_setup import get_logger
from ..security.audit_chain import verify as verify_audit

log = get_logger("jarvis.ops")

__all__ = ["OpsService"]


class OpsService:
    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self.config = kernel.config
        self.cfg = kernel.config.ops
        self.db = kernel.db
        self.bus = kernel.bus
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._last: dict[str, Any] = {"ok": None, "checks": [], "ts": None}
        self._last_repair = 0.0
        self._last_healthy: bool | None = None

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if not self.cfg.enabled:
            log.info("ops diagnostics disabled by config")
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="jarvis-ops")
        log.info("ops diagnostics started (every %.0fs)", self.cfg.interval_s)

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        log.info("ops diagnostics stopped")

    async def _loop(self) -> None:
        while not (self._stop and self._stop.is_set()):
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failed check must not stop the loop
                log.warning("ops check failed: %s", exc)
            try:
                assert self._stop is not None
                await asyncio.wait_for(self._stop.wait(), timeout=float(self.cfg.interval_s))
            except asyncio.TimeoutError:
                continue

    # -- checks ----------------------------------------------------------
    async def check(self) -> dict[str, Any]:
        started = time.perf_counter()
        checks: list[dict[str, Any]] = []
        repaired: list[dict[str, Any]] = []

        llm = await self._check_llm()
        checks.append(llm)

        checks.append(self._check_runtime())
        checks.append(self._check_goals())
        checks.append(self._check_audit())
        checks.append(self._check_sandbox())
        checks.append(self._check_db())
        checks.append(self._check_disk())

        node_checks = await self._check_nodes()
        checks.extend(node_checks)
        if self.cfg.auto_repair_tunnel:
            for item in node_checks:
                if item.get("tunnel") and not item["ok"]:
                    outcome = await self._repair_tunnel(item["name"])
                    if outcome is not None:
                        repaired.append(outcome)
                        # Observe the result of the repair, not the intent.
                        item["ok"] = bool(outcome.get("ok"))
                        item["detail"] = ("tunnel restarted; node reachable"
                                          if outcome.get("ok")
                                          else f"tunnel restart failed: {outcome.get('error')}")

        ok = all(item.get("ok") for item in checks)
        result = {
            "ts": now_iso(),
            "ok": ok,
            "checks": checks,
            "repaired": repaired,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }
        self._last = result
        if self._last_healthy is not None and self._last_healthy != ok:
            self.bus.publish("ops.state", ok=ok,
                             failures=[c["name"] for c in checks if not c.get("ok")])
            self.db.audit("ops.state", detail={
                "ok": ok,
                "failures": [c["name"] for c in checks if not c.get("ok")]},
                severity="info" if ok else "warn")
        self._last_healthy = ok
        return result

    async def _check_llm(self) -> dict[str, Any]:
        try:
            health = await asyncio.wait_for(self.kernel.router.health(), timeout=10.0)
        except Exception as exc:
            return {"name": "llm", "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                    "repair": "start Ollama (ollama serve) and pull the model"}
        ok = bool(health.get("ok"))
        return {"name": "llm", "ok": ok,
                "detail": f"{health.get('provider', '?')} {health.get('model', '')}".strip()
                          or ("reachable" if ok else str(health.get("error") or "unreachable")),
                "repair": None if ok else "start Ollama (ollama serve)"}

    def _check_runtime(self) -> dict[str, Any]:
        svc = (self.kernel.services or {}).get("runtime")
        if svc is None:
            return {"name": "runtime", "ok": False, "detail": "service not loaded",
                    "repair": "restart the Core"}
        h = svc.health()
        return {"name": "runtime", "ok": bool(h.get("available")),
                "detail": h.get("reason", ""), "repair": None if h.get("available") else "restart the Core"}

    def _check_goals(self) -> dict[str, Any]:
        svc = (self.kernel.services or {}).get("goals")
        if svc is None:
            return {"name": "goals", "ok": False, "detail": "service not loaded",
                    "repair": "restart the Core"}
        h = svc.health()
        return {"name": "goals", "ok": bool(h.get("available")),
                "detail": h.get("reason", ""), "repair": None}

    def _check_audit(self) -> dict[str, Any]:
        try:
            report = verify_audit(self.db, self.config.data_path)
        except Exception as exc:
            return {"name": "audit", "ok": False, "detail": f"{type(exc).__name__}: {exc}",
                    "repair": "run `jarvis audit` and inspect the break"}
        return {"name": "audit", "ok": bool(report.get("ok")),
                "detail": report.get("reason") or f"chain ok ({report.get('checked', 0)} rows)",
                "repair": None if report.get("ok") else "run `jarvis audit` and inspect the break"}

    def _check_sandbox(self) -> dict[str, Any]:
        svc = (self.kernel.services or {}).get("sandbox")
        if svc is None:
            return {"name": "sandbox", "ok": False, "detail": "service not loaded",
                    "repair": "restart the Core"}
        status = svc.status()
        ok = bool(status.get("contained")) or not status.get("enabled")
        return {"name": "sandbox", "ok": ok,
                "detail": f"tier {status.get('active_tier')} | contained={status.get('contained')}",
                "repair": None if ok else "check `jarvis sandbox` on this platform"}

    def _check_db(self) -> dict[str, Any]:
        try:
            row = self.db.query_one("PRAGMA integrity_check")
            value = row["integrity_check"] if row else "unknown"
        except Exception as exc:
            return {"name": "database", "ok": False, "detail": str(exc),
                    "repair": "restore from the latest checkpoint"}
        return {"name": "database", "ok": value == "ok", "detail": str(value),
                "repair": None if value == "ok" else "restore from the latest checkpoint"}

    def _check_disk(self) -> dict[str, Any]:
        try:
            import psutil
            usage = psutil.disk_usage(str(self.config.data_path))
            free_pct = round(100.0 - usage.percent, 1)
            ok = free_pct >= 10.0
            return {"name": "disk", "ok": ok,
                    "detail": f"{free_pct}% free on the data volume",
                    "repair": None if ok else "free disk space; JARVIS needs headroom"}
        except Exception as exc:  # psutil optional
            return {"name": "disk", "ok": True,
                    "detail": f"not measured ({type(exc).__name__})", "repair": None}

    async def _check_nodes(self) -> list[dict[str, Any]]:
        svc = (self.kernel.services or {}).get("nodes")
        if svc is None:
            return []
        try:
            nodes = await asyncio.wait_for(svc.list_nodes(), timeout=5.0)
        except Exception as exc:
            return [{"name": "nodes", "ok": False, "detail": str(exc), "repair": None}]
        out: list[dict[str, Any]] = []
        for node in nodes:
            node_id = str(node.get("id") or "")
            name = f"node:{node.get('name') or node_id}"
            tunnel = str(node.get("base_url") or "").startswith("http://127.0.0.1:2226")
            try:
                ping = await asyncio.wait_for(svc.ping(node_id), timeout=10.0)
            except Exception as exc:
                ping = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ok = bool(ping.get("ok"))
            out.append({
                "name": name, "ok": ok, "tunnel": tunnel,
                "detail": (f"online ({ping.get('latency_ms')} ms)" if ok
                           else str(ping.get("error") or "unreachable")),
                "repair": None if ok else ("restart the SSH tunnel" if tunnel
                                           else "check the node and its address"),
            })
        return out

    async def _repair_tunnel(self, name: str) -> dict[str, Any] | None:
        """Restart the local SSH tunnel, with a cooldown. Windows only."""
        if os.name != "nt":
            return None
        now = time.time()
        cooldown = float(self.cfg.repair_cooldown_s)
        if now - self._last_repair < cooldown:
            return None
        self._last_repair = now
        script = Path(PROJECT_ROOT) / "scripts" / "node-tunnel.ps1"
        if not script.is_file():
            return {"name": name, "ok": False, "error": "scripts/node-tunnel.ps1 is missing"}
        sb = (self.kernel.services or {}).get("sandbox")
        if sb is None:
            return {"name": name, "ok": False, "error": "sandbox service unavailable"}
        argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script), "-Start"]
        try:
            result = await sb.run_async(argv, timeout=45)
        except Exception as exc:
            return {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ok = bool(result.ok)
        self.db.audit("ops.repair_tunnel", target=name,
                      detail={"ok": ok, "exit": result.exit_code,
                              "error": result.error},
                      severity="info" if ok else "warn")
        return {"name": name, "ok": ok, "tier": result.tier,
                "error": result.error if not ok else None}

    # -- reporting -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return dict(self._last)

    def health(self) -> dict[str, Any]:
        if not self.cfg.enabled:
            return {"available": True, "reason": "ops diagnostics disabled by config"}
        last = self._last
        if last.get("ok") is None:
            return {"available": True, "reason": "no check has run yet"}
        failing = [c["name"] for c in last.get("checks", []) if not c.get("ok")]
        if failing:
            return {"available": False,
                    "reason": "failing: " + ", ".join(failing)}
        return {"available": True, "reason": f"all {len(last.get('checks', []))} checks pass"}
