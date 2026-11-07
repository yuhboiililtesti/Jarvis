"""Persistent runtime: scheduling, event reactions and durable working memory.

This is the difference between an assistant that answers and one that keeps
running. The loop, the scheduler and the event engine all route their effects
through the same controller as a spoken request — policy, approval, DEFCON and
the verifier still apply.
"""

from __future__ import annotations

from .events import EventEngine, match_condition
from .scheduler import Scheduler, due_occurrence, next_occurrence, parse_when
from .service import RuntimeService
from .sessions import SessionStore

__all__ = [
    "RuntimeService", "Scheduler", "EventEngine", "SessionStore",
    "match_condition", "parse_when", "due_occurrence", "next_occurrence",
]
