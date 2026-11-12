"""Standardized tool health probes (P2).

Resolution order for every registered tool:

1. the tool module's own ``health()`` (the precise answer);
2. a **category probe** — dependency packages, required binaries, platform
   support and required configuration;
3. otherwise **unverified**: the tool is not proven unhealthy, it simply has
   no check that can be run here. The inventory keeps that distinction.

A probe never claims availability it has not observed.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from typing import Any

__all__ = ["CATEGORY_PROBES", "probe_category", "probe_tool", "summarize"]

# category -> probe("packages" | "binary" | "platform" | "config", values)
CATEGORY_PROBES: dict[str, tuple[str, tuple[str, ...]]] = {
    "documents": ("packages", ("docx", "openpyxl", "pypdf")),
    "vision": ("packages", ("PIL",)),
    "media": ("binary", ("ffmpeg",)),
    "compute": ("packages", ("numpy",)),
    "math": ("packages", ("numpy",)),
    "input": ("platform", ("windows",)),
    "host": ("platform", ("windows",)),
    "desktop": ("platform", ("windows",)),
    "notifications": ("platform", ("windows",)),
    "voice": ("packages", ("sounddevice",)),
}

_PLATFORM = {"windows": "nt", "linux": "posix"}


def _has_package(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def probe_category(category: str) -> dict[str, Any] | None:
    rule = CATEGORY_PROBES.get(category)
    if rule is None:
        return None
    kind, values = rule
    if kind == "packages":
        missing = [v for v in values if not _has_package(v)]
        return {"available": not missing,
                "reason": "ok" if not missing
                else f"missing packages: {', '.join(missing)}",
                "method": "category-packages", "category": category}
    if kind == "binary":
        missing = [v for v in values if shutil.which(v) is None]
        return {"available": not missing,
                "reason": "ok" if not missing
                else f"missing executables: {', '.join(missing)}",
                "method": "category-binary", "category": category}
    if kind == "platform":
        wanted = {_PLATFORM.get(p, p) for p in values}
        ok = sys.platform.startswith("win") if "nt" in wanted else False
        return {"available": ok,
                "reason": "ok" if ok
                else f"requires platform {', '.join(sorted(values))}",
                "method": "category-platform", "category": category}
    return None


def probe_tool(spec: Any, module: Any) -> dict[str, Any]:
    """Return {status, available, reason, method} for one tool."""
    health_fn = getattr(module, "health", None) if module is not None else None
    if callable(health_fn):
        try:
            result = dict(health_fn())
            return {"status": "live" if result.get("available")
                    else "unavailable",
                    "available": bool(result.get("available")),
                    "reason": result.get("reason", ""),
                    "method": "module-health"}
        except Exception as exc:  # pragma: no cover — a probe must not crash
            return {"status": "unavailable", "available": False,
                    "reason": f"health() failed: {exc}",
                    "method": "module-health"}
    category = getattr(spec, "category", "")
    probed = probe_category(category)
    if probed is not None:
        probed["status"] = "live" if probed["available"] else "unavailable"
        return probed
    return {"status": "unverified", "available": None,
            "reason": "no probe defined for this tool/category",
            "method": "none"}


def summarize(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"live": 0, "unavailable": 0, "unverified": 0}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts
