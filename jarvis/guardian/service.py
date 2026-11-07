"""Guardian service for monitoring and preauthorized autonomous repairs."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from ..ids import now_iso
from ..logging_setup import get_logger

log = get_logger("jarvis.guardian")


class GuardianService:
    """Autonomous recovery with guardrails."""

    def __init__(self, jarvis: Any = None) -> None:
        self.jarvis = jarvis
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._incidents: list[dict[str, Any]] = []
        self._recovery_counts: dict[str, int] = {}
        self._last_recovery: dict[str, datetime] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="guardian.loop")
        log.info("guardian started")

    async def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stop must not raise
                pass
            self._task = None
        log.info("guardian stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check()
            except Exception as exc:
                log.warning("guardian check failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                continue

    async def check(self) -> dict[str, Any]:
        """Check system health and perform preauthorized repairs."""
        issues = []
        # Check monitoring service
        monitoring = getattr(self.jarvis, "services", {}).get("monitoring") if self.jarvis else None
        if monitoring and hasattr(monitoring, "sample_once"):
            try:
                sample = await monitoring.sample_once()
                disk_pct = sample.get("readings", {}).get("disk_percent", 0)
                if disk_pct > 90:
                    issues.append({"type": "disk_high", "severity": "warning", "value": disk_pct})
                mem_pct = sample.get("readings", {}).get("mem_percent", 0)
                if mem_pct > 95:
                    issues.append({"type": "mem_high", "severity": "critical", "value": mem_pct})
            except Exception as exc:
                issues.append({"type": "monitoring_error", "error": str(exc)})

        # Check nodes
        nodes = getattr(self.jarvis, "services", {}).get("nodes") if self.jarvis else None
        if nodes and hasattr(nodes, "list_nodes"):
            try:
                node_list = await nodes.list_nodes()
                for node in node_list:
                    if node.get("status") not in ("online", "enrolled"):
                        issues.append({"type": "node_offline", "node_id": node.get("id"), "status": node.get("status")})
            except Exception as exc:
                issues.append({"type": "nodes_error", "error": str(exc)})

        # Attempt preauthorized repairs
        for issue in issues:
            await self._attempt_repair(issue)

        return {"issues": issues, "timestamp": now_iso()}

    async def _attempt_repair(self, issue: dict[str, Any]) -> None:
        issue_type = issue.get("type")
        key = issue_type

        # Rate limiting: max 3 attempts per hour
        now = datetime.now()
        count = self._recovery_counts.get(key, 0)
        last = self._last_recovery.get(key)
        if last and (now - last) < timedelta(hours=1) and count >= 3:
            log.warning("recovery rate limited for %s", issue_type)
            return

        self._recovery_counts[key] = count + 1
        self._last_recovery[key] = now

        if issue_type == "node_offline":
            # Can ping to check, but restart requires authorization
            log.info("node offline detected, will alert")
        elif issue_type == "disk_high":
            log.info("disk high usage detected, monitoring")
        elif issue_type == "mem_high":
            log.info("memory high usage detected")

        self._incidents.append({
            "issue": issue,
            "timestamp": now_iso(),
            "action_taken": "monitored"
        })

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "incidents": len(self._incidents),
            "recovery_counts": self._recovery_counts
        }
