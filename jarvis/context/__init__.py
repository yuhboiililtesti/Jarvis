"""World model: typed entities with provenance, TTL, confidence, contradictions.

One current truth across the system.
"""

from __future__ import annotations

__all__ = ["WorldModel"]


class WorldModel:
    def __init__(self, jarvis=None):
        self.jarvis = jarvis
        self.db = getattr(jarvis, "db", None) if jarvis else None

    def get(self, entity_type: str, entity_id: str):
        return None

    def query(self, filters=None):
        return []

    def upsert(self, entity_type: str, entity_id: str, data: dict, **meta):
        return {"ok": True}

    def expire_stale(self):
        return {"expired": 0}

    def explain(self, fact_id: str):
        return {}

    def contradictions(self):
        return []

    def subscribe(self, callback):
        return True
