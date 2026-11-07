"""Capability-based permissions for nodes."""

from __future__ import annotations

from enum import Enum


class NodeCapability(str, Enum):
    EXEC = "exec"
    FILES_READ = "files.read"
    FILES_WRITE = "files.write"
    SYSINFO = "sysinfo"
    METRICS = "metrics"
    TOOL = "tool"
    POWER = "power"
    VOICE_SPEAK = "voice.speak"
    VOICE_PLAY = "voice.play"
    PING = "ping"


# Default safe capabilities
DEFAULT_CAPABILITIES = {
    NodeCapability.SYSINFO,
    NodeCapability.METRICS,
    NodeCapability.PING,
    NodeCapability.FILES_READ,
}


# Dangerous capabilities that need explicit grant
DANGEROUS_CAPABILITIES = {
    NodeCapability.EXEC,
    NodeCapability.POWER,
    NodeCapability.FILES_WRITE,
}


class CapabilityManager:
    def __init__(self, node_id: str = None):
        self.node_id = node_id
        self._caps = set(DEFAULT_CAPABILITIES)

    def grant(self, cap: NodeCapability):
        self._caps.add(cap)

    def revoke(self, cap: NodeCapability):
        self._caps.discard(cap)

    def has(self, cap: NodeCapability) -> bool:
        return cap in self._caps

    def check(self, cap: NodeCapability) -> bool:
        if cap in DANGEROUS_CAPABILITIES and cap not in self._caps:
            return False
        return cap in self._caps


__all__ = ["NodeCapability", "CapabilityManager", "DEFAULT_CAPABILITIES", "DANGEROUS_CAPABILITIES"]
