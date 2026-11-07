"""Shared pytest fixtures for the JARVIS suite.

Tests must run with NO local model, NO voice stack and NO network. Anything that
cannot work in that environment should be skipped, not faked.
"""

from __future__ import annotations

import pytest

from jarvis.config import Config


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.data_dir = str(tmp_path / "data")
    c.memory.db_path = str(tmp_path / "data" / "test.db")
    c.voice.enabled = False
    c.monitoring.enabled = False
    c.night.enabled = False
    c.nodes.enabled = False
    c.data_dir = str(tmp_path / "data")
    c.ensure_dirs()
    return c


@pytest.fixture
def db(cfg):
    from jarvis.db import Database
    d = Database(cfg.db_path)
    yield d
    d.close()


@pytest.fixture
def kernel(cfg):
    from jarvis.core import build
    j = build(cfg)
    yield j
    j.db.close()


class EchoTool:
    """Read-only test tool."""
    from jarvis.tools.base import SideEffect, ToolParam, ToolSpec

    spec = ToolSpec(
        name="test.echo",
        summary="Echo back the given text",
        category="test",
        side_effect=SideEffect.NONE,
        params=(ToolParam("text", required=True, description="text to echo"),),
    )

    async def run(self, ctx, **kwargs):
        from jarvis.tools.base import ToolResult
        return ToolResult(ok=True, data={"echo": kwargs.get("text")})

    def verify(self, ctx, result):
        return {"verified": True, "method": "read-only"}


class WriteTool:
    """Side-effecting test tool that writes a file and can be verified."""
    from jarvis.tools.base import SideEffect, ToolParam, ToolSpec

    spec = ToolSpec(
        name="test.write",
        summary="Write text to a path",
        category="test",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("path", required=True, description="file path"),
            ToolParam("content", required=False, description="content"),
        ),
    )

    async def run(self, ctx, **kwargs):
        from pathlib import Path
        from jarvis.tools.base import ToolResult
        p = Path(kwargs["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(kwargs.get("content", ""), encoding="utf-8")
        return ToolResult(ok=True, data={"path": str(p)})

    def verify(self, ctx, result):
        from pathlib import Path
        p = Path(result.data["path"])
        return {"verified": p.exists(), "method": "file-exists", "path": str(p)}


class FailTool:
    from jarvis.tools.base import SideEffect, ToolSpec

    spec = ToolSpec(name="test.fail", summary="Always fails",
                    category="test", side_effect=SideEffect.NONE)

    async def run(self, ctx, **kwargs):
        from jarvis.tools.base import ToolResult
        return ToolResult(ok=False, error="intentional failure")

    def verify(self, ctx, result):
        return None


class GatedTool:
    """Mutating tool that is deliberately NOT auto-allowed, to test approvals."""
    from jarvis.tools.base import SideEffect, ToolParam, ToolSpec

    spec = ToolSpec(
        name="test.gated",
        summary="A gated write",
        category="test",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(ToolParam("path", required=True, description="file path"),),
    )

    async def run(self, ctx, **kwargs):
        from pathlib import Path
        from jarvis.tools.base import ToolResult
        p = Path(kwargs["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("gated", encoding="utf-8")
        return ToolResult(ok=True, data={"path": str(p)})

    def verify(self, ctx, result):
        from pathlib import Path
        return {"verified": Path(result.data["path"]).exists(), "method": "exists"}


@pytest.fixture
def register_test_tools(cfg):
    """Register test tools into the global registry and auto-allow the safe ones."""
    from jarvis.tools import REGISTRY
    REGISTRY.add(EchoTool())
    REGISTRY.add(WriteTool())
    REGISTRY.add(FailTool())
    REGISTRY.add(GatedTool())
    for name in ("test.echo", "test.write", "test.fail"):
        if name not in cfg.policy.auto_allow:
            cfg.policy.auto_allow.append(name)
    # test.gated is intentionally NOT auto-allowed
    return REGISTRY
