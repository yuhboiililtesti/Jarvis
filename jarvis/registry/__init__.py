"""Machine capability registry."""

from __future__ import annotations


class CapabilityRegistry:
    def __init__(self, jarvis=None):
        self.jarvis = jarvis

    def discover(self):
        return []

    def register(self, node_id: str, caps: dict):
        return {"ok": True}

    def get(self, node_id: str):
        return {}

    def route_by_capability(self, need: str):
        return []


__all__ = ["CapabilityRegistry"]
