"""Policy broker — extended for V3 consolidation."""

from __future__ import annotations

from . import PolicyBroker as _PolicyBroker

__all__ = ["PolicyBroker", "RiskTier", "PolicyDecision"]


class RiskTier:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyDecision:
    def __init__(self, allowed: bool, reason: str = "", decision_id: str = "", risk: str = RiskTier.LOW):
        self.allowed = allowed
        self.reason = reason
        self.decision_id = decision_id
        self.risk = risk


# Re-export original
PolicyBroker = _PolicyBroker
