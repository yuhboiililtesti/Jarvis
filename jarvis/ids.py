"""Stable identifier helpers.

Task/attempt IDs are allocated *before* any external effect so that a crash
between "sent" and "recorded" leaves a durable, reconcilable marker.
"""

from __future__ import annotations

import secrets
import time
import uuid

__all__ = ["new_id", "task_id", "attempt_id", "node_id", "now_iso", "ulid_like"]


def _b32(n: int) -> str:
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"  # crockford-ish, no i/l/o/u
    out = [""] * n
    for i in range(n - 1, -1, -1):
        out[i] = alphabet[secrets.randbelow(32)]
    return "".join(out)


def ulid_like() -> str:
    """Time-ordered, sortable, collision-resistant id (Crockford base32)."""
    ts = int(time.time() * 1000)
    stamp = ""
    for _ in range(10):
        stamp = "0123456789abcdefghjkmnpqrstvwxyz"[ts & 0x1F] + stamp
        ts >>= 5
    return stamp + _b32(16)


def new_id(prefix: str = "") -> str:
    core = ulid_like()
    return f"{prefix}_{core}" if prefix else core


def task_id() -> str:
    return new_id("tsk")


def attempt_id() -> str:
    return new_id("att")


def node_id() -> str:
    return new_id("node")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def uuid4() -> str:
    return str(uuid.uuid4())
