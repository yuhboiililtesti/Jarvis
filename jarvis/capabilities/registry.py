"""Universal Capability Registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Capability:
    name: str
    category: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    enabled: bool = True


class CapabilityRegistry:
    def __init__(self, jarvis=None):
        self.jarvis = jarvis
        self._caps: dict[str, Capability] = {}
        self._register_defaults()

    def _register_defaults(self):
        self.register(Capability(
            name="software_engineering",
            category="engineering",
            description="Coding, debugging, building, testing",
            tools=["files.*", "shell.run", "git.*"]
        ))
        self.register(Capability(
            name="machine_learning",
            category="ai",
            description="Neural networks, training, evaluation",
            tools=["compute.*"]
        ))
        self.register(Capability(
            name="game_engineering",
            category="graphics",
            description="Game engines, modding, rendering, CHIMERA",
            tools=["files.*"]
        ))
        self.register(Capability(
            name="system_admin",
            category="infra",
            description="System administration, services, debugging",
            tools=["system.*", "host.*"]
        ))
        self.register(Capability(
            name="research",
            category="knowledge",
            description="Research, documentation, analysis",
            tools=["web.*", "knowledge.*", "memory.*"]
        ))

    def register(self, cap: Capability):
        self._caps[cap.name] = cap

    def get(self, name: str):
        return self._caps.get(name)

    def list_all(self):
        return list(self._caps.values())

    def find_for_request(self, request: str):
        req = request.lower()
        matches = []
        if any(w in req for w in ["code", "build", "debug", "fix", "compile", "function"]):
            matches.append("software_engineering")
        if any(w in req for w in ["train", "model", "neural", "ml", "learn", "dataset"]):
            matches.append("machine_learning")
        if any(w in req for w in ["game", "minecraft", "gta", "mod", "render", "engine"]):
            matches.append("game_engineering")
        if any(w in req for w in ["server", "crash", "log", "service", "restart"]):
            matches.append("system_admin")
        if any(w in req for w in ["research", "find", "learn", "how", "explain"]):
            matches.append("research")
        return matches


__all__ = ["CapabilityRegistry", "Capability"]
