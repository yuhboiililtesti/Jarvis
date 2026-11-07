"""Natural Language Task Engine."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskSpec:
    objective: str
    goal: str = ""
    requirements: list[str] = field(default_factory=list)
    capabilities_needed: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    resources: dict[str, Any] = field(default_factory=dict)
    verification_criteria: list[str] = field(default_factory=list)
    safety_notes: list[str] = field(default_factory=list)
    needs_approval: bool = False
    priority: int = 5


class TaskEngine:
    def __init__(self, jarvis=None):
        self.jarvis = jarvis
        self.router = getattr(jarvis, "router", None) if jarvis else None

    async def parse(self, request: str, context: dict[str, Any] | None = None) -> TaskSpec:
        """Parse natural language into structured task spec."""
        # Try to use LLM for structured parsing
        if self.router:
            try:
                prompt = f"""Convert this request into a structured task specification.
Request: {request}

Return JSON with: objective, goal, requirements (list), capabilities_needed (list), 
steps (list of {{action, description}}), tools (list), verification_criteria (list),
needs_approval (bool)."""
                # For now return basic parsing
            except Exception:
                pass
        
        spec = TaskSpec(objective=request)
        # Basic intent detection
        req_lower = request.lower()
        if any(w in req_lower for w in ["minecraft", "gta", "game", "mod", "render"]):
            spec.capabilities_needed.append("game_engineering")
        if any(w in req_lower for w in ["train", "model", "neural", "learn", "ml"]):
            spec.capabilities_needed.append("machine_learning")
        if any(w in req_lower for w in ["build", "code", "debug", "fix", "compile"]):
            spec.capabilities_needed.append("software_engineering")
        if any(w in req_lower for w in ["server", "crash", "logs", "amsd"]):
            spec.capabilities_needed.append("system_admin")
        return spec

    def select_capabilities(self, spec: TaskSpec):
        """Auto-select capabilities based on task spec."""
        caps = []
        for c in spec.capabilities_needed:
            caps.append(c)
        return caps

    def plan(self, spec: TaskSpec):
        """Create execution plan."""
        return spec.steps


__all__ = ["TaskEngine", "TaskSpec"]
