"""The desktop lab action: confinement and contract (no GUI in tests)."""

from __future__ import annotations

from jarvis.tools import REGISTRY
from jarvis.tools.base import ToolContext, SideEffect
from jarvis.tools.builtin import desktop_lab  # noqa: F401  (registration)


def _ctx(cfg):
    return ToolContext(config=cfg, db=None, bus=None)


async def test_desktop_lab_is_a_local_exec_tool_with_the_right_schema():
    tool = REGISTRY.get("desktop.lab_write")
    assert tool is not None
    spec = tool.spec
    assert spec.side_effect is SideEffect.LOCAL_EXEC
    assert {p.name for p in spec.params} == {"filename", "text"}
    assert "confined" in spec.summary.lower()


async def test_desktop_lab_rejects_paths_and_non_text_files(cfg):
    tool = REGISTRY.get("desktop.lab_write")
    for bad in ("../escape.txt", "dir/file.txt", r"dir\\file.txt",
                "..", "payload.exe"):
        result = await tool.run(_ctx(cfg), filename=bad, text="x")
        assert result.ok is False, bad
        assert result.error
