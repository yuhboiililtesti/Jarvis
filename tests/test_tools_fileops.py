"""files.copy / move / mkdir / hash / zip / unzip / duplicates / disk_usage.

All work happens under the test's temporary data dir (an allowed root via
cfg.data_path). Oracles are computed independently with hashlib/zipfile.
"""

from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from pathlib import Path

import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import SideEffect, ToolContext
from jarvis.tools.builtin import file_ops


@pytest.fixture
def ctx(cfg):
    return ToolContext(config=cfg, db=None, bus=None)


@pytest.fixture
def ws(cfg):
    root = Path(cfg.data_path) / "ws"
    root.mkdir(parents=True)
    return root


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_specs():
    expect = {"files.copy": SideEffect.LOCAL_WRITE, "files.move": SideEffect.LOCAL_WRITE,
              "files.mkdir": SideEffect.LOCAL_WRITE, "files.hash": SideEffect.NONE,
              "files.zip": SideEffect.LOCAL_WRITE, "files.unzip": SideEffect.LOCAL_WRITE,
              "files.duplicates": SideEffect.NONE, "files.disk_usage": SideEffect.NONE}
    for name, side in expect.items():
        spec = REGISTRY.get(name).spec
        assert spec.side_effect == side
        names = [p.name for p in spec.params]
        assert "allowed_roots" not in names, "boundary must not be model-widenable"
        assert ("dry_run" in names) == (side != SideEffect.NONE)


async def test_hash_matches_hashlib(ctx, ws):
    f = ws / "a.bin"
    f.write_bytes(os.urandom(300_000))
    r = await REGISTRY.call("files.hash", ctx, path=str(f))
    assert r.ok and r.data["hexdigest"] == sha(f) and r.data["size"] == 300_000
    r = await REGISTRY.call("files.hash", ctx, path=str(f), algorithm="md5")
    assert r.ok and r.data["hexdigest"] == hashlib.md5(f.read_bytes()).hexdigest()
    r = await REGISTRY.call("files.hash", ctx, path=str(ws))
    assert not r.ok and "not a file" in r.error


async def test_copy_file_verified_and_no_overwrite(ctx, ws):
    src = ws / "src.txt"
    src.write_text("hello copy", encoding="utf-8")
    tool = REGISTRY.get("files.copy")
    r = await tool.run(ctx, source=str(src), destination=str(ws / "dst.txt"))
    assert r.ok and r.data["sha256"] == sha(src)
    assert (ws / "dst.txt").read_text(encoding="utf-8") == "hello copy"
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["method"] == "sha256-readback"
    assert not [p for p in ws.iterdir() if p.name.startswith(".jarvis-tmp-")]
    # existing destination is refused, content untouched
    (ws / "dst.txt").write_text("keep me", encoding="utf-8")
    r = await tool.run(ctx, source=str(src), destination=str(ws / "dst.txt"))
    assert not r.ok and "no overwrite" in r.error
    assert (ws / "dst.txt").read_text(encoding="utf-8") == "keep me"


async def test_copy_verify_detects_tamper(ctx, ws):
    src = ws / "s.txt"
    src.write_text("one", encoding="utf-8")
    tool = REGISTRY.get("files.copy")
    r = await tool.run(ctx, source=str(src), destination=str(ws / "d.txt"))
    (ws / "d.txt").write_text("two", encoding="utf-8")
    v = await tool.verify(ctx, r)
    assert v["verified"] is False


async def test_copy_directory_manifest(ctx, ws):
    d = ws / "tree"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("a", encoding="utf-8")
    (d / "sub" / "b.txt").write_text("bb", encoding="utf-8")
    tool = REGISTRY.get("files.copy")
    r = await tool.run(ctx, source=str(d), destination=str(ws / "tree2"))
    assert r.ok and r.data["kind"] == "dir" and r.data["files"] == 2
    assert (ws / "tree2" / "sub" / "b.txt").read_text(encoding="utf-8") == "bb"
    assert (await tool.verify(ctx, r))["verified"] is True
    r = await tool.run(ctx, source=str(d), destination=str(d / "inside"))
    assert not r.ok and "inside" in r.error


async def test_copy_dry_run(ctx, ws):
    src = ws / "s.txt"
    src.write_text("x", encoding="utf-8")
    r = await REGISTRY.get("files.copy").run(ctx, source=str(src),
                                             destination=str(ws / "d.txt"), dry_run=True)
    assert r.ok and r.data["dry_run"] and not (ws / "d.txt").exists()
    assert "files.delete" in r.data["rollback"]


async def test_move_same_volume_and_verify(ctx, ws):
    src = ws / "m.txt"
    src.write_text("move me", encoding="utf-8")
    digest = sha(src)
    tool = REGISTRY.get("files.move")
    r = await tool.run(ctx, source=str(src), destination=str(ws / "moved.txt"))
    assert r.ok and r.data["method"] == "atomic-rename" and r.data["sha256"] == digest
    assert not src.exists()
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["source_gone"] is True


async def test_move_cross_volume_keeps_source_on_hash_mismatch(ctx, ws, monkeypatch):
    src = ws / "x.txt"
    src.write_text("precious", encoding="utf-8")
    real_stat = os.stat

    class _St:
        def __init__(self, st, dev):
            self._st, self.st_dev = st, dev

        def __getattr__(self, k):
            return getattr(self._st, k)

    def fake_stat(p, *a, **k):          # pretend source and destination are different volumes
        st = real_stat(p, *a, **k)
        return _St(st, 1 if Path(p) == src else 2)
    monkeypatch.setattr(file_ops.os, "stat", fake_stat)

    real_verify = file_ops._verify_hash
    monkeypatch.setattr(file_ops, "_verify_hash",
                        lambda p, e, c: dict(real_verify(p, e, c), verified=False))
    r = await REGISTRY.get("files.move").run(ctx, source=str(src),
                                             destination=str(ws / "y.txt"))
    assert not r.ok and "source kept" in r.error
    assert src.read_text(encoding="utf-8") == "precious"

    monkeypatch.setattr(file_ops, "_verify_hash", real_verify)
    r = await REGISTRY.get("files.move").run(ctx, source=str(src),
                                             destination=str(ws / "z.txt"))
    assert r.ok and r.data["method"] == "copy+sha256+delete-source"
    assert not src.exists() and (ws / "z.txt").read_text(encoding="utf-8") == "precious"


async def test_path_escape_refused(ctx, ws, tmp_path):
    outside = tmp_path / "outside.txt"          # tmp_path itself is not an allowed root
    outside.write_text("x", encoding="utf-8")
    inside = ws / "in.txt"
    inside.write_text("y", encoding="utf-8")
    if any(str(outside).lower().startswith(str(r).lower())
           for r in file_ops._allowed_roots(ctx)):
        pytest.skip("tmp_path sits inside an allowed root (home) on this machine")
    for name, kw in (("files.copy", {"source": str(outside), "destination": str(ws / "o")}),
                     ("files.copy", {"source": str(inside), "destination": str(tmp_path / "o")}),
                     ("files.hash", {"path": str(outside)}),
                     ("files.mkdir", {"path": str(tmp_path / "newdir")})):
        r = await REGISTRY.get(name).run(ctx, **kw)
        assert not r.ok and "outside the allowed roots" in r.error, name


async def test_dotdot_escape_refused(ctx, ws):
    r = await REGISTRY.get("files.mkdir").run(
        ctx, path=str(ws) + "/../../../../../../../../../../../../../Windows/jarvis_test")
    assert not r.ok


async def test_protected_roots_refused_inside_allowed_root(ctx, ws, cfg):
    secrets = Path(cfg.data_path) / "secrets.json"
    secrets.write_text("{}", encoding="utf-8")
    r = await REGISTRY.get("files.copy").run(ctx, source=str(secrets),
                                             destination=str(ws / "leak.json"))
    assert not r.ok and "protected" in r.error and r.evidence.get("refused")
    r = await REGISTRY.get("files.hash").run(ctx, path=str(Path.home() / ".ssh" / "id_rsa"))
    assert not r.ok and "protected" in r.error
    prot = [str(p).lower() for p in file_ops.protected_roots(ctx)]
    if os.name == "nt":
        assert any(p.endswith("\\windows") for p in prot)
        assert any("credentials" in p for p in prot)


async def test_walk_skips_protected(ctx, ws, cfg):
    (Path(cfg.data_path) / "secrets.json").write_text("{}", encoding="utf-8")
    files, skipped, _ = file_ops._walk_files(Path(cfg.data_path), ctx)
    assert not any(p.name == "secrets.json" for p in files) and skipped >= 1


async def test_mkdir(ctx, ws):
    tool = REGISTRY.get("files.mkdir")
    r = await tool.run(ctx, path=str(ws / "a" / "b"))
    assert r.ok and r.data["created"] and (ws / "a" / "b").is_dir()
    assert tool.verify(ctx, r)["verified"] is True
    r = await tool.run(ctx, path=str(ws / "a" / "b"))
    assert r.ok and r.data["created"] is False
    r = await tool.run(ctx, path=str(ws / "c" / "d"), parents=False)
    assert not r.ok and "parent" in r.error
    r = await tool.run(ctx, path=str(ws / "e"), dry_run=True)
    assert r.ok and r.data["dry_run"] and not (ws / "e").exists()


async def test_zip_and_unzip_roundtrip(ctx, ws):
    d = ws / "proj"
    (d / "sub").mkdir(parents=True)
    (d / "one.txt").write_text("1", encoding="utf-8")
    (d / "sub" / "two.bin").write_bytes(os.urandom(5000))
    single = ws / "single.txt"
    single.write_text("s", encoding="utf-8")
    zt = REGISTRY.get("files.zip")
    r = await zt.run(ctx, sources=[str(d), str(single)], destination=str(ws / "out.zip"))
    assert r.ok and r.data["entries"] == 3
    with zipfile.ZipFile(ws / "out.zip") as zf:
        assert sorted(zf.namelist()) == ["proj/one.txt", "proj/sub/two.bin", "single.txt"]
    assert (await zt.verify(ctx, r))["verified"] is True
    r2 = await zt.run(ctx, sources=[str(d)], destination=str(ws / "out.zip"))
    assert not r2.ok and "no overwrite" in r2.error

    ut = REGISTRY.get("files.unzip")
    r = await ut.run(ctx, archive=str(ws / "out.zip"), destination=str(ws / "x"))
    assert r.ok and r.data["count"] == 3
    assert sha(ws / "x" / "proj" / "sub" / "two.bin") == sha(d / "sub" / "two.bin")
    v = await ut.verify(ctx, r)
    assert v["verified"] is True and v["checked"] == 3
    # second extraction would overwrite -> refused before writing anything
    r = await ut.run(ctx, archive=str(ws / "out.zip"), destination=str(ws / "x"))
    assert not r.ok and "overwrite" in r.error


def _evil_zip(path: Path, name: str, data: bytes = b"evil", symlink: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ok.txt", b"fine")
        info = zipfile.ZipInfo(name)
        if symlink:
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, data)


@pytest.mark.parametrize("member", [
    "../escape.txt", "a/../../escape.txt", "/abs/escape.txt", "C:/Windows/escape.txt",
    "..\\escape.txt", "sub/file.txt:stream", "CON.txt", "nul",
])
async def test_unzip_zip_slip_refused(ctx, ws, member):
    arc = ws / "evil.zip"
    _evil_zip(arc, member)
    r = await REGISTRY.get("files.unzip").run(ctx, archive=str(arc), destination=str(ws / "out"))
    assert not r.ok and "unsafe member" in r.error and r.evidence.get("refused")
    assert not (ws / "out" / "ok.txt").exists(), "nothing may be written before validation"
    assert not (ws / "escape.txt").exists() and not (ws.parent / "escape.txt").exists()


async def test_unzip_symlink_member_refused(ctx, ws):
    arc = ws / "link.zip"
    _evil_zip(arc, "link", data=b"../../etc/passwd", symlink=True)
    r = await REGISTRY.get("files.unzip").run(ctx, archive=str(arc), destination=str(ws / "o"))
    assert not r.ok and "symlink" in r.error


async def test_unzip_bomb_guard(ctx, ws):
    arc = ws / "bomb.zip"
    with zipfile.ZipFile(arc, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", b"\0" * (20 * 1024 * 1024))
    r = await REGISTRY.get("files.unzip").run(ctx, archive=str(arc), destination=str(ws / "b"))
    assert not r.ok and "zip bomb" in r.error


async def test_unzip_dry_run_and_bad_zip(ctx, ws):
    arc = ws / "fine.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("a.txt", b"a")
    r = await REGISTRY.get("files.unzip").run(ctx, archive=str(arc), destination=str(ws / "d"),
                                              dry_run=True)
    assert r.ok and r.data["dry_run"] and not (ws / "d").exists()
    junk = ws / "junk.zip"
    junk.write_bytes(b"not a zip")
    r = await REGISTRY.get("files.unzip").run(ctx, archive=str(junk), destination=str(ws / "d"))
    assert not r.ok and "valid zip" in r.error


async def test_duplicates(ctx, ws):
    payload = os.urandom(2048)
    (ws / "a").mkdir()
    (ws / "a" / "1.bin").write_bytes(payload)
    (ws / "2.bin").write_bytes(payload)
    # Guarantee the third file differs even when the random last byte is 'x'.
    (ws / "3.bin").write_bytes(payload[:-1] + bytes([(payload[-1] + 1) % 256]))
    r = await REGISTRY.call("files.duplicates", ctx, root=str(ws))
    assert r.ok and r.data["group_count"] == 1
    g = r.data["groups"][0]
    assert len(g["paths"]) == 2 and g["wasted_bytes"] == 2048
    assert g["sha256"] == hashlib.sha256(payload).hexdigest()


async def test_disk_usage(ctx, ws):
    (ws / "big").mkdir()
    (ws / "big" / "f").write_bytes(b"x" * 10_000)
    (ws / "small.txt").write_bytes(b"y" * 10)
    r = await REGISTRY.call("files.disk_usage", ctx, path=str(ws))
    assert r.ok and r.data["size_bytes"] == 10_010 and r.data["files"] == 2
    assert r.data["largest"][0] == {"name": "big", "bytes": 10_000}
    assert r.data["volume"]["total_bytes"] > 0
    r = await REGISTRY.call("files.disk_usage", ctx, path=str(ws / "nope"))
    assert not r.ok


async def test_system_paths_outside_roots_refused(ctx, ws):
    if os.name == "nt":
        sysfile = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "win.ini"
        newdir = Path(Path.home().anchor) / "jarvis_escape_probe"
    else:
        sysfile, newdir = Path("/etc/hostname"), Path("/jarvis_escape_probe")
    r = await REGISTRY.get("files.hash").run(ctx, path=str(sysfile))
    assert not r.ok and "outside the allowed roots" in r.error
    r = await REGISTRY.get("files.mkdir").run(ctx, path=str(newdir))
    assert not r.ok and not newdir.exists()
    src = ws / "s.txt"
    src.write_text("x", encoding="utf-8")
    r = await REGISTRY.get("files.copy").run(ctx, source=str(src),
                                             destination=str(newdir / "s.txt"))
    assert not r.ok and "outside the allowed roots" in r.error
