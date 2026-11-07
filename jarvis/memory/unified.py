"""Unified memory service (V3 consolidation)."""

from __future__ import annotations


class UnifiedMemory:
    def __init__(self, jarvis=None):
        self.jarvis = jarvis
        self.store = getattr(jarvis, "memory", None) if jarvis else None

    def episodic(self):
        return []

    def semantic(self):
        return []

    def procedural(self):
        return []

    def working(self):
        return []

    def project(self):
        return []

    def remember(self, kind: str, text: str, **meta):
        if self.store and hasattr(self.store, "remember"):
            return self.store.remember(text, kind=kind, **meta)
        return {"ok": True}

    def recall(self, query: str, **opts):
        if self.store and hasattr(self.store, "recall"):
            return self.store.recall(query, **opts)
        return []

    def forget(self, memory_id: str):
        return {"ok": True}

    def correct(self, memory_id: str, new_text: str):
        return {"ok": True}

    def list_by_sensitivity(self, label: str):
        return []
