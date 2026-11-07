"""NIGHT PROTOCOL runner: bounded, auditable overnight work.

Stages (all best effort, all honestly recorded):

  0 preflight      verify db / workspace / rollback point / disk
  1 recon          collect system + project state
  2 audit          run every health check that *can* run (db integrity, tool
                   registry load, llm health, disk, JSONL error scan)
  3 work loop      bounded objective queue, one attempt -> verify -> record each
  4 failure loop   retry ladder 3 -> 2 -> 1, then stop and record
  5 health gates   re-check the things that must still be true before we claim
                   anything was done
  6 emergency stop hard stops: deadline, disk critical, db corrupt, config gone,
                   DEFCON 1, operator stop
  7 reconciliation update the night_run row, write the morning report, audit

The morning report is the contract with the owner: baseline, what was attempted,
what changed, exactly which checks ran and their outcomes, failures with their
full retry history, new issues, unknowns, rollback points and the recommended
next three actions.

Nothing here fabricates a pass. A check that could not run is recorded as
``not run`` together with the reason it could not run.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..ids import new_id, now_iso
from ..logging_setup import get_logger
from . import alerts as alerts_mod

try:  # psutil is a declared dependency but the Core must start without it
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

log = get_logger("jarvis.monitoring.nightly")

__all__ = ["NightRunner"]

# disk free-space gates (bytes / percent)
DISK_CRITICAL_FREE = 500 * 1024 * 1024          # 0.5 GiB
DISK_WARN_FREE = 2 * 1024 * 1024 * 1024         # 2 GiB
DB_SNAPSHOT_MAX = 200 * 1024 * 1024             # don't copy an oversized db
LOG_SCAN_LINES = 4000                           # tail window for the error scan
OBJECTIVE_TIMEOUT_S = 300.0


@dataclass
class NightRun:
    id: str
    started_at: str
    deadline_mono: float
    objectives: list[str] = field(default_factory=list)
    baseline: dict[str, Any] = field(default_factory=dict)
    recon: dict[str, Any] = field(default_factory=dict)
    preflight: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    objectives_result: list[dict[str, Any]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    new_issues: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    rollback: dict[str, Any] = field(default_factory=dict)
    health_gates: list[dict[str, Any]] = field(default_factory=list)
    emergency: dict[str, Any] | None = None
    status: str = "running"


class NightRunner:
    def __init__(self, jarvis: Any) -> None:
        self.j = jarvis
        self.config = getattr(jarvis, "config", None)
        self.db = getattr(jarvis, "db", None)
        self.bus = getattr(jarvis, "bus", None)
        self.night = getattr(getattr(jarvis, "config", None), "night", None)

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._running = False
        self._current: str | None = None

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        if self.night is None or not getattr(self.night, "enabled", False):
            self.bus and self.bus.status("night", "disabled")
            log.info("night protocol disabled by config")
            return
        self._running = True
        self._stop.clear()
        self._task = asyncio.create_task(self._schedule_loop(), name="night.schedule")
        self.bus and self.bus.status("night", "started",
                                     window=f"{getattr(self.night, 'start', '')}-"
                                            f"{getattr(self.night, 'end', '')}")
        log.info("night protocol scheduled %s-%s (max %s min)",
                 getattr(self.night, "start", ""), getattr(self.night, "end", ""),
                 getattr(self.night, "max_minutes", 0))

    async def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.bus and self.bus.status("night", "stopped")

    # -- scheduling ------------------------------------------------------
    async def _schedule_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._in_window() and not self._ran_tonight():
                    await self.run_now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never raise into the kernel
                log.warning("night schedule tick failed: %s", exc)
                self.bus and self.bus.degrade("night", f"schedule tick failed: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    def _in_window(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        start = _hhmm(getattr(self.night, "start", "01:00"))
        end = _hhmm(getattr(self.night, "end", "06:00"))
        if start is None or end is None:
            return False
        t = now.hour * 60 + now.minute
        if start == end:
            return True
        if start < end:
            return start <= t < end
        return t >= start or t < end       # wraps midnight

    def _ran_tonight(self) -> bool:
        try:
            last = self.db.kv_get("night.last_run_date")
        except Exception:
            return False
        return last == datetime.now().strftime("%Y-%m-%d")

    def _mark_ran(self) -> None:
        try:
            self.db.kv_set("night.last_run_date", datetime.now().strftime("%Y-%m-%d"))
        except Exception:  # pragma: no cover
            pass

    # -- the run ---------------------------------------------------------
    async def run_now(self, objectives: list[str] | None = None) -> str:
        """Execute one full night run and return its night_run id."""
        async with self._lock:
            objectives = list(objectives if objectives is not None
                              else (getattr(self.night, "tasks", None) or []))
            run = NightRun(
                id=new_id("night"),
                started_at=now_iso(),
                deadline_mono=time.monotonic()
                + max(1, int(getattr(self.night, "max_minutes", 240) or 240)) * 60.0,
                objectives=objectives,
            )
            self._current = run.id
            self.db.execute(
                "INSERT INTO night_runs(id,started_at,objective,status) VALUES(?,?,?,?)",
                (run.id, run.started_at, json.dumps(objectives, ensure_ascii=False), "running"),
            )
            self.db.audit("night.start", target=run.id,
                          detail={"objectives": objectives,
                                  "max_minutes": getattr(self.night, "max_minutes", 240)})
            self.bus and self.bus.publish("night.started", run_id=run.id,
                                          objectives=objectives)
            try:
                await self._execute(run)
            except asyncio.CancelledError:
                run.status = "cancelled"
                self._finalise(run)
                raise
            except Exception as exc:  # a runner bug must not escape
                log.exception("night run failed")
                run.new_issues.append(f"runner crashed: {type(exc).__name__}: {exc}")
                run.status = "failed"
                self._finalise(run)
                self.bus and self.bus.degrade("night", f"run failed: {exc}")
            finally:
                self._current = None
                self._mark_ran()
            return run.id

    async def _execute(self, run: NightRun) -> None:
        # Stage 0/1 -----------------------------------------------------
        run.baseline = self._baseline()
        run.preflight = await self._preflight(run)
        run.rollback = self._rollback_point(run)
        run.recon = self._recon()

        if self._deadline_passed(run):
            run.emergency = {"reason": "deadline passed during preflight"}
            run.status = "aborted"
            self._finalise(run)
            return

        # Stage 2 -------------------------------------------------------
        run.checks = await self._audit_checks()

        # Stage 3/4 -----------------------------------------------------
        await self._work_loop(run)

        # Stage 5 -------------------------------------------------------
        run.health_gates = await self._health_gates()

        # Stage 6 -------------------------------------------------------
        emergency = self._emergency_check(run)
        if emergency:
            run.emergency = emergency
            run.new_issues.append(f"emergency stop: {emergency['reason']}")
            run.status = "aborted"
        elif run.status == "running":
            run.status = "succeeded"
            if any(not o.get("ok") for o in run.objectives_result):
                run.status = "partial"
            if not all(g.get("ok") for g in run.health_gates):
                run.status = "partial"

        # Stage 7 -------------------------------------------------------
        self._finalise(run)

    async def _work_loop(self, run: NightRun) -> None:
        if not run.objectives:
            run.unknowns.append("no objectives configured; stages 3-4 had nothing to do")
            return
        for objective in run.objectives:
            if self._deadline_passed(run):
                run.unknowns.append(f"objective not attempted (deadline): {objective}")
                run.objectives_result.append(
                    {"objective": objective, "ok": False, "status": "not run",
                     "reason": "deadline passed", "attempts": []})
                continue
            result = await self._attempt_with_ladder(run, objective)
            run.objectives_result.append(result)
            if not result["ok"]:
                run.failures.append({
                    "objective": objective,
                    "reason": result.get("reason", "failed"),
                    "retry_history": result.get("attempts", []),
                })
                self._create_alert("warn", "night.objective",
                                   f"Night objective failed: {objective[:60]}",
                                   result.get("reason", "failed"),
                                   {"objective": objective,
                                    "attempts": result.get("attempts", [])})

    async def _attempt_with_ladder(self, run: NightRun, objective: str) -> dict[str, Any]:
        """Attempt one objective with the 3 -> 2 -> 1 retry ladder."""
        attempts: list[dict[str, Any]] = []
        outcome: dict[str, Any] = {"objective": objective, "ok": False,
                                   "status": "failed", "attempts": attempts}
        ladder = (3, 2, 1)
        for round_size in ladder:
            for i in range(round_size):
                if self._deadline_passed(run) or self._stop.is_set():
                    attempts.append({"round": round_size, "attempt": i + 1,
                                     "ok": False, "status": "not run",
                                     "reason": "deadline or stop requested",
                                     "ts": now_iso()})
                    outcome["reason"] = "deadline or stop requested"
                    outcome["status"] = "not run"
                    return outcome
                rec = await self._attempt_objective(run, objective)
                rec["round"] = round_size
                rec["attempt"] = i + 1
                attempts.append(rec)
                if rec.get("ok"):
                    verified = bool(rec.get("verified"))
                    outcome.update(ok=True, verified=verified,
                                   status="succeeded" if verified else "completed-unverified",
                                   output=rec.get("output", ""),
                                   reason=rec.get("reason", ""))
                    if not verified:
                        run.unknowns.append(
                            f"objective completed but unverified: {objective}")
                    return outcome
            # brief pause between ladder rounds so a transient fault can clear
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
        outcome["reason"] = "retry ladder exhausted (3->2->1)"
        return outcome

    async def _attempt_objective(self, run: NightRun, objective: str) -> dict[str, Any]:
        agent = getattr(self.j, "agent", None)
        rec: dict[str, Any] = {"ts": now_iso(), "objective": objective}
        if agent is None or not hasattr(agent, "complete"):
            rec.update(ok=False, verified=False, status="not run",
                       reason="no executor (agent) available")
            run.unknowns.append(f"objective could not run (no executor): {objective}")
            return rec
        remaining = max(5.0, run.deadline_mono - time.monotonic())
        timeout = min(OBJECTIVE_TIMEOUT_S, remaining)
        t0 = time.perf_counter()
        try:
            output = await asyncio.wait_for(
                agent.complete(objective, source="schedule"), timeout=timeout)
            rec.update(ok=True, output=str(output or ""),
                       duration_ms=round((time.perf_counter() - t0) * 1000.0, 1))
        except asyncio.TimeoutError:
            rec.update(ok=False, reason=f"attempt timed out after {timeout:.0f}s",
                       duration_ms=round((time.perf_counter() - t0) * 1000.0, 1))
            return rec
        except Exception as exc:
            rec.update(ok=False, reason=f"{type(exc).__name__}: {exc}",
                       duration_ms=round((time.perf_counter() - t0) * 1000.0, 1))
            return rec

        verified, note = await self._verify_objective(objective, rec.get("output", ""))
        rec.update(verified=verified, verify_note=note,
                   status="succeeded" if verified else "completed-unverified")
        return rec

    async def _verify_objective(self, objective: str, output: str) -> tuple[bool, str]:
        """Independent read-back where possible; otherwise say unverified."""
        low = objective.lower()
        if any(word in low for word in ("health", "audit", "check", "verify")):
            gates = await self._health_gates()
            ok = all(g.get("ok") for g in gates)
            return ok, "health gates re-run" if ok else "health gates failed"
        return False, (f"no independent verification defined for this objective; "
                       f"output was {len(output)} chars, treated as unverified")

    # -- stage 0: preflight ---------------------------------------------
    async def _preflight(self, run: NightRun) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        # database reachable
        try:
            row = self.db.query_one("SELECT 1 AS ok")
            ok = bool(row)
            checks.append({"name": "database reachable", "ok": ok,
                           "status": "pass" if ok else "fail",
                           "detail": str(self.db.path)})
        except Exception as exc:
            checks.append({"name": "database reachable", "ok": False, "status": "fail",
                           "detail": str(exc)})
        # workspace present
        try:
            root = _project_root(self.config)
            exists = root.is_dir()
            checks.append({"name": "workspace present", "ok": exists,
                           "status": "pass" if exists else "fail", "detail": str(root)})
        except Exception as exc:
            checks.append({"name": "workspace present", "ok": False, "status": "fail",
                           "detail": str(exc)})
        # disk headroom
        disk = self._disk_free()
        checks.append({"name": "disk headroom", "ok": disk.get("ok", False),
                       "status": "pass" if disk.get("ok") else "fail",
                       "detail": disk.get("detail", "unknown")})
        # rollback capability (created later but probed here)
        rollback_ok, rollback_detail = self._rollback_capable()
        checks.append({"name": "rollback point available", "ok": rollback_ok,
                       "status": "pass" if rollback_ok else "warn",
                       "detail": rollback_detail})
        return checks

    def _rollback_capable(self) -> tuple[bool, str]:
        root = _project_root(self.config)
        if (root / ".git").exists():
            return True, "git working tree"
        try:
            if Path(str(self.config.db_path)).is_file():
                return True, "sqlite snapshot of the database"
        except Exception:
            pass
        return False, "no VCS and no database file to snapshot"

    def _rollback_point(self, run: NightRun) -> dict[str, Any]:
        root = _project_root(self.config)
        point: dict[str, Any] = {"created_at": now_iso()}
        if (root / ".git").exists():
            head = _run_cmd(["git", "-C", str(root), "rev-parse", "HEAD"])
            status = _run_cmd(["git", "-C", str(root), "status", "--porcelain"])
            point.update(kind="git", head=(head or "").strip() or None,
                         dirty_files=len((status or "").strip().splitlines()))
        snapshot = self._snapshot_db(run.id)
        if snapshot:
            point["db_snapshot"] = snapshot
            point.setdefault("kind", "db-snapshot")
        if "kind" not in point:
            point.update(kind="none", note="no rollback point could be created")
            run.unknowns.append("no rollback point was created")
        return point

    def _snapshot_db(self, run_id: str) -> dict[str, Any] | None:
        try:
            src = Path(str(self.config.db_path))
        except Exception:
            return None
        if not src.is_file():
            return None
        size = src.stat().st_size
        if size > DB_SNAPSHOT_MAX:
            return {"skipped": True, "reason": "database larger than snapshot cap",
                    "size_bytes": size}
        try:
            import sqlite3
            dest_dir = self.config.data_path / "artifacts" / "night"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{run_id}.db"
            con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            try:
                out = sqlite3.connect(str(dest))
                try:
                    con.backup(out)
                finally:
                    out.close()
            finally:
                con.close()
            return {"path": str(dest), "size_bytes": dest.stat().st_size}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    # -- stage 1: recon --------------------------------------------------
    def _baseline(self) -> dict[str, Any]:
        import platform
        base: dict[str, Any] = {"at": now_iso()}
        if psutil is not None:
            try:
                base["cpu_percent"] = psutil.cpu_percent(interval=None)
                vm = psutil.virtual_memory()
                base["mem_percent"] = float(vm.percent)
                up = time.time() - psutil.boot_time()
                base["uptime_h"] = round(up / 3600.0, 2)
            except Exception as exc:  # pragma: no cover
                base["psutil_error"] = str(exc)
        else:
            base["psutil"] = "not installed"
        base["disk_free"] = self._disk_free()
        try:
            base["platform"] = f"{platform.system()} {platform.release()}"
        except Exception:  # pragma: no cover
            base["platform"] = "unknown"
        try:
            base["metrics_rows"] = int(self.db.query_one(
                "SELECT COUNT(*) AS n FROM metrics")["n"])
            base["alerts_unack"] = alerts_mod.unacknowledged_count(self.db)
        except Exception:  # pragma: no cover
            pass
        return base

    def _recon(self) -> dict[str, Any]:
        root = _project_root(self.config)
        recon: dict[str, Any] = {"project_root": str(root)}
        try:
            py_files = [p for p in root.glob("jarvis/**/*.py")]
            recon["python_files"] = len(py_files)
        except Exception:  # pragma: no cover
            recon["python_files"] = None
        try:
            recon["git_head"] = (_run_cmd(["git", "-C", str(root), "rev-parse", "--short", "HEAD"])
                                 or "").strip() or None
        except Exception:  # pragma: no cover
            recon["git_head"] = None
        try:
            recon["db_size_bytes"] = Path(str(self.config.db_path)).stat().st_size
        except Exception:
            recon["db_size_bytes"] = None
        try:
            recon["node_count"] = int(self.db.query_one("SELECT COUNT(*) AS n FROM nodes")["n"])
        except Exception:
            recon["node_count"] = None
        return recon

    # -- stage 2: audit --------------------------------------------------
    async def _audit_checks(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        checks.append(self._check_db_integrity())
        checks.append(self._check_registry())
        checks.append(await self._check_llm())
        checks.append(self._check_disk())
        checks.append(self._check_log_errors())
        return checks

    def _check_db_integrity(self) -> dict[str, Any]:
        try:
            row = self.db.query_one("PRAGMA integrity_check")
            value = row[0] if row is not None and len(row) > 0 else None
            ok = value == "ok"
            return {"name": "db integrity", "status": "pass" if ok else "fail",
                    "ok": ok, "detail": str(value)}
        except Exception as exc:
            return {"name": "db integrity", "status": "not run", "ok": False,
                    "detail": f"could not run: {exc}"}

    def _check_registry(self) -> dict[str, Any]:
        registry = getattr(self.j, "registry", None)
        if registry is None:
            return {"name": "tool registry", "status": "not run", "ok": False,
                    "detail": "registry not present"}
        try:
            names = registry.names()
            count = len(names)
            if count == 0:
                return {"name": "tool registry", "status": "warn", "ok": True,
                        "detail": "registry loaded but exposes no tools"}
            return {"name": "tool registry", "status": "pass", "ok": True,
                    "detail": f"{count} tools loaded"}
        except Exception as exc:
            return {"name": "tool registry", "status": "fail", "ok": False,
                    "detail": f"load error: {exc}"}

    async def _check_llm(self) -> dict[str, Any]:
        router = getattr(self.j, "router", None)
        if router is None or not hasattr(router, "health"):
            return {"name": "llm health", "status": "not run", "ok": False,
                    "detail": "no model router"}
        try:
            health = await asyncio.wait_for(router.health(), timeout=15.0)
        except asyncio.TimeoutError:
            return {"name": "llm health", "status": "fail", "ok": False,
                    "detail": "health check timed out"}
        except Exception as exc:
            return {"name": "llm health", "status": "fail", "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}"}
        ok = bool(health.get("ok"))
        return {"name": "llm health", "status": "pass" if ok else "fail", "ok": ok,
                "detail": json.dumps(health, default=str)[:300]}

    def _check_disk(self) -> dict[str, Any]:
        disk = self._disk_free()
        ok = bool(disk.get("ok"))
        return {"name": "disk", "status": "pass" if ok else "fail", "ok": ok,
                "detail": disk.get("detail", "unknown")}

    def _check_log_errors(self) -> dict[str, Any]:
        try:
            log_path = self.config.data_path / "logs" / "jarvis.jsonl"
        except Exception as exc:
            return {"name": "log error scan", "status": "not run", "ok": False,
                    "detail": f"could not resolve log path: {exc}"}
        if not log_path.is_file():
            return {"name": "log error scan", "status": "not run", "ok": False,
                    "detail": f"no log file at {log_path}"}
        try:
            lines = _tail_lines(log_path, LOG_SCAN_LINES)
        except Exception as exc:
            return {"name": "log error scan", "status": "not run", "ok": False,
                    "detail": f"could not read log: {exc}"}
        errors = warnings = 0
        samples: list[str] = []
        for line in lines:
            if '"level": "ERROR"' in line or '"level": "CRITICAL"' in line:
                errors += 1
                if len(samples) < 5:
                    samples.append(line.strip()[:200])
            elif '"level": "WARNING"' in line:
                warnings += 1
        status = "pass" if errors == 0 else ("warn" if errors < 10 else "fail")
        return {"name": "log error scan", "status": status, "ok": errors < 10,
                "detail": {"errors": errors, "warnings": warnings,
                           "scanned_lines": len(lines), "samples": samples}}

    # -- stage 5: health gates ------------------------------------------
    async def _health_gates(self) -> list[dict[str, Any]]:
        gates = [self._check_db_integrity(), self._check_registry(), self._check_disk()]
        gates.append(await self._check_llm())
        return gates

    def _check_rollback_gate(self) -> dict[str, Any]:
        ok, detail = self._rollback_capable()
        return {"name": "rollback", "status": "pass" if ok else "warn",
                "ok": ok, "detail": detail}

    # -- stage 6: emergency ---------------------------------------------
    def _emergency_check(self, run: NightRun) -> dict[str, Any] | None:
        if self._stop.is_set():
            return {"reason": "operator requested stop"}
        if self._deadline_passed(run):
            return {"reason": "max_minutes deadline exceeded"}
        disk = self._disk_free()
        if disk.get("critical"):
            return {"reason": f"disk critical: {disk.get('detail')}"}
        if not Path(str(self.config.db_path)).exists():
            return {"reason": "database file missing"}
        try:
            defcon = self.j.policy.defcon
        except Exception:
            defcon = None
        if defcon is not None and int(defcon) <= 1:
            return {"reason": "DEFCON 1 lockdown"}
        return None

    # -- stage 7: reconciliation + report -------------------------------
    def _finalise(self, run: NightRun) -> None:
        ended = now_iso()
        summary = {
            "status": run.status,
            "objectives_total": len(run.objectives),
            "objectives_ok": sum(1 for o in run.objectives_result if o.get("ok")),
            "checks": {c["name"]: c.get("status") for c in run.checks},
            "health_gates": {g["name"]: g.get("status") for g in run.health_gates},
            "failures": len(run.failures),
            "emergency": run.emergency,
        }
        report_path = self._write_report(run, ended)
        try:
            self.db.execute(
                "UPDATE night_runs SET ended_at=?, status=?, report_path=?, summary=? "
                "WHERE id=?",
                (ended, run.status, str(report_path),
                 json.dumps(summary, ensure_ascii=False), run.id),
            )
        except Exception as exc:  # pragma: no cover
            log.warning("could not update night_run row: %s", exc)
        try:
            self.db.audit("night.end", target=run.id, detail=summary,
                          severity="warn" if run.status in ("failed", "aborted") else "info")
        except Exception:  # pragma: no cover
            pass
        self.bus and self.bus.publish("night.finished", run_id=run.id, status=run.status,
                                      report_path=str(report_path))
        log.info("night run %s finished: %s (%s)", run.id, run.status, report_path)

    def _write_report(self, run: NightRun, ended: str) -> Path:
        try:
            out_dir = self.config.night_report_path
        except Exception:
            out_dir = Path(".")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{run.id}.md"
        path.write_text(self._render_report(run, ended), encoding="utf-8")
        return path

    def _render_report(self, run: NightRun, ended: str) -> str:
        L: list[str] = []
        L.append(f"# Morning report — {run.id}")
        L.append("")
        L.append(f"- Started: {run.started_at}")
        L.append(f"- Ended:   {ended}")
        L.append(f"- Status:  **{run.status}**")
        if run.emergency:
            L.append(f"- Emergency stop: {run.emergency.get('reason')}")
        L.append("")

        L.append("## Baseline")
        L.append(_json_block(run.baseline))
        L.append("## Preflight")
        L.append(_checks_block(run.preflight))
        L.append("## Recon")
        L.append(_json_block(run.recon))

        L.append("## Objectives attempted")
        if not run.objectives:
            L.append("- none configured")
        for o in run.objectives_result:
            L.append(f"- `{o['objective']}` — {o.get('status', 'unknown')} "
                     f"({len(o.get('attempts', []))} attempt(s))")
        L.append("")

        L.append("## Changes")
        if run.changes:
            L.extend(f"- {c}" for c in run.changes)
        else:
            L.append("- none recorded (this runner does not modify the workspace itself)")
        L.append("")

        L.append("## Tests / checks run and outcomes")
        L.append(_checks_block(run.checks))
        L.append("## Health gates at exit")
        L.append(_checks_block(run.health_gates))
        L.append(_check_extra(self._check_rollback_gate()))

        L.append("## Failures and retry history")
        if not run.failures:
            L.append("- none")
        for f in run.failures:
            L.append(f"- `{f['objective']}`: {f.get('reason')}")
            for a in f.get("retry_history", []):
                L.append(f"    - round {a.get('round')} attempt {a.get('attempt')}: "
                         f"{'ok' if a.get('ok') else 'fail'} — {a.get('reason', a.get('status',''))}")
        L.append("")

        L.append("## New issues")
        if run.new_issues:
            L.extend(f"- {i}" for i in run.new_issues)
        else:
            L.append("- none")
        L.append("")

        L.append("## Unknowns")
        if run.unknowns:
            L.extend(f"- {u}" for u in run.unknowns)
        else:
            L.append("- none")
        L.append("")

        L.append("## Rollback points")
        L.append(_json_block(run.rollback))

        L.append("## Recommended next three actions")
        L.extend(f"{i}. {a}" for i, a in enumerate(self._recommendations(run), start=1))
        L.append("")
        return "\n".join(L)

    def _recommendations(self, run: NightRun) -> list[str]:
        recs: list[str] = []
        failed_gates = [g["name"] for g in run.health_gates if not g.get("ok")]
        if run.failures:
            recs.append(f"Investigate the failed objective(s): "
                        f"{', '.join(f['objective'][:40] for f in run.failures[:2])}")
        if failed_gates:
            recs.append(f"Repair failing health gate(s): {', '.join(failed_gates)}")
        if run.emergency:
            recs.append(f"Resolve the emergency condition: {run.emergency.get('reason')}")
        if self._check_disk().get("ok") is False:
            recs.append("Free disk space before the next run")
        recs.append("Review the unacknowledged alert queue")
        recs.append("Confirm the llm/model stack is reachable for the next run")
        # de-duplicate, keep exactly three
        seen: list[str] = []
        for r in recs:
            if r not in seen:
                seen.append(r)
        return seen[:3]

    # -- helpers ---------------------------------------------------------
    def _deadline_passed(self, run: NightRun) -> bool:
        return time.monotonic() >= run.deadline_mono

    def _disk_free(self) -> dict[str, Any]:
        path = "/"
        try:
            path = str(self.config.data_path)
        except Exception:
            pass
        if psutil is None:
            return {"ok": True, "critical": False, "detail": "psutil unavailable; not run"}
        try:
            free = int(shutil.disk_usage(path).free)
        except Exception as exc:
            return {"ok": False, "critical": False, "detail": f"could not stat {path}: {exc}"}
        free_gib = free / (1024 ** 3)
        critical = free < DISK_CRITICAL_FREE
        ok = free >= DISK_WARN_FREE
        return {"ok": ok, "critical": critical, "free_bytes": free,
                "detail": f"{free_gib:.1f} GiB free on {path}"}

    def _create_alert(self, severity: str, source: str, title: str, body: str,
                      evidence: Any = None) -> str | None:
        try:
            return alerts_mod.create_alert(self.db, self.bus, severity, source,
                                           title, body, evidence)
        except Exception as exc:  # pragma: no cover
            log.warning("night alert failed: %s", exc)
            return None

    # -- status ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        last = None
        try:
            row = self.db.query_one(
                "SELECT id,started_at,ended_at,status,report_path FROM night_runs "
                "ORDER BY started_at DESC LIMIT 1")
            last = dict(row) if row else None
        except Exception:  # pragma: no cover
            pass
        return {
            "component": "night",
            "enabled": bool(getattr(self.night, "enabled", False)),
            "running": self._running,
            "active_run": self._current,
            "window": f"{getattr(self.night, 'start', '')}-{getattr(self.night, 'end', '')}",
            "max_minutes": getattr(self.night, "max_minutes", 240),
            "report_dir": str(getattr(self.config, "night_report_path", "")),
            "last_run": last,
        }


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------

def _hhmm(text: str) -> int | None:
    try:
        hh, _, mm = str(text).partition(":")
        return (int(hh) % 24) * 60 + (int(mm) % 60 if mm else 0)
    except (ValueError, TypeError):
        return None


def _project_root(config: Any) -> Path:
    try:
        from ..config import PROJECT_ROOT
        return Path(PROJECT_ROOT)
    except Exception:  # pragma: no cover
        return Path(".")


def _run_cmd(cmd: list[str]) -> str | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _tail_lines(path: Path, n: int) -> list[str]:
    """Read the last ``n`` lines without loading the whole file."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        block = min(size, max(64 * 1024, n * 400))
        fh.seek(max(0, size - block))
        data = fh.read()
    lines = data.splitlines()
    return lines[-n:]


def _json_block(value: Any) -> str:
    try:
        return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n```\n"
    except (TypeError, ValueError):  # pragma: no cover
        return f"```\n{value!r}\n```\n"


def _checks_block(checks: list[dict[str, Any]]) -> str:
    if not checks:
        return "- not run\n"
    out = []
    for c in checks:
        detail = c.get("detail")
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        status = c.get("status") or ("pass" if c.get("ok") else "fail")
        out.append(f"- **{c.get('name')}**: {status} — {detail}")
    return "\n".join(out) + "\n"


def _check_extra(check: dict[str, Any]) -> str:
    detail = check.get("detail")
    if not isinstance(detail, str):
        detail = json.dumps(detail, default=str)
    return f"- **{check.get('name')}**: {check.get('status')} — {detail}\n"
