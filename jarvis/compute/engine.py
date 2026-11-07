"""Compute engine.

Bridges JARVIS to the owner's existing algorithm toolkit (`algokit`) so the
assistant can *calculate and verify* rather than guess. The toolkit is looked up
from a list of known locations and imported lazily; if it is absent, the service
degrades honestly and every compute tool returns `ok=False` with the reason.

Nothing here executes untrusted code: only functions already present in
algokit's public registry are callable, by name, and their results are returned
verbatim (serialised) so the caller can show evidence.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("jarvis.compute")

__all__ = ["ComputeService"]

# Candidate locations for the owner's toolkit, most specific first. Set
# JARVIS_ALGOKIT_PATH to point at a custom install (nothing real is committed).
_TOOLKIT_CANDIDATES = [
    Path(p) for p in (
        os.environ.get("JARVIS_ALGOKIT_PATH"),
        str(Path.home() / "Projects" / "Algorithms" / "algorithms-toolkit"),
        "/opt/algorithms-toolkit",
    ) if p
]

# Capabilities JARVIS exposes to the model by default (name -> why it matters).
CURATED: dict[str, str] = {
    # general math / verification
    "calculator": "evaluate an expression",
    "solve_equation": "solve an algebraic equation",
    "simplify": "simplify a symbolic expression",
    "derivative": "differentiate",
    "integral": "integrate",
    "statistics": "summary statistics of a dataset",
    "mean": "arithmetic mean",
    "median": "median",
    "stddev": "standard deviation",
    # discrete / scheduling / routing
    "dijkstra": "shortest path",
    "a_star": "heuristic shortest path",
    "topological_sort": "dependency ordering",
    "knapsack": "bounded resource selection",
    "job_scheduling": "schedule tasks",
    "bin_packing": "pack items into bins",
    # text / data
    "levenshtein": "edit distance",
    "longest_common_subsequence": "sequence similarity",
    "base64_encode": "encode to base64",
    "sha256": "hash",
    # time / units
    "add_days": "date arithmetic",
    "unit_convert": "unit conversion",
}


@dataclass
class ComputeService:
    """Callable algorithm surface built on algokit (or honestly absent)."""

    def __init__(self, jarvis: Any = None) -> None:
        self.jarvis = jarvis
        self._registry: dict[str, dict[str, Any]] | None = None
        self._unavailable_reason = ""

    # -- discovery -------------------------------------------------------
    def _ensure_toolkit(self) -> bool:
        if self._registry is not None:
            return True
        for cand in _TOOLKIT_CANDIDATES:
            if cand.is_dir() and (cand / "algokit").is_dir():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                break
        try:
            registry_mod = importlib.import_module("algokit.registry")
        except Exception as exc:
            self._unavailable_reason = (
                f"algokit not importable ({type(exc).__name__}: {exc}); "
                f"looked in: {', '.join(str(c) for c in _TOOLKIT_CANDIDATES)}")
            self._registry = {}
            log.warning(self._unavailable_reason)
            return False
        raw = getattr(registry_mod, "REGISTRY", None)
        if not isinstance(raw, dict):
            self._unavailable_reason = "algokit.registry.REGISTRY missing or not a mapping"
            self._registry = {}
            return False
        self._registry = raw
        log.info("compute engine attached to algokit (%d callables)", len(raw))
        return True

    @property
    def available(self) -> bool:
        self._ensure_toolkit()
        return bool(self._registry)

    def reason(self) -> str:
        if self.available:
            return "algokit available"
        return self._unavailable_reason or "algokit unavailable"

    # -- surface ---------------------------------------------------------
    def list_capabilities(self, *, curated_only: bool = False) -> list[dict[str, Any]]:
        if not self.available:
            return []
        assert self._registry is not None
        out: list[dict[str, Any]] = []
        for name, meta in sorted(self._registry.items()):
            if curated_only and name not in CURATED:
                continue
            if not isinstance(meta, dict):
                continue
            out.append({
                "name": name,
                "category": meta.get("category", "general"),
                "description": meta.get("description") or (meta.get("docstring") or "").split("\n")[0],
                "signature": meta.get("signature", ""),
                "curated": name in CURATED,
            })
        return out

    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cap in self.list_capabilities():
            counts[cap["category"]] = counts.get(cap["category"], 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    # -- execution -------------------------------------------------------
    async def solve(self, capability: str, **kwargs: Any) -> dict[str, Any]:
        if not self.available:
            return {"ok": False, "reason": self.reason()}
        assert self._registry is not None
        meta = self._registry.get(capability)
        if meta is None:
            near = [n for n in self._registry if capability.lower() in n.lower()][:8]
            return {"ok": False, "reason": f"unknown capability '{capability}'",
                    "did_you_mean": near}
        fn = meta.get("callable") if isinstance(meta, dict) else None
        if fn is None or not callable(fn):
            return {"ok": False, "reason": f"capability '{capability}' has no callable"}
        ctx_mem = ""
        try:
            result = await asyncio.to_thread(_invoke, fn, kwargs)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}",
                    "capability": capability, "args": _safe(kwargs)}
        return {"ok": True, "capability": capability, "result": _safe(result),
                "signature": meta.get("signature", "") if isinstance(meta, dict) else "",
                "evidence": {"engine": "algokit", "category": meta.get("category") if isinstance(meta, dict) else None}}

    async def ask(self, question: str) -> dict[str, Any]:
        """Use algokit's own plain-language router when available."""
        if not self.available:
            return {"ok": False, "reason": self.reason()}
        try:
            mod = importlib.import_module("algokit.ask")
            fn = getattr(mod, "ask", None)
            if not callable(fn):
                return {"ok": False, "reason": "algokit.ask.ask not available"}
            out = await asyncio.to_thread(fn, question)
            return {"ok": True, "result": _safe(out)}
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

    # -- policy-facing status -------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason(),
            "callables": len(self._registry) if self._registry else 0,
            "curated": sorted(CURATED),
            "search_path": [p for p in _TOOLKIT_CANDIDATES if p.is_dir()],
        }


def _invoke(fn: Any, kwargs: dict[str, Any]) -> Any:
    """Call fn with keyword args the toolkit accepts; report real mismatches."""
    sig = inspect.signature(fn)
    accepted = {
        name for name, p in sig.parameters.items()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }
    unknown = [k for k in kwargs if k not in accepted]
    if unknown and not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        raise TypeError(
            f"unexpected argument(s) {', '.join(unknown)}; accepted: "
            f"{', '.join(sorted(accepted)) or '(none)'}")
    return fn(**kwargs)


def _safe(value: Any) -> Any:
    """Make a result JSON-serialisable without hiding what it was."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, (list, tuple)):
            return [_safe(v) for v in value]
        if isinstance(value, dict):
            return {str(k): _safe(v) for k, v in value.items()}
        return repr(value)
