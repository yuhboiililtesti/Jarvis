"""Tool registry: registration, lookup, validation and LLM exposure.

Tools register themselves via the `@register` decorator. The registry is the
only place that knows the full capability surface, and it can filter the set
exposed to a model by permission and side-effect class.
"""

from __future__ import annotations

import re
import sys
from typing import Any, Iterable

from .base import SideEffect, Tool, ToolContext, ToolResult, ToolSpec
from ..logging_setup import get_logger

log = get_logger("jarvis.tools")

__all__ = ["ToolRegistry", "register", "REGISTRY", "CORE_SHORTLIST"]

# Always offered to the model: the safe, everyday tools. A large registry (100+
# tools) overwhelms a small model's tool choice, so the rest are selected by
# relevance to the request.
CORE_SHORTLIST: tuple[str, ...] = (
    "time.now", "system.info", "system.metrics", "system.processes",
    "files.read", "files.list", "files.search", "files.hash",
    "memory.search", "memory.remember", "notes.list", "notes.read",
    "web.search", "web.fetch", "calc.eval", "apps.list", "apps.launch",
    "sandbox.status", "system.diagnose", "vision.screen", "knowledge.search",
    "goals.list", "goals.create",
    "host.apps.list", "host.apps.launch", "host.windows.list",
    "host.windows.focus", "host.screen.capture", "host.system.info",
    "host.files.read", "host.voice.speak", "host.exec",
)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._aliases: dict[str, str] = {}
        # Where each tool came from: "builtin" or "plugin:<id>". Set by the
        # plugin loader around its imports; everything else is builtin.
        self._sources: dict[str, str] = {}
        self.loading_source = "builtin"

    def add(self, tool: Tool, aliases: Iterable[str] = ()) -> None:
        name = tool.spec.name
        if name in self._tools:
            log.warning("tool %s re-registered; replacing", name)
        self._tools[name] = tool
        self._sources[name] = self.loading_source
        for a in aliases:
            self._aliases[a] = name

    def remove(self, name: str) -> bool:
        """Unregister a tool and its aliases (used to back out a refused plugin)."""
        if self._tools.pop(name, None) is None:
            return False
        self._sources.pop(name, None)
        for alias in [a for a, target in self._aliases.items() if target == name]:
            del self._aliases[alias]
        return True

    def source_of(self, name: str) -> str:
        return self._sources.get(self._aliases.get(name, name), "builtin")

    def get(self, name: str) -> Tool | None:
        return self._tools.get(self._aliases.get(name, name))

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in self.names()]

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self.all()]

    def by_category(self) -> dict[str, list[ToolSpec]]:
        out: dict[str, list[ToolSpec]] = {}
        for t in self.all():
            out.setdefault(t.spec.category, []).append(t.spec)
        return out

    def llm_tools(self, allow: Iterable[str] | None = None,
                  max_side_effect: SideEffect | None = None) -> list[dict[str, Any]]:
        order = [SideEffect.NONE, SideEffect.NETWORK, SideEffect.LOCAL_WRITE,
                 SideEffect.LOCAL_EXEC, SideEffect.REMOTE, SideEffect.DESTRUCTIVE]
        allow_set = set(allow) if allow is not None else None
        out = []
        for t in self.all():
            if allow_set is not None and t.spec.name not in allow_set:
                continue
            if max_side_effect is not None and t.spec.side_effect in order:
                if order.index(t.spec.side_effect) > order.index(max_side_effect):
                    continue
            out.append(t.spec.to_llm_tool())
        return out

    def shortlist(self, query: str, *, limit: int = 24) -> list[str]:
        """Names worth showing the model for this request.

        A 100+ tool surface drowns a small model's tool choice, so the request is
        scored against each tool's name, summary, category and parameter names;
        the best matches are offered along with a fixed core set. Deterministic
        and cheap (no model call).
        """
        words = {w for w in re.findall(r"[a-z0-9_]+", (query or "").lower())
                 if len(w) > 1}
        scored: list[tuple[int, str]] = []
        for spec in self.specs():
            haystack = " ".join([
                spec.name, spec.summary, spec.category,
                " ".join(p.name for p in spec.params),
            ]).lower()
            score = sum(1 for w in words if w in haystack)
            score += 2 * sum(1 for w in words if w in spec.name.lower().split("."))
            scored.append((score, spec.name))
        ranked = [name for score, name in
                  sorted(scored, key=lambda item: (-item[0], item[1])) if score > 0]
        chosen = ranked[:limit]
        for name in CORE_SHORTLIST:
            if len(chosen) >= limit:
                break
            if name in self._tools and name not in chosen:
                chosen.append(name)
        if not chosen:
            chosen = self.names()[:limit]
        return chosen

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.spec.name,
                "summary": t.spec.summary,
                "version": t.spec.version,
                "category": t.spec.category,
                "side_effect": t.spec.side_effect.value,
                "source": self.source_of(t.spec.name),
                "dry_run": supports_dry_run(t.spec),
                "params": [
                    {"name": p.name, "type": p.type, "required": p.required,
                     "description": p.description}
                    for p in t.spec.params
                ],
            }
            for t in self.all()
        ]

    def health(self) -> dict[str, dict[str, Any]]:
        """``{tool: {available, reason, source}}`` — cheap; never performs an effect.

        A tool may define ``health()`` itself; otherwise its module's ``health()``
        (the convention in ``builtin/_common.py``) is used. A tool with neither
        is reported available with an explicit "no health check" reason, so the
        caller can tell "checked and fine" from "never checked".
        """
        out: dict[str, dict[str, Any]] = {}
        for t in self.all():
            name = t.spec.name
            probe = getattr(t, "health", None)
            if not callable(probe):
                probe = getattr(sys.modules.get(type(t).__module__), "health", None)
            if callable(probe):
                try:
                    res = probe()
                    entry = dict(res) if isinstance(res, dict) else {
                        "available": False, "reason": f"health() returned {type(res).__name__}"}
                except Exception as exc:  # a broken probe is itself a finding
                    entry = {"available": False, "reason": f"health check raised: {exc}"}
            else:
                entry = {"available": True, "reason": "no health check declared"}
            entry["available"] = bool(entry.get("available"))
            entry["source"] = self.source_of(name)
            out[name] = entry
        return out

    async def call(self, tool_name: str, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        tool = self.get(tool_name)
        if tool is None:
            return ToolResult(ok=False, error=f"unknown tool '{tool_name}'")
        err = self._validate(tool.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        return await tool.run(ctx, **kwargs)

    @staticmethod
    def _validate(spec: ToolSpec, kwargs: dict[str, Any]) -> str | None:
        for p in spec.params:
            if p.required and kwargs.get(p.name) in (None, ""):
                return f"missing required parameter '{p.name}'"
            if p.name in kwargs and kwargs[p.name] is not None and p.enum:
                if str(kwargs[p.name]) not in p.enum:
                    return (f"parameter '{p.name}' must be one of "
                            f"{', '.join(p.enum)}")
        return None


def supports_dry_run(spec: ToolSpec) -> bool:
    """A tool supports previews when it declares the shared ``dry_run`` parameter."""
    return any(p.name == "dry_run" for p in spec.params)


REGISTRY = ToolRegistry()


def register(aliases: Iterable[str] = ()):
    """Class decorator: instantiate and register a BaseTool subclass."""
    def deco(cls):
        REGISTRY.add(cls(), aliases=aliases)
        return cls
    return deco
