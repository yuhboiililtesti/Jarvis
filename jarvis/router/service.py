"""Local-first model router."""

from __future__ import annotations


class ModelRouterService:
    def __init__(self, jarvis=None):
        self.jarvis = jarvis
        self.router = getattr(jarvis, "router", None) if jarvis else None

    def route(self, task_type: str = "chat", **opts):
        return {"model": "local", "reason": "local-first", "endpoint": "ollama"}

    def health(self):
        if self.router and hasattr(self.router, "health"):
            return self.router.health()
        return {"ok": True}
