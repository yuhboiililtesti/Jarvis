"""Plugin lifecycle: discover → enable (pin) → load; change, ceiling and shadowing refusals."""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from jarvis.plugins import PluginManager
from jarvis.tools import REGISTRY, load_builtin_tools

TEMPLATE = Path(__file__).resolve().parent.parent / "plugins" / "_template"

TOOL_SRC = textwrap.dedent('''
    from jarvis.tools import BaseTool, SideEffect, ToolResult, ToolSpec, register

    @register()
    class T(BaseTool):
        spec = ToolSpec(name="{name}", summary="test tool", side_effect=SideEffect.{side})

        async def run(self, ctx, **kw):
            return ToolResult(ok=True, data="hi")
''')


def _plugin(root: Path, pid: str, *, tool: str, side: str = "NONE",
            ceiling: str = "none") -> Path:
    folder = root / pid
    folder.mkdir(parents=True)
    (folder / "plugin.toml").write_text(
        f'id = "{pid}"\nversion = "1.0"\nmodules = ["tools"]\nmax_side_effect = "{ceiling}"\n',
        encoding="utf-8")
    (folder / "tools.py").write_text(TOOL_SRC.format(name=tool, side=side), encoding="utf-8")
    return folder


@pytest.fixture
def mgr(tmp_path, db):
    load_builtin_tools()
    m = PluginManager(tmp_path / "plugins", db, REGISTRY)
    yield m
    for info in m.plugins.values():   # keep the global registry clean for other tests
        m._back_out(info)


def test_not_loaded_until_enabled(mgr, tmp_path):
    _plugin(tmp_path / "plugins", "alpha", tool="alpha.hello")
    infos = mgr.load_enabled()
    assert infos[0].state == "disabled"
    assert REGISTRY.get("alpha.hello") is None


def test_enable_pins_and_loads_with_provenance(mgr, tmp_path, db):
    _plugin(tmp_path / "plugins", "alpha", tool="alpha.hello")
    mgr.enable("alpha")
    infos = mgr.load_enabled()
    assert infos[0].state == "loaded" and infos[0].tools == ["alpha.hello"]
    assert REGISTRY.source_of("alpha.hello") == "plugin:alpha"
    assert db.query_one("SELECT 1 FROM audit WHERE action='plugin.enable'") is not None


def test_changed_files_are_refused_until_reapproved(mgr, tmp_path):
    folder = _plugin(tmp_path / "plugins", "alpha", tool="alpha.hello")
    mgr.enable("alpha")
    (folder / "tools.py").write_text(
        (folder / "tools.py").read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    infos = mgr.load_enabled()
    assert infos[0].state == "changed"
    assert REGISTRY.get("alpha.hello") is None


def test_side_effect_above_ceiling_backs_out(mgr, tmp_path):
    _plugin(tmp_path / "plugins", "beta", tool="beta.wipe", side="DESTRUCTIVE", ceiling="none")
    mgr.enable("beta")
    infos = mgr.load_enabled()
    assert infos[0].state == "refused" and "ceiling" in infos[0].reason
    assert REGISTRY.get("beta.wipe") is None


def test_cannot_shadow_a_builtin(mgr, tmp_path):
    original = REGISTRY.get("time.now")
    assert original is not None
    _plugin(tmp_path / "plugins", "gamma", tool="time.now")
    mgr.enable("gamma")
    infos = mgr.load_enabled()
    assert infos[0].state == "refused" and "replace" in infos[0].reason
    assert REGISTRY.get("time.now") is original
    assert REGISTRY.source_of("time.now") == "builtin"


def test_invalid_manifest_is_reported_not_loaded(mgr, tmp_path):
    folder = tmp_path / "plugins" / "Bad Name"
    folder.mkdir(parents=True)
    (folder / "plugin.toml").write_text('id = "Bad Name"\nmodules = []\n', encoding="utf-8")
    infos = mgr.discover()
    assert infos[0].state == "invalid"
    with pytest.raises(ValueError):
        mgr.enable("Bad Name")


def test_disable_withdraws_approval(mgr, tmp_path):
    _plugin(tmp_path / "plugins", "alpha", tool="alpha.hello")
    mgr.enable("alpha")
    assert mgr.disable("alpha") is True
    assert mgr.discover()[0].state == "disabled"


def test_shipped_template_is_valid_and_loads(mgr, tmp_path):
    dest = tmp_path / "plugins" / "wordcount"
    shutil.copytree(TEMPLATE, dest)
    toml = (dest / "plugin.toml").read_text(encoding="utf-8")
    (dest / "plugin.toml").write_text(toml.replace('id = "_template"', 'id = "wordcount"'),
                                      encoding="utf-8")
    mgr.enable("wordcount")
    infos = mgr.load_enabled()
    assert infos[0].state == "loaded", infos[0].reason
    assert REGISTRY.health()["template.word_count"]["available"] is True
