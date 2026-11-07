"""Config loading: Windows-friendly parsing and env precedence.

The config file is edited by hand on Windows, where Notepad and PowerShell 5.1
both like to prepend a UTF-8 BOM. ``tomllib`` rejects a BOM, so a perfectly
valid file would stop the Core from starting. That is a real failure mode, not a
theoretical one — this test exists because it happened.
"""

from __future__ import annotations

from jarvis.config import Config, load_config


def test_loads_a_plain_toml(tmp_path):
    path = tmp_path / "jarvis.toml"
    path.write_text('core_name = "unit-core"\n\n[runtime]\ntick_s = 1.5\n',
                    encoding="utf-8")
    cfg = load_config(path)
    assert cfg.core_name == "unit-core"
    assert cfg.runtime.tick_s == 1.5


def test_loads_a_toml_with_a_utf8_bom(tmp_path):
    path = tmp_path / "jarvis.toml"
    path.write_bytes(b"\xef\xbb\xbf" + b'core_name = "bom-core"\n')
    cfg = load_config(path)
    assert cfg.core_name == "bom-core"


def test_missing_file_falls_back_to_defaults(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.toml")
    assert isinstance(cfg, Config)
    assert cfg.runtime.enabled is True


def test_env_overrides_a_section_value(tmp_path, monkeypatch):
    path = tmp_path / "jarvis.toml"
    path.write_text("[runtime]\ntick_s = 9.0\n", encoding="utf-8")
    monkeypatch.setenv("JARVIS_RUNTIME__TICK_S", "0.5")
    assert load_config(path).runtime.tick_s == 0.5


def test_env_list_override_is_split_not_iterated_as_characters(tmp_path, monkeypatch):
    path = tmp_path / "jarvis.toml"
    path.write_text("[runtime]\nevent_topics = []\n", encoding="utf-8")
    monkeypatch.setenv("JARVIS_RUNTIME__EVENT_TOPICS", "alert, security.*")
    assert load_config(path).runtime.event_topics == ["alert", "security.*"]
