"""Tool contract: the single interface every JARVIS capability implements.

Design rules (from the Jarvis spec pack):
  * A tool declares its side effect class up front so policy can gate it.
  * A tool returns *evidence*, not just a claim of success.
  * Side-effecting tools declare a `verify` read-back so the observer can
    independently confirm the effect instead of trusting the tool's own word.
  * Tools are versioned; malformed input and version mismatch must fail loudly.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

__all__ = [
    "SideEffect", "ToolParam", "ToolSpec", "ToolResult", "Tool", "ToolContext",
]


class SideEffect(str, Enum):
    NONE = "none"                 # read-only, safe to auto-run
    NETWORK = "network"           # reads remote data (search/fetch)
    LOCAL_WRITE = "local_write"   # writes files/notes/settings
    LOCAL_EXEC = "local_exec"     # runs a process/command
    REMOTE = "remote"             # acts on another machine (node/laptop)
    DESTRUCTIVE = "destructive"   # deletes/overwrites/irreversible


@dataclass(frozen=True)
class ToolParam:
    name: str
    type: str = "string"          # string | integer | number | boolean | array | object
    description: str = ""
    required: bool = False
    default: Any = None
    enum: tuple[str, ...] | None = None

    def to_json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.type, "description": self.description}
        if self.enum:
            schema["enum"] = list(self.enum)
        return schema


@dataclass
class ToolSpec:
    name: str
    summary: str
    version: str = "1.0"
    category: str = "general"
    side_effect: SideEffect = SideEffect.NONE
    params: tuple[ToolParam, ...] = ()
    returns: str = ""
    needs_network: bool = False
    cheap: bool = True            # False => don't call speculatively

    def required_params(self) -> list[str]:
        return [p.name for p in self.params if p.required]

    def parameters_schema(self) -> dict[str, Any]:
        props = {p.name: p.to_json_schema() for p in self.params}
        return {
            "type": "object",
            "properties": props,
            "required": self.required_params(),
        }

    def to_llm_tool(self) -> dict[str, Any]:
        """Ollama / OpenAI tool-calling shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"{self.summary} [side_effect={self.side_effect.value}]",
                "parameters": self.parameters_schema(),
            },
        }


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    # "settled" means the tool both acted and was sure of the effect.
    # "unverified" means the effect may have happened but read-back is pending.
    certainty: str = "settled"    # settled | unverified | unknown
    duration_ms: float = 0.0

    def brief(self, limit: int = 400) -> str:
        """Short human/model-readable rendering."""
        if self.ok:
            text = self.data if isinstance(self.data, str) else _json_brief(self.data)
            return text[:limit]
        return f"failed: {self.error}"[:limit]


def _json_brief(data: Any, limit: int = 400) -> str:
    import json
    try:
        return json.dumps(data, ensure_ascii=False, default=str)[:limit]
    except Exception:  # pragma: no cover
        return str(data)[:limit]


@dataclass
class ToolContext:
    """Everything a tool needs, handed in by the controller.

    Tools must not reach around this object for authority: permissions are
    already resolved by the broker, and secrets are injected only here.
    """

    config: Any
    db: Any
    bus: Any
    tool_name: str = ""
    task_id: str = ""
    attempt_id: str = ""
    user: str = "owner"
    log: Any = None
    secrets: Callable[[str], str | None] | None = None
    emit: Callable[[str, dict[str, Any]], None] | None = None
    services: dict[str, Any] = field(default_factory=dict)

    def service(self, name: str) -> Any:
        return self.services.get(name)


class Tool(Protocol):
    spec: ToolSpec

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult: ...

    def verify(self, ctx: ToolContext, result: ToolResult) -> Awaitable[dict[str, Any] | None] | None: ...


class BaseTool:
    """Convenience base class implementing the protocol with sane defaults."""

    spec: ToolSpec

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:  # pragma: no cover
        raise NotImplementedError

    def verify(self, ctx: ToolContext, result: ToolResult):
        """Default: tools with no side effect are self-evidently verified."""
        if self.spec.side_effect == SideEffect.NONE:
            return {"verified": True, "method": "read-only"}
        return None

    # -- helpers for subclasses -----------------------------------------
    @staticmethod
    def _check_required(spec: ToolSpec, kwargs: dict[str, Any]) -> str | None:
        missing = [p for p in spec.required_params() if not kwargs.get(p)]
        if missing:
            return f"missing required parameter(s): {', '.join(missing)}"
        return None

    @classmethod
    async def timed(cls, coro: Awaitable[Any]) -> tuple[Any, float]:
        t0 = time.perf_counter()
        out = await coro
        return out, (time.perf_counter() - t0) * 1000.0


def is_async(fn: Any) -> bool:
    return inspect.iscoroutinefunction(fn)
