"""Durable task controller.

Owns the lifecycle: received -> validated -> planning -> awaiting-authorization
-> running -> verifying -> succeeded | failed | blocked | cancelled |
outcome-unknown. Guarantees:

  * an attempt row is committed BEFORE any external effect is attempted;
  * an approval is requested and recorded for gated tools;
  * the verifier read back the effect before "succeeded" is claimed;
  * on restart, attempts left 'running' are reconciled, not blindly replayed.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..ids import attempt_id, new_id, now_iso, task_id
from ..logging_setup import get_logger, redact
from ..tools.base import SideEffect, ToolContext, ToolResult

log = get_logger("jarvis.tasks")

__all__ = ["TaskController", "Task", "Attempt"]

TERMINAL = {"succeeded", "failed", "cancelled", "blocked"}


@dataclass
class Task:
    id: str
    request: str
    source: str = "text"
    user: str = "owner"
    goal: str | None = None
    status: str = "received"
    plan: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    session_id: str | None = None


@dataclass
class Attempt:
    id: str
    task_id: str
    tool: str
    args: dict[str, Any]
    side_effect: str = "none"
    step_index: int = 0
    status: str = "pending"


class ApprovalRequired(Exception):
    def __init__(self, approval_id: str, tool: str) -> None:
        super().__init__(f"approval required for {tool}")
        self.approval_id = approval_id
        self.tool = tool


class TaskController:
    def __init__(self, db: Any, bus: Any, registry: Any, policy: Any,
                 verifier: Any, config: Any) -> None:
        self.db = db
        self.bus = bus
        self.registry = registry
        self.policy = policy
        self.verifier = verifier
        self.config = config
        self._pending_approvals: dict[str, asyncio.Future[str]] = {}
        self._cancels: set[str] = set()
        # Populated by the kernel: shared services tools may reach (secrets,
        # nodes, discovery...). Authority is still resolved by the broker.
        self.services: dict[str, Any] = {}

    # -- lifecycle -------------------------------------------------------
    def create(self, request: str, *, source: str = "text", user: str = "owner",
               session_id: str | None = None) -> Task:
        t = Task(id=task_id(), request=request, source=source, user=user,
                 session_id=session_id)
        self.db.execute(
            "INSERT INTO tasks(id,created_at,updated_at,source,user,request,status,session_id)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (t.id, now_iso(), now_iso(), source, user, request, t.status, session_id),
        )
        self.bus.publish("task.created", task_id=t.id, request=request, source=source)
        self.db.audit("task.create", target=t.id, detail={"request": request, "source": source})
        return t

    def set_status(self, t: Task, status: str, **extra: Any) -> None:
        t.status = status
        self.db.execute(
            "UPDATE tasks SET status=?, updated_at=?, goal=COALESCE(?, goal),"
            " result=COALESCE(?, result), error=COALESCE(?, error) WHERE id=?",
            (status, now_iso(), extra.get("goal"),
             json.dumps(extra["result"], default=str) if "result" in extra else None,
             extra.get("error"), t.id),
        )
        self.bus.publish("task.status", task_id=t.id, status=status,
                         **{k: v for k, v in extra.items() if k != "result"})

    def cancel(self, tid: str) -> bool:
        self._cancels.add(tid)
        row = self.db.query_one("SELECT status FROM tasks WHERE id=?", (tid,))
        if row and row["status"] not in TERMINAL:
            self.db.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=?",
                            (now_iso(), tid))
            self.bus.publish("task.status", task_id=tid, status="cancelled")
            self.db.audit("task.cancel", target=tid)
            return True
        return False

    # -- attempts --------------------------------------------------------
    def _open_attempt(self, t: Task, tool: str, args: dict[str, Any],
                      side_effect: str, step_index: int) -> Attempt:
        a = Attempt(id=attempt_id(), task_id=t.id, tool=tool,
                    args=redact(args), side_effect=side_effect, step_index=step_index)
        # committed before any effect is attempted
        self.db.execute(
            "INSERT INTO attempts(id,task_id,step_index,tool,args,side_effect,status,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (a.id, t.id, step_index, tool, json.dumps(a.args, default=str),
             side_effect, "running", now_iso(), now_iso()),
        )
        return a

    def _settle_attempt(self, a: Attempt, result: ToolResult,
                        evidence: dict[str, Any]) -> None:
        status = "succeeded" if result.ok and evidence.get("verified") else (
            "completed-unverified" if result.ok else "failed")
        self.db.execute(
            "UPDATE attempts SET status=?, updated_at=?, evidence=?, error=? WHERE id=?",
            (status, now_iso(), json.dumps(evidence, default=str),
             result.error, a.id),
        )

    # -- approvals -------------------------------------------------------
    def _request_approval(self, t: Task, tool: str, args: dict[str, Any],
                          side_effect: str, reason: str) -> str:
        aid = new_id("apr")
        self.db.execute(
            "INSERT INTO approvals(id,created_at,task_id,tool,args,side_effect,reason)"
            " VALUES(?,?,?,?,?,?,?)",
            (aid, now_iso(), t.id, tool, json.dumps(redact(args), default=str),
             side_effect, reason),
        )
        self.bus.publish("approval.requested", approval_id=aid, task_id=t.id,
                         tool=tool, args=redact(args), side_effect=side_effect,
                         reason=reason)
        self.db.audit("approval.requested", target=tool, detail={"approval_id": aid,
                      "task_id": t.id}, severity="warn")
        return aid

    async def await_approval(self, aid: str, timeout: float) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_approvals[aid] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.resolve_approval(aid, "timeout", by="system")
            return "timeout"
        finally:
            self._pending_approvals.pop(aid, None)

    def resolve_approval(self, aid: str, decision: str, *, by: str = "owner",
                         remember_for_s: float | None = None,
                         remember_session: bool = False) -> bool:
        row = self.db.query_one("SELECT * FROM approvals WHERE id=?", (aid,))
        if row is None or row["decision"] != "pending":
            return False
        self.db.execute(
            "UPDATE approvals SET decision=?, decided_by=?, resolved_at=? WHERE id=?",
            (decision, by, now_iso(), aid),
        )
        if decision == "allow" and remember_session:
            self.policy.add_session_grant(row["tool"], target=None,
                                          note=f"from approval {aid}")
        elif decision == "allow" and remember_for_s:
            self.policy.add_grant(row["tool"], target=None, ttl_s=remember_for_s,
                                  note=f"from approval {aid}")
        fut = self._pending_approvals.get(aid)
        if fut and not fut.done():
            fut.set_result(decision)
        self.bus.publish("approval.resolved", approval_id=aid, decision=decision, by=by)
        self.db.audit("approval.resolved", target=row["tool"],
                      detail={"approval_id": aid, "decision": decision, "by": by},
                      severity="warn")
        return True

    def pending_approvals(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM approvals WHERE decision='pending' ORDER BY created_at")]

    # -- the single effect path -----------------------------------------
    async def execute_tool(self, t: Task, tool_name: str, args: dict[str, Any],
                           step_index: int = 0, ctx_extra: dict[str, Any] | None = None,
                           ) -> tuple[ToolResult, dict[str, Any]]:
        """Run one tool under policy + approval + verification. Sole effect path."""
        tool = self.registry.get(tool_name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown tool '{tool_name}'"), {"verified": False}

        if t.id in self._cancels:
            return ToolResult(ok=False, error="task cancelled",
                              certainty="unknown"), {"verified": False}

        # Preflight: a tool may refuse an impossible action *before* we ask the
        # owner for approval. It must not act; it only validates.
        pre_ctx = ToolContext(
            config=self.config, db=self.db, bus=self.bus, tool_name=tool_name,
            user=t.user, services=dict(self.services), secrets=self._secret_lookup,
        )
        try:
            pre = (tool.preflight(pre_ctx, **args)
                   if callable(getattr(tool, "preflight", None)) else None)
        except Exception as exc:  # a broken preflight must not block the tool
            log.warning("preflight for %s raised: %s", tool_name, exc)
            pre = None
        if isinstance(pre, ToolResult) and not pre.ok:
            self.db.audit("tool.preflight_refused", target=tool_name,
                          detail={"error": pre.error, "task_id": t.id}, severity="warn")
            return pre, {"verified": False, "method": "preflight"}

        side = tool.spec.side_effect
        decision = self.policy.decide(tool_name, side, args)

        if not decision.allow and decision.needs_approval:
            aid = self._request_approval(t, tool_name, args, side.value, decision.reason)
            self.set_status(t, "awaiting-authorization", goal=t.goal)
            verdict = await self.await_approval(aid, self.config.policy.approval_timeout_s)
            if verdict != "allow":
                self.bus.publish("task.blocked", task_id=t.id, tool=tool_name,
                                 reason=f"approval {verdict}")
                return (ToolResult(ok=False, error=f"not authorised ({verdict})",
                                   certainty="unknown"),
                        {"verified": False, "method": "denied"})
        elif not decision.allow:
            self.db.audit("policy.deny", target=tool_name,
                          detail={"reason": decision.reason, "task_id": t.id},
                          severity="warn")
            return (ToolResult(ok=False, error=f"denied by policy: {decision.reason}",
                               certainty="unknown"),
                    {"verified": False, "method": "denied"})

        self.policy.consume_grant(decision.grant_id)

        attempt = self._open_attempt(t, tool_name, args, side.value, step_index)
        # Kernel services are the base; per-call extras may add, never remove.
        svc = dict(self.services)
        svc.update((ctx_extra or {}).get("services", {}) or {})
        ctx = ToolContext(
            config=self.config, db=self.db, bus=self.bus,
            tool_name=tool_name, task_id=t.id, attempt_id=attempt.id, user=t.user,
            log=log, secrets=self._secret_lookup, emit=self._emit_for(t),
            services=svc,
        )
        t0 = time.perf_counter()
        try:
            result = await self.registry.call(tool_name, ctx, **args)
        except Exception as exc:  # a tool bug must not crash the controller
            log.exception("tool %s raised", tool_name)
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                                certainty="unknown")
        result.duration_ms = (time.perf_counter() - t0) * 1000.0

        evidence = await self.verifier.verify(tool_name, ctx, result)
        self._settle_attempt(attempt, result, evidence)

        self.bus.publish("tool.result", task_id=t.id, tool=tool_name,
                         ok=result.ok, duration_ms=round(result.duration_ms, 1),
                         side_effect=side.value, verified=bool(evidence.get("verified")),
                         certainty=result.certainty)
        self.db.audit("tool.call", target=tool_name,
                      detail={"ok": result.ok, "task_id": t.id,
                              "verified": evidence.get("verified"),
                              "duration_ms": round(result.duration_ms, 1)},
                      severity="info" if result.ok else "warn")
        if ctx_extra:
            ctx_extra.update({"attempt_id": attempt.id})
        return result, evidence

    def _secret_lookup(self, name: str) -> str | None:
        store = self.services.get("secrets")
        return store.get(name) if store else None

    def _emit_for(self, t: Task):
        def _emit(topic: str, data: dict[str, Any]) -> None:
            self.bus.publish(topic, task_id=t.id, **data)
        return _emit

    # -- restart reconciliation -----------------------------------------
    def reconcile(self) -> dict[str, Any]:
        """Called on startup: settle attempts left 'running' after a crash."""
        stuck = self.db.query(
            "SELECT a.id, a.task_id, a.tool FROM attempts a WHERE a.status='running'")
        for row in stuck:
            self.db.execute(
                "UPDATE attempts SET status='outcome-unknown', updated_at=? WHERE id=?",
                (now_iso(), row["id"]))
            self.db.execute(
                "UPDATE tasks SET status='outcome-unknown', updated_at=? WHERE id=?",
                (now_iso(), row["task_id"]))
        if stuck:
            self.db.audit("task.reconcile", detail={"count": len(stuck)},
                          severity="warn")
            self.bus.publish("task.reconcile", count=len(stuck))
        return {"reconciled": len(stuck)}
