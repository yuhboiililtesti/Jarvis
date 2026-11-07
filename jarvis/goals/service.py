"""Durable goals: plan once, execute step by step, survive a restart.

The planner is the part the earlier system lacked: a request is decomposed into a
bounded list of steps, persisted, and executed by the runtime. Every step still
goes through :class:`TaskController`, so policy, approval, DEFCON and the verifier
apply exactly as for a spoken request. A step that needs approval waits like any
other task; a crash leaves ``running`` steps as ``outcome-unknown`` and never
replays them.

Step kinds:

  * ``tool``    — one registered tool with JSON arguments;
  * ``request`` — a sub-task handed to the agent (for reasoning that needs tools);
  * ``answer``  — the final text; ends the goal.

Plans are **data**: the model may only name tools that exist, and the executor
never runs a step the model did not put in the stored plan.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..ids import new_id, now_iso
from ..llm import ChatMessage
from ..logging_setup import get_logger

log = get_logger("jarvis.goals")

__all__ = ["GoalService", "PLANNER_SYSTEM"]

PLANNER_SYSTEM = """You are JARVIS's planner. Decompose the owner's request into \
at most {max_steps} executable steps.

Return ONLY a JSON object, no markdown fences:
{{"steps": [
  {{"kind": "tool", "tool": "<exact tool name>", "args": {{}}, "why": "short reason"}},
  {{"kind": "request", "prompt": "a self-contained sub-task", "why": "short reason"}},
  {{"kind": "answer", "text": "the final reply to the owner", "why": "short reason"}}
]}}

Rules:
- Use ONLY the tool names listed. Never invent a tool or a parameter.
- One action per step. Put dependent actions in order.
- Prefer read-only tools; include a mutating step only when the request asks for it.
- Keep the plan minimal. If the request is a single question, return one "answer" step.
- The final "answer" step is what the owner sees."""

_TERMINAL = ("succeeded", "failed", "cancelled", "outcome-unknown")
_KINDS = ("tool", "request", "answer")
_PLACEHOLDER = re.compile(r"\[[A-Za-z_][A-Za-z0-9 _-]*\]")
_GENERIC_ANSWER = {"done", "done.", "completed", "completed.", "finished", "ok", "okay"}


def _needs_synthesis(text: str) -> bool:
    """A closing that carries no information should be written from the results."""
    return text.strip().lower() in _GENERIC_ANSWER or bool(_PLACEHOLDER.search(text))


def _parse_plan(text: str) -> list[dict[str, Any]] | None:
    """Extract a ``{"steps": [...]}`` object from model output, or None."""
    raw = (text or "").strip()
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return None
    return [s for s in steps if isinstance(s, dict)]


class GoalService:
    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self.db = kernel.db
        self.config = kernel.config
        self.cfg = kernel.config.goals
        self._tasks: set[asyncio.Task] = set()
        self._active: set[str] = set()

    # -- planning --------------------------------------------------------
    def _catalogue(self) -> str:
        lines = []
        for spec in self.kernel.registry.specs():
            lines.append(f"- {spec.name}: {spec.summary}")
        return "\n".join(lines)

    async def plan(self, request: str) -> list[dict[str, Any]]:
        """Ask the planner model; fall back to a single agent sub-task if it fails."""
        request = str(request or "").strip()
        if not request:
            raise ValueError("request is required")
        messages = [
            ChatMessage("system", PLANNER_SYSTEM.format(max_steps=int(self.cfg.max_steps))),
            ChatMessage("user", f"TOOLS:\n{self._catalogue()}\n\nREQUEST: {request}"),
        ]
        text: list[str] = []
        try:
            async for ev in self.kernel.router.chat(
                    messages, temperature=0.2, num_ctx=4096, num_predict=700):
                if ev.kind == "token":
                    text.append(ev.text)
                elif ev.kind == "error":
                    log.warning("planner model error: %s", ev.error)
                    break
        except Exception as exc:
            log.warning("planner call failed: %s", exc)
        steps = self._validated(_parse_plan("".join(text)), request)
        return steps

    def _validated(self, raw: list[dict[str, Any]] | None,
                   request: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for step in raw or []:
            kind = str(step.get("kind") or "").lower()
            if kind not in _KINDS:
                continue
            if kind == "tool":
                name = str(step.get("tool") or "").strip()
                args = step.get("args") or {}
                if not name or self.kernel.registry.get(name) is None:
                    continue
                if not isinstance(args, dict) or "ctx" in args:
                    continue
                out.append({"kind": "tool", "tool": name, "args": args,
                            "why": str(step.get("why") or "")[:200]})
            elif kind == "request":
                prompt = str(step.get("prompt") or "").strip()
                if prompt:
                    out.append({"kind": "request", "prompt": prompt,
                                "why": str(step.get("why") or "")[:200]})
            else:
                text = str(step.get("text") or "").strip()
                if text:
                    out.append({"kind": "answer", "text": text,
                                "why": str(step.get("why") or "")[:200]})
            if len(out) >= int(self.cfg.max_steps):
                break
        if not out:
            # Honest fallback: let the agent try the request as one sub-task.
            out = [{"kind": "request", "prompt": request,
                    "why": "planner fallback: no valid plan"}]
        if out[-1]["kind"] != "answer":
            out.append({"kind": "answer", "text": "Done.",
                        "why": "closing step"})
        return out[: int(self.cfg.max_steps)]

    # -- persistence -----------------------------------------------------
    def create(self, request: str, *, user: str = "owner",
               session_id: str | None = None, steps: list[dict[str, Any]]) -> dict[str, Any]:
        request = str(request or "").strip()
        if not request:
            raise ValueError("request is required")
        if not steps:
            raise ValueError("a plan needs at least one step")
        goal_id = new_id("goal")
        ts = now_iso()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO goals(id,created_at,updated_at,request,user,session_id,"
                "status,plan,steps_total) VALUES(?,?,?,?,?,?,?,?,?)",
                (goal_id, ts, ts, request, user, session_id, "planned",
                 json.dumps({"steps": steps}, ensure_ascii=False), len(steps)))
            for ordinal, step in enumerate(steps):
                conn.execute(
                    "INSERT INTO goal_steps(id,goal_id,ordinal,kind,tool,args,prompt,"
                    "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (new_id("step"), goal_id, ordinal, step["kind"],
                     step.get("tool"),
                     json.dumps(step.get("args") or {}, ensure_ascii=False)
                     if step["kind"] == "tool" else None,
                     step.get("prompt") or step.get("text"),
                     "pending", ts, ts))
        self.db.audit("goal.create", target=goal_id,
                      detail={"steps": len(steps), "request": request[:200]})
        return self.status(goal_id)

    def _goal(self, goal_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM goals WHERE id=?", (goal_id,))
        return dict(row) if row else None

    def status(self, goal_id: str) -> dict[str, Any]:
        goal = self._goal(goal_id)
        if goal is None:
            return {"id": goal_id, "status": "missing", "steps": []}
        steps = [dict(r) for r in self.db.query(
            "SELECT id,ordinal,kind,tool,args,prompt,status,attempts,result,error "
            "FROM goal_steps WHERE goal_id=? ORDER BY ordinal", (goal_id,))]
        for step in steps:
            for key in ("args", "result"):
                if step.get(key):
                    try:
                        step[key] = json.loads(step[key])
                    except (TypeError, json.JSONDecodeError):
                        pass
        goal["steps"] = steps
        if goal.get("plan"):
            try:
                goal["plan"] = json.loads(goal["plan"])
            except (TypeError, json.JSONDecodeError):
                pass
        return goal

    def list_goals(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id,created_at,updated_at,request,status,steps_total,steps_done,error "
            "FROM goals ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 100),))
        return [dict(r) for r in rows]

    def cancel(self, goal_id: str) -> bool:
        goal = self._goal(goal_id)
        if goal is None or goal["status"] in _TERMINAL:
            return False
        ts = now_iso()
        with self.db.tx() as conn:
            conn.execute("UPDATE goals SET status='cancelled', updated_at=?, error=? "
                         "WHERE id=?", (ts, "cancelled by owner", goal_id))
            conn.execute("UPDATE goal_steps SET status='cancelled', updated_at=? "
                         "WHERE goal_id=? AND status IN ('pending','running')", (ts, goal_id))
        self.db.audit("goal.cancel", target=goal_id, severity="warn")
        return True

    # -- execution -------------------------------------------------------
    async def advance(self, goal_id: str) -> dict[str, Any]:
        """Execute the next pending step once."""
        goal = self._goal(goal_id)
        if goal is None or goal["status"] in _TERMINAL:
            return self.status(goal_id)
        step = self.db.query_one(
            "SELECT * FROM goal_steps WHERE goal_id=? AND status='pending' "
            "ORDER BY ordinal LIMIT 1", (goal_id,))
        if step is None:
            self._refresh_goal(goal_id)
            return self.status(goal_id)
        ts = now_iso()
        claimed = self.db.execute(
            "UPDATE goal_steps SET status='running', updated_at=? "
            "WHERE id=? AND status='pending'", (ts, step["id"]))
        if claimed.rowcount == 0:
            return self.status(goal_id)
        self.db.execute("UPDATE goals SET status='running', updated_at=? WHERE id=?",
                        (ts, goal_id))
        try:
            out = await self._run_step(dict(step), goal)
        except Exception as exc:  # a step can never crash the service
            out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        if out.get("ok"):
            self.db.execute(
                "UPDATE goal_steps SET status='succeeded', result=?, error=NULL, "
                "updated_at=? WHERE id=?",
                (json.dumps(out.get("result") or {}, ensure_ascii=False), now_iso(),
                 step["id"]))
        else:
            attempts = int(step["attempts"] or 0) + 1
            max_attempts = max(1, int(self.cfg.max_attempts))
            status = "pending" if attempts < max_attempts else "failed"
            self.db.execute(
                "UPDATE goal_steps SET status=?, attempts=?, error=?, updated_at=? "
                "WHERE id=?",
                (status, attempts, str(out.get("error") or "")[:1000], now_iso(),
                 step["id"]))
            if status == "failed":
                self.db.audit("goal.step_failed", target=goal_id,
                              detail={"step": step["ordinal"], "error": out.get("error")},
                              severity="warn")
        self._refresh_goal(goal_id)
        return self.status(goal_id)

    async def _run_step(self, step: dict[str, Any],
                        goal: dict[str, Any]) -> dict[str, Any]:
        kind = step["kind"]
        if kind == "answer":
            text = str(step.get("prompt") or "").strip()
            if self.cfg.synthesize_answers and _needs_synthesis(text):
                text = await self._synthesize(goal, text)
            return {"ok": True, "result": {"text": text}}
        if kind == "request":
            agent = getattr(self.kernel, "agent", None)
            if agent is None:
                return {"ok": False, "error": "no agent configured"}
            session = self.kernel.session(f"goal:{goal['id']}")
            final, final_status = "", "succeeded"
            async for ev in agent.run_turn(str(step.get("prompt") or ""),
                                           session=session, source="schedule"):
                if ev.get("kind") == "final":
                    final = ev.get("text") or ""
                    final_status = ev.get("status") or "succeeded"
            if final_status != "succeeded":
                return {"ok": False, "error": f"sub-task ended '{final_status}'"}
            return {"ok": True, "result": {"text": final[:1000]}}
        # tool
        controller = getattr(self.kernel, "controller", None)
        if controller is None:
            return {"ok": False, "error": "no controller"}
        try:
            args = json.loads(step.get("args") or "{}")
        except (TypeError, json.JSONDecodeError):
            return {"ok": False, "error": "stored step arguments are not valid JSON"}
        task = controller.create(f"goal {goal['id']} step {step['ordinal']}: "
                                 f"{step.get('tool')}", source="schedule")
        result, evidence = await controller.execute_tool(
            task, str(step.get("tool")), args)
        controller.set_status(task, "succeeded" if result.ok else "failed",
                              result={"ok": result.ok,
                                      "verified": bool(evidence.get("verified")),
                                      "brief": result.brief()},
                              error=result.error)
        if not result.ok:
            return {"ok": False, "error": result.error or "tool failed"}
        return {"ok": True, "result": {"tool": step.get("tool"),
                                       "verified": bool(evidence.get("verified")),
                                       "data": result.data if isinstance(result.data, (dict, list))
                                       else result.brief()}}

    async def _synthesize(self, goal: dict[str, Any], draft: str) -> str:
        """Turn step results into the final answer when the draft has a placeholder."""
        rows = self.db.query(
            "SELECT kind, tool, result FROM goal_steps WHERE goal_id=? "
            "AND status='succeeded' AND result IS NOT NULL ORDER BY ordinal",
            (goal["id"],))
        results = []
        for row in rows:
            try:
                results.append(json.loads(row["result"]))
            except (TypeError, json.JSONDecodeError):
                continue
        if not results:
            return draft
        messages = [
            ChatMessage("system",
                        "You are JARVIS. Write the final reply to the owner using ONLY the "
                        "executed step results. Be concise and concrete. Never invent a result."),
            ChatMessage("user",
                        f"Request: {goal.get('request')}\n"
                        f"Step results: {json.dumps(results, ensure_ascii=False, default=str)[:3000]}\n"
                        f"Draft: {draft}\nWrite the final reply."),
        ]
        buf: list[str] = []
        try:
            async for ev in self.kernel.router.chat(
                    messages, temperature=0.2, num_predict=300):
                if ev.kind == "token":
                    buf.append(ev.text)
                elif ev.kind == "error":
                    return draft
        except Exception as exc:  # pragma: no cover - a failed summary keeps the draft
            log.warning("goal answer synthesis failed: %s", exc)
            return draft
        text = "".join(buf).strip()
        return text or draft

    def _refresh_goal(self, goal_id: str) -> None:
        rows = self.db.query("SELECT status, error FROM goal_steps WHERE goal_id=?",
                             (goal_id,))
        statuses = [r["status"] for r in rows]
        done = sum(1 for s in statuses if s in ("succeeded", "skipped"))
        failed = next((r["error"] for r in rows if r["status"] == "failed"), None)
        pending = any(s in ("pending", "running") for s in statuses)
        if failed:
            status, error = "failed", failed
        elif pending:
            status, error = "running", None
        elif statuses and all(s in ("succeeded", "skipped") for s in statuses):
            status, error = "succeeded", None
        else:
            status, error = "failed", "no executable steps"
        last = self.db.query_one(
            "SELECT result FROM goal_steps WHERE goal_id=? AND kind='answer' "
            "ORDER BY ordinal DESC LIMIT 1", (goal_id,))
        result = None
        if last and last["result"]:
            try:
                result = json.loads(last["result"]).get("text")
            except (TypeError, json.JSONDecodeError, AttributeError):
                result = None
        self.db.execute(
            "UPDATE goals SET status=?, updated_at=?, steps_done=?, error=?, "
            "result=COALESCE(?, result) WHERE id=?",
            (status, now_iso(), done, error, result, goal_id))
        if status in ("succeeded", "failed"):
            self.db.audit(f"goal.{status}", target=goal_id,
                          detail={"steps": len(statuses), "done": done},
                          severity="info" if status == "succeeded" else "warn")

    async def run_pending(self, limit: int | None = None) -> list[str]:
        """Advance up to ``limit`` planned goals (one step each) in the background.

        A goal with a step already running is skipped, so two ticks can never run
        two steps of the same goal at once.
        """
        if not self.cfg.enabled or not self.cfg.auto_run:
            return []
        rows = self.db.query(
            "SELECT g.id FROM goals g WHERE g.status IN ('planned','running') "
            "AND NOT EXISTS (SELECT 1 FROM goal_steps s WHERE s.goal_id=g.id "
            "AND s.status='running') ORDER BY g.created_at LIMIT ?",
            (max(1, int(limit or self.cfg.per_tick)),))
        started: list[str] = []
        for row in rows:
            goal_id = row["id"]
            if goal_id in self._active:
                continue
            self._active.add(goal_id)
            task = asyncio.create_task(self._advance_guarded(goal_id),
                                       name=f"goal:{goal_id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            started.append(goal_id)
        return started

    async def _advance_guarded(self, goal_id: str) -> None:
        try:
            await self.advance(goal_id)
        finally:
            self._active.discard(goal_id)

    async def stop(self) -> None:
        self.cancel_all()
        await self.wait_idle(5.0)
        self._active.clear()

    async def wait_idle(self, timeout: float = 30.0) -> None:
        pending = set(self._tasks)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    def reconcile(self) -> int:
        """A step interrupted by a crash is not replayed."""
        ts = now_iso()
        cur = self.db.execute(
            "UPDATE goal_steps SET status='outcome-unknown', updated_at=?, "
            "error=COALESCE(error,'interrupted; not retried because it may have run') "
            "WHERE status='running'", (ts,))
        count = int(cur.rowcount or 0)
        if count:
            self.db.execute(
                "UPDATE goals SET status='outcome-unknown', updated_at=?, "
                "error='interrupted while a step was running' "
                "WHERE status IN ('planned','running')", (ts,))
            self.db.audit("goal.reconcile", detail={"count": count}, severity="warn")
        return count

    # -- reporting -------------------------------------------------------
    def health(self) -> dict[str, Any]:
        if not self.cfg.enabled:
            return {"available": True, "reason": "goals disabled by config (not a fault)"}
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM goals WHERE status IN ('planned','running')")
        active = int(row["n"]) if row else 0
        return {"available": True,
                "reason": f"{active} active goal(s); max {self.cfg.max_steps} steps, "
                          f"{self.cfg.max_attempts} attempts"}
