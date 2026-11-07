"""World model service."""

from __future__ import annotations

from . import WorldModel


class ContextService(WorldModel):
    def __init__(self, jarvis=None):
        super().__init__(jarvis)
        self.jarvis = jarvis


__all__ = ["ContextService", "WorldModel"]
