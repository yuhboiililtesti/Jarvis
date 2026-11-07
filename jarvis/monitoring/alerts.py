"""Alert creation, de-duplication, cooldown and acknowledgement.

Alerts are the durable, owner-facing record of "something needs attention". This
module is deliberately tiny and side-effect honest:

  * an alert row is written to the `alerts` table *before* the bus event is
    published, so a subscriber that reacts by re-reading the alert always finds
    it;
  * the `delivered` column records which channels actually reached the user. If
    no live subscriber was attached at publish time we say so instead of
    pretending a human was notified;
  * cooldown state lives in the `kv` table so a restart cannot re-fire a storm.

Nothing here decides *whether* a condition is worth alerting on; callers do that
and use :func:`should_raise` to respect a global cooldown and quiet hours.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Iterable

from ..ids import new_id, now_iso
from ..logging_setup import get_logger

log = get_logger("jarvis.monitoring.alerts")

__all__ = [
    "SEVERITIES",
    "acknowledge",
    "active_alerts",
    "alert_key",
    "cooldown_remaining",
    "create_alert",
    "in_quiet_hours",
    "mark_cooldown",
    "parse_window",
    "recent_alerts",
    "should_raise",
    "unacknowledged_count",
]

SEVERITIES = ("info", "warn", "critical")
_COOLDOWN_PREFIX = "alert.cooldown."


# ---------------------------------------------------------------------------
# creation
# ---------------------------------------------------------------------------

def create_alert(db: Any, bus: Any, severity: str, source: str, title: str,
                 body: str = "", evidence: Any = None, *,
                 dedup_window_s: float = 0.0) -> str:
    """Insert an alert, publish it, and record honest delivery status.

    Returns the new alert id. When ``dedup_window_s`` > 0 an identical
    *unacknowledged* alert (same source + title) within that window is reused
    instead of inserting a duplicate.
    """
    severity = severity if severity in SEVERITIES else "info"
    if dedup_window_s > 0:
        existing = _recent_duplicate(db, source, title, dedup_window_s)
        if existing:
            log.debug("deduped alert %s (%s/%s)", existing, source, title)
            return existing

    alert_id = new_id("alt")
    created = now_iso()
    payload = {
        "alert_id": alert_id,
        "severity": severity,
        "source": source,
        "title": title,
        "body": body,
        "evidence": evidence,
    }

    subscribers = _subscriber_count(bus)
    channels = ["db"] + (["bus"] if subscribers > 0 else [])
    delivered: dict[str, Any] = {
        "channels": channels,
        "db": True,
        "bus": subscribers > 0,
        "subscribers": subscribers,
        "checked_at": created,
    }
    if subscribers == 0:
        delivered["note"] = ("no live bus subscriber at publish time; alert is "
                             "retrievable from history but nobody was pushed to")
    if bus is None:
        delivered["note"] = "no event bus available; alert recorded only"

    db.execute(
        "INSERT INTO alerts(id,created_at,severity,source,title,body,evidence,"
        "acknowledged,delivered) VALUES(?,?,?,?,?,?,?,0,?)",
        (alert_id, created, severity, source, title, body,
         _dumps(evidence), json.dumps(delivered, ensure_ascii=False)),
    )

    if bus is not None:
        try:
            bus.publish("alert", **payload)
        except Exception as exc:  # a bad subscriber must not lose the record
            log.warning("alert publish failed: %s", exc)
            delivered["bus"] = False
            delivered["error"] = f"{type(exc).__name__}: {exc}"
            if "bus" in delivered["channels"]:
                delivered["channels"].remove("bus")
            db.execute("UPDATE alerts SET delivered=? WHERE id=?",
                       (json.dumps(delivered, ensure_ascii=False), alert_id))

    try:
        db.audit("alert.create", target=source,
                 detail={"alert_id": alert_id, "severity": severity, "title": title},
                 severity="warn" if severity in ("warn", "critical") else "info")
    except Exception:  # pragma: no cover - audit must never break alerting
        pass

    log.log(40 if severity == "critical" else 30 if severity == "warn" else 20,
            "ALERT [%s] %s: %s", severity, source, title)
    return alert_id


# ---------------------------------------------------------------------------
# reads / acknowledgement
# ---------------------------------------------------------------------------

def recent_alerts(db: Any, limit: int = 50, *, only_unack: bool = False) -> list[dict[str, Any]]:
    sql = ("SELECT id,created_at,severity,source,title,body,evidence,acknowledged,"
           "delivered FROM alerts")
    if only_unack:
        sql += " WHERE acknowledged=0"
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    rows = db.query(sql, (int(limit),))
    return [_row_to_dict(r) for r in rows]


def active_alerts(db: Any, limit: int = 50) -> list[dict[str, Any]]:
    return recent_alerts(db, limit, only_unack=True)


def unacknowledged_count(db: Any) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM alerts WHERE acknowledged=0")
    return int(row["n"]) if row else 0


def acknowledge(db: Any, alert_id: str, *, by: str = "owner") -> bool:
    cur = db.execute("UPDATE alerts SET acknowledged=1 WHERE id=? AND acknowledged=0",
                     (alert_id,))
    if cur.rowcount > 0:
        try:
            db.audit("alert.ack", target=alert_id, actor=by)
        except Exception:  # pragma: no cover
            pass
        return True
    return False


# ---------------------------------------------------------------------------
# cooldown
# ---------------------------------------------------------------------------

def alert_key(source: str, key: str) -> str:
    """Stable, human-readable cooldown/dedup key for one alert condition."""
    return f"{source}:{key}"


def cooldown_remaining(db: Any, key: str, cooldown_s: float,
                       now: float | None = None) -> float:
    """Seconds left before ``key`` may alert again (0.0 when clear)."""
    now = time.time() if now is None else now
    last = db.kv_get(_COOLDOWN_PREFIX + key)
    try:
        last_f = float(last)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, float(cooldown_s) - (now - last_f))


def mark_cooldown(db: Any, key: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    db.kv_set(_COOLDOWN_PREFIX + key, round(now, 3))


# ---------------------------------------------------------------------------
# quiet hours
# ---------------------------------------------------------------------------

def parse_window(spec: str) -> tuple[int, int] | None:
    """Parse ``"HH:MM-HH:MM"`` into (start_minutes, end_minutes) or None."""
    if not spec or not isinstance(spec, str):
        return None
    start_s, sep, end_s = spec.partition("-")
    if not sep:
        return None
    try:
        return _to_minutes(start_s.strip()), _to_minutes(end_s.strip())
    except (ValueError, TypeError):
        return None


def _to_minutes(text: str) -> int:
    hh, _, mm = text.partition(":")
    hours = int(hh) % 24
    minutes = int(mm) % 60 if mm else 0
    return hours * 60 + minutes


def in_quiet_hours(specs: Iterable[str] | None, now: datetime | None = None) -> bool:
    """True if the wall-clock time falls inside any quiet window (wraps midnight)."""
    if not specs:
        return False
    now = now or datetime.now()
    t = now.hour * 60 + now.minute
    for spec in specs:
        window = parse_window(spec)
        if window is None:
            continue
        start, end = window
        if start == end:
            return True                      # degenerate window -> all day
        if start < end:
            if start <= t < end:
                return True
        else:                                 # wraps midnight, e.g. 23:30-07:00
            if t >= start or t < end:
                return True
    return False


def should_raise(db: Any, *, key: str, cooldown_s: float,
                 quiet_hours: Iterable[str] | None = None,
                 now: datetime | None = None,
                 epoch: float | None = None) -> tuple[bool, str]:
    """Gate one alert by quiet hours then cooldown. Returns (allowed, reason)."""
    if in_quiet_hours(quiet_hours, now=now):
        return False, "quiet hours"
    remaining = cooldown_remaining(db, key, cooldown_s, now=epoch)
    if remaining > 0:
        return False, f"cooldown ({remaining:.0f}s remaining)"
    return True, "ok"


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _subscriber_count(bus: Any) -> int:
    if bus is None:
        return 0
    subs = getattr(bus, "_subs", None)
    try:
        return len(subs) if subs is not None else 0
    except TypeError:  # pragma: no cover - defensive
        return 0


def _recent_duplicate(db: Any, source: str, title: str, window_s: float) -> str | None:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                           time.localtime(time.time() - float(window_s)))
    row = db.query_one(
        "SELECT id FROM alerts WHERE source=? AND title=? AND acknowledged=0 "
        "AND created_at>=? ORDER BY created_at DESC LIMIT 1",
        (source, title, cutoff),
    )
    return row["id"] if row else None


def _row_to_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["acknowledged"] = bool(out.get("acknowledged"))
    out["evidence"] = _loads(out.get("evidence"))
    out["delivered"] = _loads(out.get("delivered"))
    return out


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return json.dumps({"repr": repr(value)})


def _loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value
