"""The agent turn loop.

One owner turn:
  request -> recall memory -> build prompt -> model streams
  -> model may call tools (gated by policy/approval, verified)
  -> feed results back -> final spoken/typed answer -> persist memory

Bounded by `max_steps` so a tool-loop can never run away. Cancellation is
honoured between steps. Every step is published on the bus for the HUD.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..llm import ChatMessage
from ..logging_setup import get_logger

log = get_logger("jarvis.agent")

__all__ = ["Agent", "AgentTurn"]


@dataclass
class AgentTurn:
    task_id: str
    session_id: str
    user: str = "owner"
    source: str = "text"
    messages: list[ChatMessage] = field(default_factory=list)
    steps: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None


class Agent:
    def __init__(self, *, config: Any, bus: Any, router: Any, registry: Any,
                 controller: Any, persona: Any, memory: Any,
                 max_steps: int = 6, session_store: Any = None, db: Any = None,
                 learning: Any = None) -> None:
        self.config = config
        self.bus = bus
        self.router = router
        self.registry = registry
        self.controller = controller
        self.persona = persona
        self.memory = memory
        self.max_steps = max_steps
        self.session_store = session_store
        self.db = db
        self.learning = learning

    # -- prompt assembly -------------------------------------------------
    async def _recall(self, text: str, user: str) -> str:
        if self.memory is None:
            return ""
        try:
            res = await self.memory.search(text, user=user, top_k=self.config.memory.retrieval_top_k)
        except Exception as exc:
            log.warning("recall failed: %s", exc)
            return ""
        items = res.get("items", [])
        if not items:
            return ""
        self.bus.publish("memory.recall", count=len(items), method=res["evidence"].get("method"))
        return self.memory.context_block(items)

    async def _recall_lessons(self, text: str, user: str) -> str:
        """Recall lessons learned from past outcomes and owner teaching."""
        if self.learning is None:
            return ""
        try:
            res = await self.learning.relevant(
                text, user=user, top_k=self.config.memory.retrieval_top_k)
        except Exception as exc:
            log.warning("lesson recall failed: %s", exc)
            return ""
        items = res.get("items", [])
        if not items:
            return ""
        self.bus.publish("learning.recall", count=len(items))
        return self.learning.context_block(items)

    def _tool_names(self) -> list[str]:
        return self.registry.names()

    def _routing_hint(self) -> str:
        """Tell the model which desktop apps it can actually launch.

        The tool list alone does not tell a small model that "steam" is a program
        rather than a website; this is the missing fact, stated as data.
        """
        if self.db is None:
            return ""
        try:
            from types import SimpleNamespace
            from ..tools.builtin.apps import catalog
            entries = catalog(SimpleNamespace(db=self.db))
        except Exception:  # pragma: no cover - prompt must never fail a turn
            return ""
        ready = sorted(name for name, entry in entries.items() if entry.get("installed"))
        if not ready:
            return ""
        return ("Desktop apps installed and ready to launch: " + ", ".join(ready) +
                ". For \"open <app>\" call apps.launch with app=<name>. "
                "web.open is only for an explicit http(s) URL, never for launching a program.")

    def _frontier_triggered(self, request: str) -> bool:
        """True when the request should escalate to the frontier tier.

        Escalation is opt-in and keyword-driven: only requests matching a
        configured trigger escalate, so normal chat stays local and private.
        """
        triggers = self.config.llm.frontier_trigger
        if not triggers:
            return False
        r = (request or "").lower()
        return any(t.lower() in r for t in triggers)

    def _route_tier(self, request: str, source: str) -> str:
        """Choose the model tier for this turn (frontier / fast / primary)."""
        llm = self.config.llm
        if (llm.frontier_provider and self.router.frontier is not None
                and self._frontier_triggered(request)):
            log.info("escalating to frontier tier (%s/%s)",
                     llm.frontier_provider, llm.frontier_model or "default")
            return "frontier"
        if source == "voice" and llm.fast_model:
            return "fast"
        return "primary"

    # -- main loop -------------------------------------------------------
    _OPEN_APP = re.compile(
        r"^(?:please\s+)?(?:open|launch|start)\s+(?:the\s+)?(.+?)"
        r"(?:\s+(?:app|application|program))?[.!]?$", re.IGNORECASE)
    _TIME_INTENT = re.compile(
        r"\b(?:what(?:'s| is)\s+the\s+(?:current\s+)?(?:time|date)|"
        r"what\s+time\s+is\s+it|what\s+day\s+is\s+it|"
        r"current\s+(?:time|date)|date\s+today|time\s+now)\b",
        re.IGNORECASE)

    def _time_intent(self, request: str) -> str | None:
        """Return 'time' or 'date' for an unambiguous clock request.

        Small models sometimes answer from stale training context instead of
        calling ``time.now``. A clock question has exactly one authoritative
        source, so route it deterministically and let TTS speak the tool result.
        """
        match = self._TIME_INTENT.search(str(request or ""))
        if not match:
            return None
        phrase = match.group(0).lower()
        return "date" if ("date" in phrase or "day" in phrase) else "time"

    def _app_intent(self, request: str) -> str | None:
        """An unambiguous 'open <installed app>' intent, or None.

        The model is good at planning but a small one reliably mistakes "open
        steam" for a website. When the phrase is exactly an app launch and the
        name is an installed, allowlisted app, routing is not a judgement call.
        The effect still goes through the controller (policy, approval,
        verifier), so this is routing, never a bypass.
        """
        if self.db is None:
            return None
        match = self._OPEN_APP.match(str(request or "").strip())
        if not match:
            return None
        raw = match.group(1).strip().strip("\"'")
        if not raw or "://" in raw or "/" in raw or raw.lower().startswith("www."):
            return None
        try:
            from types import SimpleNamespace
            from ..tools.builtin.apps import catalog
            entries = catalog(SimpleNamespace(db=self.db))
        except Exception:  # pragma: no cover - prompt/routing must not fail a turn
            return None
        for name, entry in entries.items():
            if entry.get("installed") and name.lower() == raw.lower():
                return name
        return None

    async def run_turn(self, request: str, *, session: dict[str, Any],
                       source: str = "text", user: str = "owner") -> AsyncIterator[dict[str, Any]]:
        """Run one turn, then persist the session so a restart keeps the context."""
        try:
            async for ev in self._run_turn(request, session=session, source=source, user=user):
                yield ev
        finally:
            self._save_session(session)

    def _save_session(self, session: dict[str, Any] | None) -> None:
        if self.session_store is None or session is None:
            return
        try:
            self.session_store.save(session)
        except Exception as exc:  # pragma: no cover - persistence must not break a turn
            log.warning("session persist failed: %s", exc)

    async def _anton_context(self, request: str, task: Any,
                             seen_calls: dict[str, int]) -> str:
        """Fetch Anton's real state before the model answers about Anton.

        A small model reliably answers from tool *descriptions* instead of
        calling them ("a node is online") when asked what Anton is doing. This
        grounds the turn in a read-only fetch through the normal controller, so
        policy, the audit log and the attempt journal all still apply.
        """
        if "anton" not in (request or "").lower():
            return ""
        parts: list[str] = []
        for name, args in (("anton.status", {}), ("anton.jobs", {"limit": 5})):
            try:
                result, _ = await self.controller.execute_tool(task, name, args)
            except Exception as exc:  # a read failure must not fail the turn
                log.debug("anton context %s failed: %s", name, exc)
                continue
            if result.ok:
                payload = json.dumps(result.data, ensure_ascii=False, default=str)
                parts.append(f"{name} -> {payload[:1500]}")
                seen_calls[f"{name}|{json.dumps(args, sort_keys=True)}"] = 1
        if not parts:
            return ("Anton was queried and did not return data. Say that plainly; "
                    "do not invent a status.")
        return ("Live Anton reading, fetched just now. Use these exact facts:\n"
                + "\n".join(parts))

    async def _run_turn(self, request: str, *, session: dict[str, Any],
                        source: str = "text", user: str = "owner") -> AsyncIterator[dict[str, Any]]:
        """Yield structured turn events; the caller renders/streams them."""
        task = self.controller.create(request, source=source, user=user,
                                      session_id=session.get("id"))
        seen_calls: dict[str, int] = {}

        # Unambiguous app launch: route directly; do not ask the model to guess
        # between a program and a website.
        app = self._app_intent(request)
        if app:
            self.controller.set_status(task, "planning")
            yield {"kind": "task", "task_id": task.id, "status": "planning"}
            yield {"kind": "ack", "text": self.persona.ack()}
            yield {"kind": "tool_call", "tool": "apps.launch", "args": {"app": app}}
            result, evidence = await self.controller.execute_tool(
                task, "apps.launch", {"app": app})
            yield {"kind": "tool_result", "tool": "apps.launch", "ok": result.ok,
                   "verified": bool(evidence.get("verified")),
                   "summary": result.brief(), "evidence": evidence}
            if result.ok:
                text = f"Opening {app}, {self.persona.addr}."
            else:
                text = f"I could not open {app}, {self.persona.addr}: {result.error}"
            self.controller.set_status(
                task, "succeeded" if result.ok else "failed", goal=request,
                result={"text": text, "steps": 1,
                        "tool_calls": [{"tool": "apps.launch", "ok": result.ok}]},
                error=result.error)
            yield {"kind": "final", "text": text, "task_id": task.id,
                   "status": "succeeded" if result.ok else "failed", "steps": 1}
            return

        clock = self._time_intent(request)
        if clock:
            self.controller.set_status(task, "planning")
            yield {"kind": "task", "task_id": task.id, "status": "planning"}
            yield {"kind": "ack", "text": self.persona.ack()}
            yield {"kind": "tool_call", "tool": "time.now", "args": {}}
            result, evidence = await self.controller.execute_tool(
                task, "time.now", {})
            yield {"kind": "tool_result", "tool": "time.now", "ok": result.ok,
                   "verified": bool(evidence.get("verified")),
                   "summary": result.brief(), "evidence": evidence}
            if result.ok and isinstance(result.data, dict):
                d = result.data
                when = d.get("date") if clock == "date" else d.get("time")
                if clock == "date":
                    text = (f"It is {d.get('weekday')}, {when}, "
                            f"{self.persona.addr}.")
                else:
                    text = (f"It is {when} on {d.get('weekday')}, {d.get('date')}, "
                            f"{self.persona.addr}.")
            else:
                text = f"I could not read the clock, {self.persona.addr}: {result.error}"
            self.controller.set_status(
                task, "succeeded" if result.ok else "failed", goal=request,
                result={"text": text, "steps": 1,
                        "tool_calls": [{"tool": "time.now", "ok": result.ok}]},
                error=None if result.ok else result.error)
            yield {"kind": "final", "text": text, "task_id": task.id,
                   "status": "succeeded" if result.ok else "failed", "steps": 1}
            return

        mode = self.config.policy.mode
        t0 = time.perf_counter()
        recall = await self._recall(request, user)
        t1 = time.perf_counter()
        lessons = await self._recall_lessons(request, user)
        routing = self._routing_hint()
        anton = await self._anton_context(request, task, seen_calls)
        context = "\n\n".join(part for part in (recall, lessons, routing, anton) if part)
        # A 100+ tool surface overwhelms a small model's tool choice; offer the
        # relevant subset (plus a fixed core) rather than everything. Keep the
        # list short so the prompt (and thus per-turn prompt-eval) stays small.
        tool_names = self.registry.shortlist(request, limit=5)
        system = self.persona.system_prompt(context=context, tools=tool_names,
                                            mode=mode, spoken=source == "voice")

        history: list[ChatMessage] = session.setdefault("messages", [])
        system_msg = ChatMessage("system", system)
        if history and getattr(history[0], "role", None) == "system":
            history[0] = system_msg
        else:
            # Never overwrite a user message when a stored history lacks a system
            # prompt (e.g. after trimming).
            history.insert(0, system_msg)
        history.append(ChatMessage("user", request))

        self.controller.set_status(task, "planning")
        yield {"kind": "task", "task_id": task.id, "status": "planning"}
        yield {"kind": "ack", "text": self.persona.ack()}

        turn = AgentTurn(task_id=task.id, session_id=session.get("id", ""),
                         user=user, source=source)

        for step in range(self.max_steps):
            if task.id in self.controller._cancels:
                turn.error = "cancelled"
                break
            turn.steps = step + 1
            self.bus.publish("agent.step", task_id=task.id, step=step + 1)

            tools = self.registry.llm_tools(allow=tool_names)
            text_buf: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            errored = False

            # Spoken turns favour the small, low-latency model when one is
            # configured. Typed turns keep the primary model and its broader
            # reasoning/tool quality. A request matching a configured frontier
            # trigger escalates to the remote engine. The router still applies
            # the same policy, approval and verification path after tool selection.
            want = self._route_tier(request, source)
            async for ev in self.router.chat(
                    history, want=want, tools=tools or None,
                    temperature=self.config.llm.temperature,
                    num_ctx=self.config.llm.num_ctx,
                    num_predict=self.config.llm.num_predict):
                if ev.kind == "token":
                    text_buf.append(ev.text)
                    yield {"kind": "token", "text": ev.text}
                elif ev.kind == "tool_calls":
                    tool_calls.extend(ev.tool_calls)
                elif ev.kind == "meta":
                    turn.tool_calls.append({"meta": ev.meta})
                    log.info("model meta: prompt_eval=%s eval=%s dur=%.2fs",
                             ev.meta.get("prompt_eval_count"), ev.meta.get("eval_count"),
                             (ev.meta.get("total_duration") or 0) / 1e9)
                elif ev.kind == "error":
                    errored = True
                    turn.error = ev.error
                    yield {"kind": "error", "text": ev.error}
                    break

            assistant_text = "".join(text_buf)
            t2 = time.perf_counter()
            log.info("timing: recall=%.2fs model=%.2fs", t1 - t0, t2 - t1)

            if errored:
                break

            if not tool_calls:
                turn.final_text = self.persona.polish(assistant_text)
                history.append(ChatMessage("assistant", turn.final_text))
                break

            # Model wants tools.
            history.append(ChatMessage("assistant", assistant_text, tool_calls=tool_calls))
            for call in tool_calls:
                fn = (call.get("function") or {})
                name = fn.get("name") or call.get("name") or ""
                raw_args = fn.get("arguments") or call.get("arguments") or {}
                args = _coerce_args(raw_args)
                yield {"kind": "tool_call", "tool": name, "args": args}
                self.bus.publish("agent.tool_call", task_id=task.id, tool=name, args=args)

                # A looping model can burn the whole step budget on one call.
                # Refuse an exact repeat instead of acting twice; the model sees
                # why and can answer from the earlier result.
                call_key = f"{name}|{json.dumps(args, sort_keys=True, default=str)}"
                if seen_calls.get(call_key):
                    note = (f"Already called {name} with those exact arguments this turn. "
                            "Use the earlier result; do not repeat the call.")
                    yield {"kind": "tool_result", "tool": name, "ok": False,
                           "verified": False, "summary": note, "evidence": {}}
                    history.append(ChatMessage("tool", content=note, name=name,
                                               tool_call_id=call.get("id")))
                    continue
                seen_calls[call_key] = 1

                result, evidence = await self.controller.execute_tool(
                    task, name, args, step_index=step,
                    ctx_extra={"services": {}})
                turn.tool_calls.append({"tool": name, "args": args, "ok": result.ok,
                                        "verified": evidence.get("verified"),
                                        "error": (result.error if not result.ok else None)})
                yield {"kind": "tool_result", "tool": name, "ok": result.ok,
                       "verified": bool(evidence.get("verified")),
                       "summary": result.brief(), "evidence": evidence}
                history.append(ChatMessage(
                    "tool", content=result.brief(1200) if result.ok
                    else (result.error or "tool failed"),
                    name=name, tool_call_id=call.get("id")))
        else:
            turn.final_text = self.persona.polish(
                "I've reached my step limit on that, " + self.persona.addr +
                ". I'll stop here rather than continue acting blind.")

        if not turn.final_text and not turn.error:
            turn.final_text = "(no response)"

        status = "succeeded" if not turn.error else "failed"
        self.controller.set_status(task, status, goal=request,
                                  result={"text": turn.final_text,
                                          "steps": turn.steps,
                                          "tool_calls": turn.tool_calls},
                                  error=turn.error)
        yield {"kind": "final", "text": turn.final_text, "task_id": task.id,
               "status": status, "steps": turn.steps}

        # persist a compact episode for later recall
        if (self.memory is not None and turn.final_text and not turn.error
                and source != "voice"):
            try:
                t3 = time.perf_counter()
                await self.memory.remember(
                    f"Owner asked: {request}\nJarvis answered: {turn.final_text[:400]}",
                    kind="episode", user=user, source=task.id, confidence=0.6,
                    private=True)
                log.info("timing: episode-persist=%.2fs", time.perf_counter() - t3)
            except Exception as exc:  # pragma: no cover
                log.debug("episode persist failed: %s", exc)

        # learn from the turn's outcomes: failed tool calls become durable lessons
        if self.learning is not None:
            try:
                await self.learning.record_turn(
                    request=request, tool_calls=turn.tool_calls, user=user,
                    source=source, error=turn.error)
            except Exception as exc:  # pragma: no cover
                log.debug("learning record failed: %s", exc)

    async def complete(self, request: str, *, session: dict[str, Any] | None = None,
                       source: str = "text", user: str = "owner") -> str:
        """Non-streaming convenience: return only the final text."""
        session = session if session is not None else {}
        final = ""
        async for ev in self.run_turn(request, session=session, source=source, user=user):
            if ev["kind"] == "final":
                final = ev["text"]
        return final


def _coerce_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            out = json.loads(raw)
            return out if isinstance(out, dict) else {"value": out}
        except json.JSONDecodeError:
            return {"value": raw}
    return {}
