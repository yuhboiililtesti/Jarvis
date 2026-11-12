"""Code navigation tools (read/search/symbols/diff/git) and the system
monitor probe — the "search and read code, monitor the machine" capability.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin.code_tool import (CodeDiffTool, CodeReadTool,
                                            CodeSearchTool, CodeSymbolsTool,
                                            GitInfoTool)
from jarvis.tools.builtin.monitor_tool import MonitorProbeTool

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ctx(cfg):
    return ToolContext(config=cfg, db=None, bus=None)


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        "class Alpha:\n"
        "    def method_one(self):\n"
        "        return 1\n"
        "\n"
        "def helper(x):\n"
        "    return x + 1\n",
        encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "export function startApp() { return 1; }\n"
        "const helper = (x) => x;\n",
        encoding="utf-8")
    (tmp_path / "notes.txt").write_text("alpha beta\nGamma delta\n",
                                        encoding="utf-8")
    return tmp_path


async def test_code_read_numbers_lines_and_ranges(ctx, tree):
    tool = CodeReadTool()
    full = await tool.run(ctx, path=str(tree / "pkg" / "mod.py"))
    assert full.ok
    assert full.data["total_lines"] == 6
    assert full.data["lines"][0] == {"n": 1, "text": "class Alpha:"}
    window = await tool.run(ctx, path=str(tree / "pkg" / "mod.py"),
                            start_line=2, end_line=3)
    assert window.ok
    assert [row["n"] for row in window.data["lines"]] == [2, 3]
    assert window.data["truncated"] is True


async def test_code_search_finds_content_and_filters(ctx, tree):
    tool = CodeSearchTool()
    res = await tool.run(ctx, path=str(tree), pattern="helper")
    assert res.ok
    paths = {m["path"] for m in res.data["matches"]}
    assert any(p.endswith("mod.py") for p in paths)
    assert any(p.endswith("app.js") for p in paths)
    only_js = await tool.run(ctx, path=str(tree), pattern="helper",
                             include="*.js")
    assert only_js.ok
    assert all(m["path"].endswith(".js") for m in only_js.data["matches"])
    # case sensitivity changes the result set on the text file
    insensitive = await tool.run(ctx, path=str(tree), pattern="gamma")
    sensitive = await tool.run(ctx, path=str(tree), pattern="gamma",
                               case_sensitive=True)
    assert insensitive.data["match_count"] >= 1
    assert sensitive.data["match_count"] == 0


async def test_code_symbols_indexes_definitions(ctx, tree):
    tool = CodeSymbolsTool()
    res = await tool.run(ctx, path=str(tree))
    assert res.ok
    names = {(s["name"], s["kind"]) for s in res.data["symbols"]}
    assert ("Alpha", "class") in names
    assert ("method_one", "function") in names
    assert ("helper", "function") in names
    assert ("startApp", "function") in names


async def test_code_diff_reports_changes(ctx, tree):
    a = tree / "notes.txt"
    b = tree / "notes_changed.txt"
    b.write_text("alpha beta\nGamma epsilon\n", encoding="utf-8")
    tool = CodeDiffTool()
    res = await tool.run(ctx, path_a=str(a), path_b=str(b))
    assert res.ok
    assert res.data["changed"] is True
    assert "epsilon" in res.data["diff"]
    same = await tool.run(ctx, path_a=str(a), path_b=str(a))
    assert same.ok and same.data["changed"] is False


async def test_git_info_read_only_actions(ctx, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    tool = GitInfoTool()
    status = await tool.run(ctx, repo=str(repo), action="status")
    assert status.ok, status.error
    assert "a.txt" in status.data["stdout"]
    assert (await tool.run(ctx, repo=str(repo), action="branch")).ok


async def test_monitor_probe_snapshot(ctx):
    tool = MonitorProbeTool()
    res = await tool.run(ctx, interval_s=0.05, top=5)
    assert res.ok, res.error
    data = res.data
    assert 0.0 <= data["cpu"]["percent"] <= 100.0
    assert data["memory"]["total_gb"] > 0
    assert data["health"] in ("ok", "warning", "critical")
    assert len(data["top_cpu"]) <= 5


async def test_code_tools_enforce_the_file_boundary(ctx):
    tool = CodeReadTool()
    res = await tool.run(ctx, path=r"C:\Windows\win.ini")
    assert res.ok is False
    assert res.error


async def test_new_tools_registered_and_auto_allowed(kernel):
    # canonical names are registered; system.snapshot is an alias of
    # monitor.probe and resolves through REGISTRY.get
    for name in ("code.read", "code.search", "code.symbols", "code.diff",
                 "git.info", "monitor.probe", "system.snapshot"):
        assert REGISTRY.get(name) is not None, name
    for name in ("code.read", "code.search", "code.symbols", "code.diff",
                 "git.info", "monitor.probe"):
        assert name in kernel.config.policy.auto_allow, name
