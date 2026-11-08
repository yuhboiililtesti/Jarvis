"""Kernel: builds and owns every subsystem, wired in dependency order.

`Jarvis` is the single object the API/UI and CLI talk to. Subsystems are plain
attributes so they can be replaced in tests.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

from ..config import Config, load_config
from ..db import Database
from ..events import EventBus
from ..logging_setup import get_logger, setup_logging
from ..learning import LearningService
from ..llm import ChatMessage, ModelRouter
from ..memory import MemoryStore
from ..persona import Persona
from ..policy import PolicyBroker, SecretStore
from ..tasks import TaskController, Verifier
from ..tools import REGISTRY, load_builtin_tools
from .agent import Agent

log = get_logger("jarvis.kernel")

__all__ = ["Jarvis", "build"]


@dataclass
class Jarvis:
    config: Config
    db: Database
    bus: EventBus
    policy: PolicyBroker
    secrets: SecretStore
    registry: Any
    verifier: Verifier
    controller: TaskController
    memory: MemoryStore
    learning: Any
    router: ModelRouter
    persona: Persona
    agent: Agent
    services: dict[str, Any]
    sessions: dict[str, dict[str, Any]]

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self.bus.status("core", "starting")
        recon = self.controller.reconcile()
        if recon["reconciled"]:
            log.warning("reconciled %d interrupted attempt(s)", recon["reconciled"])

        # Tamper-evident audit: verify the hash chain against the external
        # anchor and advance it only when the chain is intact.
        if self.config.security.audit_enabled:
            try:
                from ..security.audit_chain import verify_and_anchor
                audit = verify_and_anchor(self.db, self.config.data_path)
                self.services["audit_chain"] = audit
                if not audit.get("ok"):
                    log.error("AUDIT CHAIN BROKEN: %s (at row %s)",
                              audit.get("reason"), audit.get("broken_at"))
                    self.bus.publish("audit.broken", reason=audit.get("reason"),
                                     broken_at=audit.get("broken_at"))
            except Exception as exc:  # pragma: no cover - must never block startup
                log.warning("audit chain check could not run: %s", exc)

        # start optional background services if present
        for name in ("monitoring", "security", "discovery", "night", "nodes", "voice", "compute",
                     "knowledge", "runtime", "ops", "guardian"):
            svc = self.services.get(name)
            starter = getattr(svc, "start", None) if svc else None
            if starter:
                try:
                    res = starter()
                    if hasattr(res, "__await__"):
                        await res
                    self.bus.status(name, "started")
                except Exception as exc:
                    log.error("service %s failed to start: %s", name, exc)
                    self.bus.degrade(name, f"start failed: {exc}")
        self.bus.status("core", "ready")
        log.info("JARVIS core ready")

        # Warm the LLM (load the model + prime the KV cache) in the background so
        # the first turn isn't hit with a cold model load.
        async def _warm_llm() -> None:
            try:
                got = False
                async for ev in self.router.chat(
                        [ChatMessage("user", "ping")], want="primary",
                        temperature=0.0, num_predict=2):
                    if ev.kind == "token":
                        got = True
                log.info("llm warmed (model=%s, loaded=%s)",
                         self.config.llm.model, got)
            except Exception as exc:  # pragma: no cover - must never block startup
                log.debug("llm warm failed: %s", exc)
        self._warm_llm_task = asyncio.create_task(_warm_llm())

    async def stop(self) -> None:
        warm = getattr(self, "_warm_llm_task", None)
        if warm is not None and not warm.done():
            warm.cancel()
        for name in ("ops", "goals", "runtime", "knowledge", "voice", "nodes", "night", "discovery",
                     "security", "monitoring", "guardian"):
            svc = self.services.get(name)
            stopper = getattr(svc, "stop", None) if svc else None
            if stopper:
                try:
                    res = stopper()
                    if hasattr(res, "__await__"):
                        await res
                except (asyncio.CancelledError, Exception) as exc:  # pragma: no cover
                    log.warning("service %s stop error: %s", name, exc)
        self.bus.status("core", "stopped")
        self.db.close()

    # -- convenience -----------------------------------------------------
    def session(self, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or "default"
        if sid not in self.sessions:
            # Bound the in-process cache; the store is authoritative.
            if len(self.sessions) >= 200:
                with contextlib.suppress(StopIteration):
                    self.sessions.pop(next(iter(self.sessions)))
            store = self.services.get("sessions")
            self.sessions[sid] = (store.get(sid) if store is not None
                                  else {"id": sid, "messages": []})
        return self.sessions[sid]

    async def health(self) -> dict[str, Any]:
        llm = await self.router.health()
        sandbox = self.services.get("sandbox")
        runtime = self.services.get("runtime")
        goals = self.services.get("goals")
        ops = self.services.get("ops")
        return {
            "core": "ready",
            "llm": llm,
            "mode": self.config.policy.mode,
            "defcon": self.policy.defcon,
            "tools": len(self.registry.names()),
            "node_role": self.config.host_role,
            "sandbox": sandbox.status() if sandbox is not None else
                       {"active_tier": "none", "reason": "sandbox service not loaded"},
            "runtime": runtime.health() if runtime is not None else
                       {"available": False, "reason": "runtime service not loaded"},
            "goals": goals.health() if goals is not None else
                     {"available": False, "reason": "goals service not loaded"},
            "ops": ops.health() if ops is not None else
                   {"available": False, "reason": "ops service not loaded"},
        }


def build(config: Config | None = None) -> Jarvis:
    cfg = config or load_config()
    cfg.ensure_dirs()
    setup_logging(cfg.log_level, cfg.data_path / "logs")

    db = Database(cfg.db_path)
    bus = EventBus()
    policy = PolicyBroker(cfg, db, bus)
    secrets = SecretStore(cfg.data_path / "secrets.json")
    # Scrub known secret values out of audit target/detail at the write boundary
    # (F11). Key-based redaction is already applied by Database.audit().
    db.set_redactor(secrets.scrub)

    load_builtin_tools()
    # Owner-approved plugins load after builtins so they can never shadow one.
    from ..plugins import PluginManager
    plugins = PluginManager(cfg.plugins_path, db, REGISTRY)
    plugins.load_enabled()

    router = ModelRouter(cfg, secrets=secrets)
    verifier = Verifier(REGISTRY, bus)
    controller = TaskController(db, bus, REGISTRY, policy, verifier, cfg)
    memory = MemoryStore(db, router)
    learning = LearningService(memory)
    persona = Persona(cfg.persona)
    # Durable working memory: the conversation survives a restart.
    from ..runtime.sessions import SessionStore
    sessions = SessionStore(db, cfg.runtime.session_max_messages)
    services: dict[str, Any] = {"secrets": secrets, "memory": memory, "plugins": plugins,
                                "sessions": sessions, "learning": learning}
    controller.services = services
    agent = Agent(config=cfg, bus=bus, router=router, registry=REGISTRY,
                  controller=controller, persona=persona, memory=memory,
                  session_store=sessions, db=db, learning=learning)

    j = Jarvis(config=cfg, db=db, bus=bus, policy=policy, secrets=secrets,
               registry=REGISTRY, verifier=verifier, controller=controller,
               memory=memory, learning=learning, router=router, persona=persona,
               agent=agent, services=services, sessions={})

    # Optional subsystems are imported lazily so the core runs even if a
    # module is missing (keeps the Core robust and diagnosable).
    optional = _try_build_optional(j)
    services.update(optional)
    return j


def _try_build_optional(j: Jarvis) -> dict[str, Any]:
    out: dict[str, Any] = {}
    builders = {
        "sandbox": ("jarvis.sandbox", "SandboxService"),
        "goals": ("jarvis.goals", "GoalService"),
        "runtime": ("jarvis.runtime", "RuntimeService"),
        "ops": ("jarvis.ops", "OpsService"),
        "monitoring": ("jarvis.monitoring", "MonitoringService"),
        "discovery": ("jarvis.discovery", "DiscoveryService"),
        "night": ("jarvis.monitoring.nightly", "NightRunner"),
        "nodes": ("jarvis.nodes", "NodeService"),
        "security": ("jarvis.security", "SecurityService"),
        "voice": ("jarvis.voice", "VoiceService"),
        "compute": ("jarvis.compute", "ComputeService"),
        "knowledge": ("jarvis.knowledge", "KnowledgeService"),
        "context": ("jarvis.context.service", "ContextService"),
        "world": ("jarvis.context.service", "ContextService"),
        "memory_service": ("jarvis.memory", "MemoryService"),
        "router_service": ("jarvis.router", "ModelRouterService"),
        "capabilities": ("jarvis.registry", "CapabilityRegistry"),
        "eval": ("jarvis.evaluations", "EvaluationHarness"),
        "planner": ("jarvis.planner", "TaskEngine"),
        "capability_registry": ("jarvis.capabilities", "CapabilityRegistry"),
        "ml_lab": ("jarvis.ml_lab", "MLLab"),
        "chimera": ("jarvis.chimera", "ChimeraLab"),
        "compute_scheduler": ("jarvis.scheduler", "ComputeScheduler"),
        "guardian": ("jarvis.guardian", "GuardianService"),
        "backup": ("jarvis.backup", "BackupController"),
        "evolve": ("jarvis.evolve", "EvolveService"),
    }
    for key, (module, cls) in builders.items():
        try:
            mod = __import__(module, fromlist=[cls])
            out[key] = getattr(mod, cls)(j)
        except ModuleNotFoundError as exc:
            log.warning("optional subsystem %s unavailable: %s", key, exc)
        except Exception as exc:  # pragma: no cover
            log.error("optional subsystem %s failed to build: %s", key, exc)
    return out
