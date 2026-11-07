"""Contract every registered tool must satisfy — builtins and anything added later.

These checks are structural on purpose: they run with no model, no network and
no side effects, so a new tool that breaks the contract fails here before it
can confuse the model or slip past policy.
"""

from __future__ import annotations

import inspect
import json

import pytest

from jarvis.tools import REGISTRY, SideEffect, load_builtin_tools
from jarvis.tools.registry import supports_dry_run

load_builtin_tools()
TOOLS = REGISTRY.all()
MUTATING = {SideEffect.LOCAL_WRITE, SideEffect.LOCAL_EXEC, SideEffect.REMOTE,
            SideEffect.DESTRUCTIVE}


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.spec.name)
def test_spec_is_well_formed(tool):
    spec = tool.spec
    assert "." in spec.name and spec.name == spec.name.strip().lower(), spec.name
    assert spec.summary.strip(), "summary is what the model reads; it cannot be empty"
    assert spec.category.strip()
    assert isinstance(spec.side_effect, SideEffect)
    names = [p.name for p in spec.params]
    assert len(names) == len(set(names)), f"duplicate params: {names}"
    # ToolRegistry.call(tool_name, ctx, **kwargs): a kwarg named 'ctx' would collide.
    assert "ctx" not in names
    for p in spec.params:
        assert p.type in {"string", "integer", "number", "boolean", "array", "object"}, p
    json.dumps(spec.to_llm_tool())   # must serialise for the model


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.spec.name)
def test_run_is_async_and_verify_exists(tool):
    assert inspect.iscoroutinefunction(tool.run)
    assert callable(getattr(tool, "verify", None))


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.spec.name)
def test_dry_run_param_is_a_boolean_and_optional(tool):
    if not supports_dry_run(tool.spec):
        return
    p = next(p for p in tool.spec.params if p.name == "dry_run")
    assert p.type == "boolean" and not p.required


def test_health_reports_every_tool_with_a_reason():
    health = REGISTRY.health()
    assert set(health) == set(REGISTRY.names())
    for name, h in health.items():
        assert isinstance(h["available"], bool), name
        assert h.get("reason"), f"{name}: health must say why"
        assert h["source"] == "builtin" or h["source"].startswith("plugin:")


def test_mutating_tools_are_never_auto_allowed_by_default():
    from jarvis.config import PolicyConfig
    auto = set(PolicyConfig().auto_allow)
    offenders = sorted(t.spec.name for t in TOOLS
                       if t.spec.side_effect in MUTATING and t.spec.name in auto)
    assert offenders == []
