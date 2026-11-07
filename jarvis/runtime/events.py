"""Event engine: persisted events, safe rules, and reactions through the normal gate.

The in-process :class:`~jarvis.events.EventBus` is a telemetry seam — its history
is a 300-item ring and it disappears on restart. This module is the durable half:
an event is written to ``runtime_events``, matched against declarative rules, and
each match is claimed in ``event_fires`` with ``UNIQUE(rule_id, event_id)`` so the
same event can never trigger the same rule twice, even across a restart.

Rules are **data, not code**. A condition is a small JSON predicate
(``{"field": "cpu", "op": ">=", "value": 90}``) with a fixed operator set — no
``eval``, no expressions. A corrupt condition **skips** the rule: it must never
turn a narrow rule into a wildcard. Conditions can inspect ``source`` and
``topic`` as well as the payload, so a rule can distinguish a real bus alert from
an event posted over HTTP.

Reactions still go through :meth:`TaskController.execute_tool`, so policy,
approval, DEFCON and the verifier apply exactly as they do for a spoken request.
An event cannot bypass the gate; it can at most *ask* for something that then
needs the owner's approval.

A per-rule cooldown bounds feedback loops (a reaction that produces another
event). A ``notify`` action writes an alert row directly — that is a durable,
approval-free notification visible in the HUD — and only attempts a desktop toast
when policy already permits it without asking.
"""

from __future__ import annotations

import fnmatch
import json
from datetime import datetime, timedelta
from typing import Any

from ..ids import new_id, now_iso
from ..logging_setup import get_logger
from ..tools.base import SideEffect

log = get_logger("jarvis.runtime.events")

__all__ = ["EventEngine", "match_condition", "RULE_KINDS", "RULE_OPS"]

RULE_KINDS = ("tool", "notify", "request")
RULE_OPS = ("==", "!=", ">", ">=", "<", "<=", "contains", "startswith", "exists")
_SEVERITIES = ("info", "warn", "critical")


def _value_at(data: Any, path: str | None) -> Any:
    if not path:
        return data
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def match_condition(condition: Any, data: dict[str, Any]) -> bool:
    """Evaluate one declarative predicate. Unknown operators fail closed."""
    if not condition:
        return True
    if not isinstance(condition, dict):
        return False
    op = str(condition.get("op") or "==")
    got = _value_at(data, condition.get("field"))
    want = condition.get("value")
    if op == "exists":
        return got is not None
    if op == "contains":
        if isinstance(got, (list, tuple, set, dict)):
            return want in got
        return str(want) in str(got if got is not None else "")
    if op == "startswith":
        return str(got if got is not None else "").startswith(str(want))
    a, b = _num(got), _num(want)
    if a is not None and b is not None:
        got, want = a, b
    try:
        if op == "==":
            return got == want
        if op == "!=":
            return got != want
        if op == ">":
            return got > want
        if op == ">=":
            return got >= want
        if op == "<":
            return got < want
        if op == "<=":
            return got <= want
    except TypeError:
        return False
    return False


def _parse_action(raw: Any) -> dict[str, Any]:
    try:
        action = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        raise ValueError("action is not valid JSON") from None
    if not isinstance(action, dict):
        raise ValueError("action must be an object")
    kind = str(action.get("kind") or "")
    if kind not in RULE_KINDS:
        raise ValueError(f"action.kind must be one of {', '.join(RULE_KINDS)}")
    if kind == "tool":
        if not str(action.get("tool") or "").strip():
            raise ValueError("a tool action needs 'tool'")
        if not isinstance(action.get("args") or {}, dict):
            raise ValueError("'args' must be an object")
    elif kind == "notify":
        if not str(action.get("title") or "").strip():
            raise ValueError("a notify action needs 'title'")
        if str(action.get("severity") or "info") not in _SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(_SEVERITIES)}")
    else:
        if not str(action.get("prompt") or "").strip():
            raise ValueError("a request action needs 'prompt'")
    return action


def _seconds_since(iso: Any) -> float | None:
    try:
        then = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    now = datetime.now().astimezone() if then.tzinfo else datetime.now()
    try:
        return (now - then).total_seconds()
    except TypeError:
        return None


class EventEngine:
    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self.db = kernel.db
        self.config = kernel.config

    # -- ingest ----------------------------------------------------------
    def ingest(self, topic: str, data: Any = None, source: str = "api",
               dedupe_key: str | None = None) -> dict[str, Any]:
        """Persist one event and queue every matching rule (idempotently)."""
        topic = str(topic or "").strip()
        if not topic:
            raise ValueError("topic is required")
        payload = data if isinstance(data, dict) else ({"value": data} if data is not None else {})
        dedupe = str(dedupe_key).strip() if dedupe_key else None
        ts = now_iso()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO runtime_events"
            "(ts,topic,source,data,dedupe_key,created_at) VALUES(?,?,?,?,?,?)",
            (ts, topic, source,
             json.dumps(payload, ensure_ascii=False, default=str), dedupe, ts))
        if cur.rowcount == 0 and dedupe:
            existing = self.db.query_one(
                "SELECT id FROM runtime_events WHERE dedupe_key=?", (dedupe,))
            return {"event_id": int(existing["id"]) if existing else None,
                    "topic": topic, "duplicate": True, "matched": 0, "queued": []}
        event_id = int(cur.lastrowid)

        view = {**payload, "source": source, "topic": topic}
        queued: list[str] = []
        for rule in self._enabled_rules():
            if not fnmatch.fnmatchcase(topic, rule["topic"]):
                continue
            try:
                condition = json.loads(rule["condition"]) if rule["condition"] else None
            except (TypeError, json.JSONDecodeError):
                # Fail closed: a corrupt condition must not become a wildcard.
                log.warning("rule %s has an unreadable condition; skipped", rule["name"])
                continue
            if not match_condition(condition, view):
                continue
            if self._cooling_down(rule["id"]):
                continue
            fire_id = new_id("fire")
            ins = self.db.execute(
                "INSERT OR IGNORE INTO event_fires"
                "(id,rule_id,event_id,status,created_at) VALUES(?,?,?,?,?)",
                (fire_id, rule["id"], event_id, "pending", ts))
            if ins.rowcount:
                queued.append(fire_id)
        if queued:
            self.db.audit("event.matched", target=topic,
                          detail={"event": event_id, "queued": len(queued)})
        return {"event_id": event_id, "topic": topic, "matched": len(queued),
                "queued": queued}

    def _cooling_down(self, rule_id: str) -> bool:
        """True while a rule's most recent fire is inside its cooldown window."""
        cooldown = float(getattr(self.config.runtime, "rule_cooldown_s", 0) or 0)
        if cooldown <= 0:
            return False
        row = self.db.query_one(
            "SELECT created_at FROM event_fires WHERE rule_id=? ORDER BY rowid DESC LIMIT 1",
            (rule_id,))
        if row is None:
            return False
        age = _seconds_since(row["created_at"])
        return age is not None and age < cooldown

    def _enabled_rules(self) -> list[Any]:
        return self.db.query("SELECT * FROM event_rules WHERE enabled=1 ORDER BY created_at")

    # -- reactions -------------------------------------------------------
    def pending(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT f.id, f.rule_id, f.event_id, f.created_at, r.name, r.action "
            "FROM event_fires f JOIN event_rules r ON r.id=f.rule_id "
            "WHERE f.status='pending' AND r.enabled=1 ORDER BY f.rowid ASC LIMIT ?",
            (min(max(int(limit), 1), 100),))
        return [dict(r) for r in rows]

    async def execute_fire(self, fire: dict[str, Any]) -> dict[str, Any]:
        """Claim and run one queued reaction. Re-claims are refused atomically."""
        claimed = self.db.execute(
            "UPDATE event_fires SET status='running' WHERE id=? AND status='pending'",
            (fire["id"],))
        if claimed.rowcount == 0:
            return {"ok": False, "skipped": True, "fire_id": fire["id"]}
        # The rule may have been removed or disabled since the fire was queued.
        rule = self.db.query_one("SELECT enabled FROM event_rules WHERE id=?",
                                 (fire["rule_id"],))
        if rule is None or not rule["enabled"]:
            self._finish(fire["id"], "cancelled", "rule removed or disabled")
            return {"ok": False, "skipped": True, "fire_id": fire["id"],
                    "reason": "rule removed or disabled"}
        try:
            action = _parse_action(fire["action"])
        except ValueError as exc:
            self._finish(fire["id"], "failed", str(exc))
            return {"ok": False, "fire_id": fire["id"], "error": str(exc)}

        try:
            kind = action["kind"]
            if kind == "tool":
                out = await self._run_tool(fire, action.get("tool"), action.get("args") or {})
            elif kind == "notify":
                out = await self._notify(fire, action)
            else:
                out = await self._run_request(fire, action.get("prompt"))
        except Exception as exc:
            self._finish(fire["id"], "failed", f"{type(exc).__name__}: {exc}")
            return {"ok": False, "fire_id": fire["id"], "error": f"{type(exc).__name__}: {exc}"}

        if not out.get("ok"):
            status = "failed"
        elif out.get("verified") is False:
            status = "completed-unverified"
        else:
            status = "succeeded"
        self._finish(fire["id"], status, out.get("error"))
        return {"ok": bool(out.get("ok")), "fire_id": fire["id"], **out}

    async def _run_tool(self, fire: dict[str, Any], tool_name: str,
                        args: dict[str, Any]) -> dict[str, Any]:
        controller = getattr(self.kernel, "controller", None)
        if controller is None:
            return {"ok": False, "error": "no controller"}
        task = controller.create(f"event:{fire['name']} -> {tool_name}", source="event")
        controller.set_status(task, "running", goal=f"react to event {fire['event_id']}")
        result, evidence = await controller.execute_tool(task, str(tool_name), args)
        controller.set_status(
            task, "succeeded" if result.ok else "failed",
            result={"ok": result.ok, "verified": bool(evidence.get("verified")),
                    "brief": result.brief()},
            error=result.error)
        self.db.execute("UPDATE event_fires SET task_id=? WHERE id=?", (task.id, fire["id"]))
        return {"ok": result.ok, "task_id": task.id, "error": result.error,
                "verified": bool(evidence.get("verified"))}

    async def _notify(self, fire: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
        """Write a durable alert. Visible in the HUD without needing an approval."""
        title = str(action.get("title") or "")[:200]
        body = str(action.get("message") or "")[:2000]
        severity = str(action.get("severity") or "info")
        alert_id = new_id("alert")
        self.db.execute(
            "INSERT INTO alerts(id,created_at,severity,source,title,body,evidence,"
            "acknowledged,delivered) VALUES(?,?,?,?,?,?,?,?,?)",
            (alert_id, now_iso(), severity, f"runtime:{fire['name']}", title, body,
             json.dumps({"fire_id": fire["id"], "event_id": fire["event_id"]}),
             0, json.dumps({"hud": True})))
        self.db.audit("event.alert", target=alert_id,
                      detail={"rule": fire["name"], "severity": severity})
        # The alert row is the guaranteed delivery. A desktop toast is a LOCAL_EXEC
        # effect, so it is attempted only when policy would not prompt for it.
        toast = False
        policy = getattr(self.kernel, "policy", None)
        if policy is not None and toast_supported():
            try:
                decision = policy.decide("notify.toast", SideEffect.LOCAL_EXEC)
            except Exception:  # pragma: no cover - policy must not break an alert
                decision = None
            if decision is not None and decision.allow and not decision.needs_approval:
                run = await self._run_tool(fire, "notify.toast",
                                           {"title": title, "message": body})
                toast = bool(run.get("ok"))
        return {"ok": True, "alert_id": alert_id, "verified": True, "toast": toast}

    async def _run_request(self, fire: dict[str, Any], prompt: str) -> dict[str, Any]:
        agent = getattr(self.kernel, "agent", None)
        if agent is None:
            return {"ok": False, "error": "no agent configured"}
        # One session per fire: concurrent reactions of the same rule must not
        # interleave writes into one message list.
        session = self.kernel.session(f"event:{fire['id']}")
        task_id = None
        final = ""
        final_status = "succeeded"
        async for ev in agent.run_turn(str(prompt), session=session, source="event"):
            if ev.get("kind") == "task":
                task_id = ev.get("task_id")
                self.db.execute("UPDATE event_fires SET task_id=? WHERE id=?",
                                (task_id, fire["id"]))
            elif ev.get("kind") == "final":
                final = ev.get("text") or ""
                final_status = ev.get("status") or "succeeded"
        if final_status != "succeeded":
            return {"ok": False, "task_id": task_id, "error": f"turn ended '{final_status}'"}
        return {"ok": True, "task_id": task_id, "verified": True, "text": final[:400]}

    def _finish(self, fire_id: str, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE event_fires SET status=?, finished_at=?, error=? WHERE id=?",
            (status, now_iso(), error, fire_id))
        if status not in ("succeeded", "cancelled"):
            self.db.audit("event.reaction_failed", target=fire_id,
                          detail={"status": status, "error": error}, severity="warn")

    def reconcile(self) -> int:
        """A reaction interrupted by a crash is not replayed."""
        cur = self.db.execute(
            "UPDATE event_fires SET status='outcome-unknown', finished_at=?, "
            "error=COALESCE(error,'interrupted; not retried because it may have run') "
            "WHERE status='running'", (now_iso(),))
        count = int(cur.rowcount or 0)
        if count:
            self.db.audit("event.reconcile", detail={"count": count}, severity="warn")
        return count

    def prune(self) -> dict[str, int]:
        """Keep the log bounded: drop old events/fires and orphaned fires."""
        days = max(0, int(getattr(self.config.runtime, "event_retention_days", 30) or 0))
        max_events = max(0, int(getattr(self.config.runtime, "max_events", 5000) or 0))
        removed_events = removed_fires = 0
        if days:
            cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat()
            removed_events += int(self.db.execute(
                "DELETE FROM runtime_events WHERE created_at < ?", (cutoff,)).rowcount or 0)
            removed_fires += int(self.db.execute(
                "DELETE FROM event_fires WHERE created_at < ?", (cutoff,)).rowcount or 0)
        removed_fires += int(self.db.execute(
            "DELETE FROM event_fires WHERE rule_id NOT IN (SELECT id FROM event_rules) "
            "AND status NOT IN ('pending','running')").rowcount or 0)
        if max_events:
            row = self.db.query_one("SELECT COUNT(*) AS n FROM runtime_events")
            excess = int(row["n"]) - max_events if row else 0
            if excess > 0:
                removed_events += int(self.db.execute(
                    "DELETE FROM runtime_events WHERE id IN "
                    "(SELECT id FROM runtime_events ORDER BY id ASC LIMIT ?)",
                    (excess,)).rowcount or 0)
        if removed_events or removed_fires:
            self.db.audit("event.prune", detail={"events": removed_events,
                                                 "fires": removed_fires})
        return {"events": removed_events, "fires": removed_fires}

    # -- rules -----------------------------------------------------------
    def add_rule(self, name: str, topic: str, action: Any, condition: Any = None,
                 enabled: bool = True) -> dict[str, Any]:
        name = str(name or "").strip()
        topic = str(topic or "").strip()
        if not name:
            raise ValueError("name is required")
        if not topic:
            raise ValueError("topic is required")
        parsed = _parse_action(action)
        if condition is not None and not isinstance(condition, dict):
            raise ValueError("condition must be an object or null")
        if isinstance(condition, dict):
            op = str(condition.get("op") or "==")
            if op not in RULE_OPS:
                raise ValueError(f"condition.op must be one of {', '.join(RULE_OPS)}")
            if not str(condition.get("field") or "").strip():
                raise ValueError("condition.field is required")
        if parsed.get("kind") == "tool":
            registry = getattr(self.kernel, "registry", None)
            if registry is not None and registry.get(parsed["tool"]) is None:
                raise ValueError(f"unknown tool '{parsed['tool']}'")
        rule_id = new_id("rule")
        try:
            self.db.execute(
                "INSERT INTO event_rules(id,name,topic,condition,action,enabled,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (rule_id, name, topic,
                 json.dumps(condition, ensure_ascii=False) if condition else None,
                 json.dumps(parsed, ensure_ascii=False), 1 if enabled else 0, now_iso()))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ValueError(f"a rule named '{name}' already exists") from None
            raise
        self.db.audit("event.rule_add", target=name,
                      detail={"topic": topic, "kind": parsed.get("kind"),
                              "tool": parsed.get("tool")})
        return {"id": rule_id, "name": name, "topic": topic, "condition": condition,
                "action": parsed, "enabled": bool(enabled)}

    def list_rules(self) -> list[dict[str, Any]]:
        out = []
        for row in self.db.query("SELECT * FROM event_rules ORDER BY created_at"):
            d = dict(row)
            for key in ("condition", "action"):
                try:
                    d[key] = json.loads(d[key]) if d[key] else None
                except (TypeError, json.JSONDecodeError):
                    pass
            d["enabled"] = bool(d.get("enabled"))
            out.append(d)
        return out

    def remove_rule(self, name_or_id: str) -> bool:
        key = str(name_or_id or "").strip()
        if not key:
            return False
        row = self.db.query_one("SELECT id,name FROM event_rules WHERE id=? OR name=?",
                                (key, key))
        if row is None:
            return False
        # Queued reactions for a removed rule must not fire. One already inside a
        # tool call or awaiting approval is in progress; it re-checks `enabled`
        # before it acts, so a not-yet-started one stops too.
        self.db.execute("DELETE FROM event_fires WHERE rule_id=?", (row["id"],))
        self.db.execute("DELETE FROM event_rules WHERE id=?", (row["id"],))
        self.db.audit("event.rule_remove", target=row["name"], severity="warn")
        return True

    # -- reporting -------------------------------------------------------
    def status(self, limit: int = 10) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for row in self.db.query("SELECT status, COUNT(*) AS n FROM event_fires GROUP BY status"):
            counts[row["status"]] = int(row["n"])
        events = [dict(r) for r in self.db.query(
            "SELECT id,ts,topic,source,data FROM runtime_events ORDER BY id DESC LIMIT ?",
            (min(max(limit, 1), 100),))]
        for ev in events:
            try:
                ev["data"] = json.loads(ev["data"]) if ev["data"] else {}
            except (TypeError, json.JSONDecodeError):
                pass
        return {
            "rules": self.list_rules(),
            "events_recent": events,
            "event_count": int((self.db.query_one(
                "SELECT COUNT(*) AS n FROM runtime_events") or {"n": 0})["n"]),
            "fire_counts": counts,
        }


def toast_supported() -> bool:
    """Whether a desktop notifier exists on this platform (no side effect)."""
    try:
        from ..tools.builtin import notify_tool
        return bool(notify_tool.health().get("available"))
    except Exception:  # pragma: no cover - optional module
        return False
