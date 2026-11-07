"""Memory service facade."""

from __future__ import annotations

from .unified import UnifiedMemory


class MemoryService(UnifiedMemory):
    def __init__(self, jarvis=None):
        super().__init__(jarvis)


__all__ = ["MemoryService", "UnifiedMemory"]
