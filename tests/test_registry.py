"""Tool registry validation and LLM exposure filtering."""

from __future__ import annotations

import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import SideEffect, ToolContext, ToolResult, ToolSpec


def ctx(cfg, db):
    return ToolContext(config=cfg, db=db, bus=None)


def test_registry_has_expected_builtin_categories():
    cats = REGISTRY.by_category()
    assert cats, "no tools registered at all"
    for expected in ("system", "files", "notes"):
        assert expected in cats, f"missing tool category {expected}"


def test_call_unknown_tool_returns_error(cfg, db):
    import asyncio

    async def run():
        return await REGISTRY.call("does.not.exist", ctx(cfg, db))
    res = asyncio.run(run())
    assert not res.ok and "unknown tool" in res.error


def test_validation_rejects_missing_required(cfg, db, register_test_tools):
    import asyncio

    async def run():
        return await REGISTRY.call("test.echo", ctx(cfg, db))  # 'text' required
    res = asyncio.run(run())
    assert not res.ok and "missing required parameter" in res.error


def test_llm_tools_are_openai_shaped(cfg):
    tools = REGISTRY.llm_tools()
    assert tools
    sample = tools[0]
    assert sample["type"] == "function"
    assert "name" in sample["function"]
    assert "parameters" in sample["function"]


def test_side_effect_ceiling_filters_mutating_tools(cfg):
    read_only = REGISTRY.llm_tools(max_side_effect=SideEffect.NONE)
    names = {t["function"]["name"] for t in read_only}
    assert "files.write" not in names
    assert "files.delete" not in names
    assert "system.info" in names


def test_every_tool_declares_a_spec_and_summary():
    for tool in REGISTRY.all():
        assert isinstance(tool.spec, ToolSpec)
        assert tool.spec.name
        assert tool.spec.summary
        assert tool.spec.side_effect in SideEffect


def test_required_params_have_descriptions():
    for spec in REGISTRY.specs():
        for p in spec.params:
            if p.required:
                assert p.description, f"{spec.name}.{p.name} has no description"
