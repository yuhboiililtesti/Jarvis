"""Regression tests for the files.* boundary.

These guard two model-facing escapes found in the night audit:

* ``allowed_roots`` used to be a tool parameter, so a prompt-injected call could
  widen its own file scope (``files.read`` on ``data/api-token`` or ``~/.ssh``).
* ``files.*`` shared the allowed-root check with ``file_ops`` but not its
  protected-root check, so JARVIS's own bearer tokens and credential stores
  were readable even though they sat under the allowed home/data roots.
"""

from __future__ import annotations

from pathlib import Path

from jarvis.tools import REGISTRY
from jarvis.tools.base import SideEffect, ToolContext
from jarvis.tools.builtin import files


def ctx(cfg) -> ToolContext:
    return ToolContext(config=cfg, db=None, bus=None)


def test_files_tools_do_not_expose_a_model_widenable_root():
    for name in ("files.read", "files.write", "files.list", "files.search",
                 "files.delete"):
        spec = REGISTRY.get(name).spec
        assert "allowed_roots" not in [p.name for p in spec.params], name


async def test_allowed_roots_kwarg_cannot_widen_the_boundary(cfg, tmp_path, monkeypatch):
    # Keep "home" at the data dir so tmp_path is genuinely outside every root.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(cfg.data_path)))
    outside = tmp_path / "outside.txt"
    outside.write_text("not for jarvis", encoding="utf-8")
    r = await REGISTRY.get("files.read").run(
        ctx(cfg), path=str(outside), allowed_roots=[str(tmp_path)])
    assert not r.ok
    assert "outside the allowed roots" in (r.error or "")


async def test_runtime_tokens_and_secret_store_are_refused(cfg):
    data = Path(cfg.data_path)
    (data / "api-token").write_text("test-token-value\n", encoding="utf-8")
    (data / "secrets.json").write_text('{"k":"v"}', encoding="utf-8")
    reader = REGISTRY.get("files.read")
    for name in ("api-token", "secrets.json"):
        r = await reader.run(ctx(cfg), path=str(data / name))
        assert not r.ok, name
        assert "protected location" in (r.error or ""), r.error


async def test_write_to_the_token_file_is_refused(cfg):
    target = Path(cfg.data_path) / "api-token"
    target.write_text("keep\n", encoding="utf-8")
    r = await REGISTRY.get("files.write").run(
        ctx(cfg), path=str(target), content="replace")
    assert not r.ok
    assert "protected location" in (r.error or "")
    assert target.read_text(encoding="utf-8") == "keep\n"


async def test_search_does_not_return_protected_files(cfg):
    data = Path(cfg.data_path)
    (data / "api-token").write_text("x", encoding="utf-8")
    (data / "visible.txt").write_text("ok", encoding="utf-8")
    r = await REGISTRY.get("files.search").run(
        ctx(cfg), root=str(data), pattern="*", recursive=False)
    assert r.ok
    names = {m["name"] for m in r.data["matches"]}
    assert "visible.txt" in names
    assert "api-token" not in names


async def test_home_credential_stores_are_refused(cfg, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    key = home / ".ssh" / "id_ed25519"
    key.write_text("PRIVATE", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    r = await REGISTRY.get("files.read").run(ctx(cfg), path=str(key))
    assert not r.ok
    assert "protected location" in (r.error or "")


async def test_read_does_not_hash_a_huge_file(cfg, monkeypatch):
    p = Path(cfg.data_path) / "huge.bin"
    with p.open("wb") as fh:
        fh.seek(40 * 1024 * 1024)
        fh.write(b"x")

    def must_not_hash(*args, **kwargs):
        raise AssertionError("files.read must not hash a file above MAX_HASH_BYTES")

    monkeypatch.setattr(files, "_sha256_file", must_not_hash)
    r = await REGISTRY.get("files.read").run(
        ctx(cfg), path=str(p), max_bytes=1024)
    assert r.ok
    assert r.data["sha256"] is None and r.data["sha256_skipped"] is True
    assert r.data["truncated"] is True


async def test_ordinary_file_still_reads(cfg):
    p = Path(cfg.data_path) / "notes" / "hello.txt"
    p.parent.mkdir(parents=True)
    p.write_text("hello", encoding="utf-8")
    r = await REGISTRY.get("files.read").run(ctx(cfg), path=str(p))
    assert r.ok and r.data["text"] == "hello"


def test_files_delete_is_still_destructive():
    assert REGISTRY.get("files.delete").spec.side_effect == SideEffect.DESTRUCTIVE
