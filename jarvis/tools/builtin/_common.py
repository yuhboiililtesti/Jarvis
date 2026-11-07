"""Shared conventions for the newer builtin tool modules (private; not loaded as tools).

* **Dry run / preview.** Every state-changing tool accepts ``dry_run`` (default
  false). When true the tool validates its arguments, resolves its target with
  read-only calls, and returns exactly what it *would* do — target, operation,
  expected effect, whether it is reversible and how to roll it back — without
  acting. :func:`preview_verify` makes the verifier record such a result as
  "no effect performed", never as a verified effect.
* **Health.** Each module exposes ``health() -> {"available": bool, "reason": str}``
  (dependency present, platform supported). Tools put that reason in their error
  when unavailable, so the failure is actionable.
"""

from __future__ import annotations

from typing import Any

from ..base import ToolParam, ToolResult

__all__ = ["DRY_RUN_PARAM", "dry_run_requested", "preview", "is_preview", "preview_verify",
           "health_ok", "health_fail", "unavailable_result"]

DRY_RUN_PARAM = ToolParam(
    "dry_run", "boolean",
    "Preview only: return the exact target, operation, effect and rollback without acting.",
    default=False,
)


def dry_run_requested(kwargs: dict[str, Any]) -> bool:
    val = kwargs.get("dry_run")
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def preview(*, target: Any, operation: str, effect: str, reversible: bool | str,
            rollback: str, **extra: Any) -> ToolResult:
    """A dry-run result. ``data['dry_run'] is True`` marks that nothing was done."""
    data = {"dry_run": True, "target": target, "operation": operation,
            "expected_effect": effect, "reversible": reversible, "rollback": rollback}
    data.update(extra)
    return ToolResult(ok=True, data=data, evidence={"method": "dry-run", "acted": False},
                      certainty="settled")


def is_preview(result: ToolResult | None) -> bool:
    return bool(result is not None and result.ok and isinstance(result.data, dict)
                and result.data.get("dry_run") is True)


def preview_verify() -> dict[str, Any]:
    return {"verified": False, "method": "dry-run",
            "note": "preview only; no effect was performed, so there is nothing to confirm"}


def health_ok(reason: str = "ok", **extra: Any) -> dict[str, Any]:
    return {"available": True, "reason": reason, **extra}


def health_fail(reason: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "reason": reason, **extra}


def unavailable_result(health: dict[str, Any]) -> ToolResult | None:
    """ToolResult(ok=False) carrying the health reason, or None when available."""
    if health.get("available"):
        return None
    return ToolResult(ok=False, error=f"unavailable: {health.get('reason')}",
                      evidence={"health": health}, certainty="settled")
