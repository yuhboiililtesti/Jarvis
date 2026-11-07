"""Security posture, integrity watching, audit tailing and the optional decoy.

Three jobs, all narrow and evidence-based:

  * **DEFCON integration.** Posture is read from and written to the policy broker
    (the single source of truth); this service only exposes it and publishes the
    change on the bus.
  * **Integrity watcher.** A small set of critical files (config, the optional
    secrets file, the database) is fingerprinted. Content changes to config and
    secrets raise alerts; the database is expected to churn, so only the loss of
    the file or a large shrink is treated as unexpected. The baseline is stored
    in the ``kv`` table so it survives restarts.
  * **Audit tailer.** New rows in the ``audit`` table are scanned for suspicious
    patterns (repeated policy denials, repeated tool failures, authentication
    failures). A cursor in ``kv`` means each row is examined once.

The optional decoy lives in :mod:`jarvis.security.honeypot`, defaults to
disabled, and only ever records connection attempts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..ids import now_iso
from ..logging_setup import get_logger
from ..monitoring import alerts as alerts_mod
from .honeypot import Honeypot

try:
    from ..policy import DEFCON_EFFECTS
except Exception:  # pragma: no cover - policy is core but stay defensive
    DEFCON_EFFECTS = {}

log = get_logger("jarvis.security.service")

__all__ = ["SecurityService"]

BASELINE_KEY = "security.integrity.baseline"
AUDIT_CURSOR_KEY = "security.audit_tail.id"
HASH_LIMIT = 8 * 1024 * 1024        # hash at most 8 MiB per file
DB_SHRINK_ALERT_RATIO = 0.8         # alert when the db drops below 80% of size

# audit-tail thresholds (count within the rows examined this tick)
THRESHOLDS = {
    "policy_denials": 5,
    "approval_denials": 3,
    "tool_failures": 5,
    "auth_failures": 3,
}


class SecurityService:
    def __init__(self, jarvis: Any) -> None:
        self.j = jarvis
        self.config = getattr(jarvis, "config", None)
        self.db = getattr(jarvis, "db", None)
        self.bus = getattr(jarvis, "bus", None)
        self.policy = getattr(jarvis, "policy", None)
        self.security = getattr(getattr(jarvis, "config", None), "security", None)

        try:
            self.interval_s = float(os.environ.get("JARVIS_SECURITY_INTERVAL_S", "60") or 60)
        except ValueError:
            self.interval_s = 60.0
        self.interval_s = max(10.0, self.interval_s)

        self.honeypot = Honeypot(jarvis)

        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_integrity_at: str | None = None
        self._last_integrity_changes: list[dict[str, Any]] = []
        self._last_tail_at: str | None = None
        self._last_tail_alerts: list[str] = []

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._bootstrap_baseline()
        await self._tick()
        self._task = asyncio.create_task(self._loop(), name="security.watch")
        await self.honeypot.start()
        self.bus and self.bus.status("security", "started",
                                     defcon=self.defcon, interval_s=self.interval_s)
        log.info("security started (defcon=%s, interval=%ss, honeypot=%s)",
                 self.defcon, self.interval_s, self.honeypot.enabled)

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
        await self.honeypot.stop()
        self.bus and self.bus.status("security", "stopped")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never raise into the kernel
                log.warning("security tick failed: %s", exc)
                self.bus and self.bus.degrade("security", f"tick failed: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        if self.security is not None and not getattr(self.security, "audit_enabled", True):
            return
        self.integrity_check()
        self.audit_tail()

    # -- DEFCON ----------------------------------------------------------
    @property
    def defcon(self) -> int:
        try:
            return int(self.policy.defcon) if self.policy is not None else 5
        except Exception:  # pragma: no cover
            return 5

    def set_defcon(self, level: int) -> int:
        """Set DEFCON posture via the policy broker. Returns the applied level."""
        try:
            level = int(level)
        except (TypeError, ValueError):
            raise ValueError(f"DEFCON must be an integer 1-5, got {level!r}")
        level = max(1, min(5, level))
        previous = self.defcon
        if self.policy is not None and hasattr(self.policy, "set_defcon"):
            self.policy.set_defcon(level)
        else:  # pragma: no cover - degrade gracefully without a broker
            try:
                self.db.kv_set("defcon", level)
            except Exception:
                pass
        note = DEFCON_EFFECTS.get(level, {}).get("note", "")
        try:
            self.db.audit("security.defcon", actor="owner",
                          detail={"from": previous, "to": level},
                          severity="warn" if level <= 3 else "info")
        except Exception:  # pragma: no cover
            pass
        if self.bus is not None:
            try:
                self.bus.publish("security.defcon", level=level, previous=previous, note=note)
            except Exception:  # pragma: no cover
                pass
        return level

    # -- integrity -------------------------------------------------------
    def _critical_files(self) -> list[tuple[str, Path]]:
        root = _project_root()
        # Only *runtime state* is fingerprinted. Source files (config.py, the
        # example TOML) are deliberately excluded: editing them is normal
        # development, and treating every code change as a critical integrity
        # event is noise that trains the owner to ignore the alert.
        files: list[tuple[str, Path]] = [
            ("config", root / "config" / "jarvis.toml"),
        ]
        try:
            files.append(("secrets", Path(self.config.data_path) / "secrets.json"))
        except Exception:  # pragma: no cover
            pass
        try:
            files.append(("db", Path(self.config.db_path)))
        except Exception:  # pragma: no cover
            pass
        return files

    def _bootstrap_baseline(self) -> dict[str, Any]:
        try:
            existing = self.db.kv_get(BASELINE_KEY)
        except Exception:  # pragma: no cover
            existing = None
        if isinstance(existing, dict) and existing:
            return existing
        current = self._fingerprint_all()
        try:
            self.db.kv_set(BASELINE_KEY, current)
            self.db.audit("security.integrity.baseline",
                          detail={"files": len(current)})
        except Exception as exc:  # pragma: no cover
            log.warning("could not store integrity baseline: %s", exc)
        log.info("security integrity baseline established over %d file(s)", len(current))
        return current

    def _fingerprint_all(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for kind, path in self._critical_files():
            fp = self._fingerprint(path)
            fp["kind"] = kind
            out[str(path)] = fp
        return out

    @staticmethod
    def _fingerprint(path: Path) -> dict[str, Any]:
        try:
            p = Path(path)
            if not p.is_file():
                return {"exists": False}
            st = p.stat()
            digest, truncated = _hash_file(p, HASH_LIMIT)
            return {"exists": True, "size": int(st.st_size),
                    "mtime_ns": int(st.st_mtime_ns), "sha256": digest,
                    "truncated": truncated}
        except Exception as exc:  # pragma: no cover - defensive
            return {"exists": False, "error": f"{type(exc).__name__}: {exc}"}

    def integrity_check(self) -> list[dict[str, Any]]:
        """Compare critical files against the stored baseline; alert on change."""
        now = now_iso()
        try:
            baseline = self.db.kv_get(BASELINE_KEY)
        except Exception:  # pragma: no cover
            baseline = None
        if not isinstance(baseline, dict):
            baseline = self._bootstrap_baseline()

        current = self._fingerprint_all()
        changes: list[dict[str, Any]] = []
        for key, fp in current.items():
            old = baseline.get(key)
            if not isinstance(old, dict):
                continue
            kind = fp.get("kind", "config")
            if bool(old.get("exists")) != bool(fp.get("exists")):
                if fp.get("exists"):
                    changes.append({"path": key, "kind": kind, "change": "created",
                                    "severity": "info", "at": now})
                else:
                    sev = "critical" if kind in ("config", "db") else "warn"
                    changes.append({"path": key, "kind": kind, "change": "missing",
                                    "severity": sev, "at": now,
                                    "detail": "previously present file is gone"})
                continue
            if not fp.get("exists"):
                continue
            size_changed = old.get("size") != fp.get("size")
            content_changed = old.get("sha256") != fp.get("sha256")
            if kind == "db":
                old_size = int(old.get("size") or 0)
                new_size = int(fp.get("size") or 0)
                if old_size and new_size < old_size * DB_SHRINK_ALERT_RATIO:
                    changes.append({"path": key, "kind": kind, "change": "shrank",
                                    "severity": "warn", "at": now,
                                    "detail": f"{old_size} -> {new_size} bytes"})
                # routine growth is expected; baseline still updates below
                continue
            if content_changed or size_changed:
                sev = "critical" if kind == "config" else "warn"
                changes.append({"path": key, "kind": kind, "change": "content",
                                "severity": sev, "at": now,
                                "detail": "content hash changed"})

        try:
            self.db.kv_set(BASELINE_KEY, current)
        except Exception as exc:  # pragma: no cover
            log.warning("could not update integrity baseline: %s", exc)

        for change in changes:
            if change["severity"] not in ("warn", "critical"):
                continue
            self._raise_integrity_alert(change)

        self._last_integrity_at = now
        self._last_integrity_changes = changes
        return changes

    def _raise_integrity_alert(self, change: dict[str, Any]) -> None:
        key = f"integrity:{change['path']}:{change['change']}"
        cooldown = self._alert_cooldown()
        allowed, reason = alerts_mod.should_raise(self.db, key=key, cooldown_s=cooldown)
        if not allowed:
            log.debug("integrity alert suppressed (%s): %s", reason, key)
            return
        title = f"Integrity change: {Path(change['path']).name} ({change['change']})"
        body = (f"{change['kind']} file {change['path']} {change['change']} "
                f"({change.get('detail', '')}). Verified by hash comparison.")
        self.create_alert(change["severity"], "security.integrity", title, body, change)
        alerts_mod.mark_cooldown(self.db, key)

    # -- audit tail ------------------------------------------------------
    def audit_tail(self) -> list[str]:
        """Scan new audit rows for suspicious patterns; returns alert ids raised."""
        try:
            cursor = int(self.db.kv_get(AUDIT_CURSOR_KEY, 0) or 0)
        except Exception:  # pragma: no cover
            cursor = 0
        try:
            rows = self.db.query(
                "SELECT id,ts,actor,action,target,detail,severity FROM audit "
                "WHERE id>? ORDER BY id LIMIT 500", (cursor,))
        except Exception as exc:  # pragma: no cover
            log.warning("audit tail query failed: %s", exc)
            return []

        if not rows:
            self._last_tail_at = now_iso()
            return []

        max_id = max(int(r["id"]) for r in rows)
        buckets: dict[str, list[dict[str, Any]]] = {
            "policy_denials": [], "approval_denials": [],
            "tool_failures": [], "auth_failures": [],
        }
        for r in rows:
            action = str(r["action"] or "")
            detail = _loads(r["detail"]) or {}
            record = {"id": int(r["id"]), "ts": r["ts"], "action": action,
                      "target": r["target"], "severity": r["severity"]}
            if action == "policy.deny":
                buckets["policy_denials"].append(record)
            elif action == "approval.resolved" and str(detail.get("decision")) in ("deny", "timeout"):
                record["decision"] = detail.get("decision")
                buckets["approval_denials"].append(record)
            elif action in ("tool.error", "tool.fail") or (
                    action == "tool.call" and detail.get("ok") is False):
                buckets["tool_failures"].append(record)
            elif action in ("auth.fail", "login.fail", "node.auth.fail",
                            "secrets.denied", "auth.denied"):
                buckets["auth_failures"].append(record)

        raised: list[str] = []
        for name, records in buckets.items():
            threshold = THRESHOLDS.get(name, 5)
            if len(records) < threshold:
                continue
            cooldown_key = f"audit:{name}"
            allowed, reason = alerts_mod.should_raise(
                self.db, key=cooldown_key, cooldown_s=self._alert_cooldown())
            if not allowed:
                log.debug("audit alert suppressed (%s): %s", reason, name)
                continue
            evidence = {"pattern": name, "count": len(records),
                        "threshold": threshold,
                        "samples": records[:5]}
            alert_id = self.create_alert(
                "warn", "security.audit",
                f"Suspicious audit pattern: {name.replace('_', ' ')}",
                f"{len(records)} '{name}' entries in the last audit window "
                f"(threshold {threshold}).",
                evidence)
            alerts_mod.mark_cooldown(self.db, cooldown_key)
            if alert_id:
                raised.append(alert_id)

        try:
            self.db.kv_set(AUDIT_CURSOR_KEY, max_id)
        except Exception:  # pragma: no cover
            pass
        self._last_tail_at = now_iso()
        self._last_tail_alerts = raised
        return raised

    # -- alerts ----------------------------------------------------------
    def create_alert(self, severity: str, source: str, title: str, body: str,
                     evidence: Any = None, **kwargs: Any) -> str | None:
        try:
            return alerts_mod.create_alert(self.db, self.bus, severity, source,
                                           title, body, evidence, **kwargs)
        except Exception as exc:  # pragma: no cover
            log.warning("security alert failed: %s", exc)
            return None

    def _alert_cooldown(self) -> float:
        try:
            return float(self.config.monitoring.alert_cooldown_s)
        except Exception:
            return 600.0

    # -- status ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        defcon = self.defcon
        baseline = {}
        try:
            baseline = self.db.kv_get(BASELINE_KEY) or {}
        except Exception:  # pragma: no cover
            pass
        unack = 0
        try:
            unack = alerts_mod.unacknowledged_count(self.db)
        except Exception:  # pragma: no cover
            pass
        return {
            "component": "security",
            "running": self._running,
            "defcon": defcon,
            "defcon_note": DEFCON_EFFECTS.get(defcon, {}).get("note", ""),
            "audit_enabled": bool(getattr(self.security, "audit_enabled", True)),
            "bind_local_only": bool(getattr(self.security, "bind_local_only", True)),
            "interval_s": self.interval_s,
            "integrity": {
                "files_tracked": len(baseline) if isinstance(baseline, dict) else 0,
                "last_check_at": self._last_integrity_at,
                "last_changes": self._last_integrity_changes,
            },
            "audit_tail": {
                "last_tail_at": self._last_tail_at,
                "last_alerts": self._last_tail_alerts,
            },
            "unacknowledged_alerts": unack,
            "honeypot": self.honeypot.status(),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _project_root() -> Path:
    try:
        from ..config import PROJECT_ROOT
        return Path(PROJECT_ROOT)
    except Exception:  # pragma: no cover
        return Path(".")


def _hash_file(path: Path, limit: int) -> tuple[str, bool]:
    h = hashlib.sha256()
    read = 0
    with path.open("rb") as fh:
        while read < limit:
            chunk = fh.read(min(65536, limit - read))
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
    truncated = False
    try:
        truncated = path.stat().st_size > read
    except OSError:  # pragma: no cover
        pass
    return h.hexdigest(), truncated


def _loads(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value
