"""Evaluation harness for behavioral testing."""

from __future__ import annotations


class EvaluationHarness:
    def __init__(self, jarvis=None):
        self.jarvis = jarvis

    def run_suite(self, suite: str = "heldout"):
        return {"passed": 0, "failed": 0, "total": 0, "status": "not_run"}


__all__ = ["EvaluationHarness"]
