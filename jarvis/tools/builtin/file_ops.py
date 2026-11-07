"""File operations beyond ``files.*``: copy, move, mkdir, hash, zip, unzip,
duplicates, disk_usage.

Security boundary (same as ``files.py``, whose guard is reused verbatim):
  * every path is resolved (symlinks/junctions followed) and must sit inside an
    allowed root (``files._safe_path``);
  * **protected roots** are refused even when they are inside an allowed root:
    OS system directories, boot/recovery areas, credential stores (``.ssh``,
    ``.gnupg``, Windows Credentials/Protect/Vault, browser profile stores) and
    JARVIS's own ``secrets.json`` / database. Tree walks skip them silently and
    report how many entries were skipped;
  * these tools do **not** expose the ``allowed_roots`` parameter that the
    older ``files.*`` tools expose to the model: widening the boundary is an
    owner/config decision (``config.files.allowed_roots`` or
    ``JARVIS_FILES_ALLOWED_ROOTS``), never a model argument.

Integrity:
  * nothing overwrites: an existing destination is refused (delete it first with
    the DESTRUCTIVE ``files.delete`` if that is really intended);
  * copy/zip build into a temporary sibling, check SHA-256 against the source,
    then place the result with a no-clobber atomic rename;
  * move uses an atomic rename on the same volume; across volumes it copies to a
    temp file, verifies the SHA-256, places it, re-verifies, and only then
    deletes the source;
  * unzip is zip-slip safe (absolute paths, drive letters, ``..``, ADS colons,
    device names, symlink members and encrypted members are refused; every
    target must resolve inside the destination), bounded against zip bombs, and
    validates the whole archive before writing a single file.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common, _win
from .files import (_allowed_roots, _norm, _protected_hit, _safe_path,
                    _sha256_file, _within, protected_roots)

log = get_logger("jarvis.tools.file_ops")

__all__ = [
    "FilesCopyTool", "FilesMoveTool", "FilesMkdirTool", "FilesHashTool", "FilesZipTool",
    "FilesUnzipTool", "FilesDuplicatesTool", "FilesDiskUsageTool", "protected_roots",
    "check_zip_member", "health", "MAX_UNZIP_BYTES", "MAX_UNZIP_MEMBERS",
]

MAX_TREE_FILES = 20000
MAX_TREE_BYTES = 20 * 1024 ** 3
MAX_UNZIP_MEMBERS = 10000
MAX_UNZIP_BYTES = 4 * 1024 ** 3
MAX_COMPRESSION_RATIO = 200
_TMP_PREFIX = ".jarvis-tmp-"


def health() -> dict[str, Any]:
    return _common.health_ok("stdlib (shutil, zipfile, hashlib)")


# ---------------------------------------------------------------------------
# protected roots (shared with files.py and vision.ocr)
# ---------------------------------------------------------------------------

def _guard(ctx: ToolContext, raw: Any, what: str) -> Path:
    """Allowed-root check + protected-root refusal. Raises ValueError/PermissionError."""
    if raw is None or not str(raw).strip():
        raise ValueError(f"{what} is required")
    path = _safe_path(ctx, raw)
    hit = _protected_hit(path, protected_roots(ctx))
    if hit is not None:
        raise PermissionError(f"refused: {what} '{path}' is inside protected location '{hit}'")
    return path


def _is_allowed_root(ctx: ToolContext, path: Path) -> bool:
    return any(_norm(path) == _norm(r) for r in _allowed_roots(ctx))


def _walk_files(root: Path, ctx: ToolContext, *, max_files: int = MAX_TREE_FILES
                ) -> tuple[list[Path], int, bool]:
    """Regular files under root, skipping symlinks, protected roots and anything that
    resolves outside the allowed roots. Returns (files, skipped, truncated)."""
    prot = protected_roots(ctx)
    allowed = _allowed_roots(ctx)
    files: list[Path] = []
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        keep = []
        for d in dirnames:
            p = here / d
            if p.is_symlink() or _protected_hit(p.resolve(strict=False), prot):
                skipped += 1
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            p = here / f
            try:
                if p.is_symlink() or not p.is_file():
                    skipped += 1
                    continue
                rp = p.resolve(strict=False)
            except OSError:
                skipped += 1
                continue
            if _protected_hit(rp, prot) or not any(_within(rp, a) for a in allowed):
                skipped += 1
                continue
            files.append(p)
            if len(files) >= max_files:
                return files, skipped, True
    return files, skipped, False


def _manifest(root: Path, ctx: ToolContext) -> dict[str, str]:
    files, _skipped, truncated = _walk_files(root, ctx)
    if truncated:
        raise ValueError(f"directory has more than {MAX_TREE_FILES} files; refusing")
    return {p.relative_to(root).as_posix(): _sha256_file(p)[0] for p in files}


def _tmp_sibling(dest: Path) -> Path:
    return dest.parent / f"{_TMP_PREFIX}{uuid.uuid4().hex[:12]}-{dest.name}"


def _place_no_clobber(tmp: Path, dest: Path) -> None:
    """Atomically rename tmp -> dest, failing if dest exists (no overwrite)."""
    if os.name == "nt":
        os.rename(tmp, dest)            # raises FileExistsError if dest exists
        return
    if tmp.is_dir():
        if dest.exists() or dest.is_symlink():
            raise FileExistsError(str(dest))
        os.rename(tmp, dest)
        return
    try:
        os.link(tmp, dest)              # fails if dest exists
        os.unlink(tmp)
    except (AttributeError, NotImplementedError, PermissionError):
        if dest.exists() or dest.is_symlink():
            raise FileExistsError(str(dest)) from None
        os.rename(tmp, dest)


def _remove_quiet(p: Path) -> None:
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    except OSError as exc:  # pragma: no cover - best effort cleanup
        log.warning("could not remove temp %s: %s", p, exc)


def _copy_verified(src: Path, dest: Path, ctx: ToolContext) -> dict[str, Any]:
    """Copy src (file or dir) to a temp sibling of dest, verify, place atomically."""
    tmp = _tmp_sibling(dest)
    try:
        if src.is_dir():
            want = _manifest(src, ctx)
            total = sum((src / k).stat().st_size for k in want)
            if total > MAX_TREE_BYTES:
                raise ValueError(f"directory is {total} bytes; cap is {MAX_TREE_BYTES}")
            tmp.mkdir()
            for rel in want:
                target = tmp / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / rel, target)
            got = {p.relative_to(tmp).as_posix(): _sha256_file(p)[0]
                   for p in tmp.rglob("*") if p.is_file()}
            if got != want:
                raise OSError("copy verification failed: directory manifest mismatch")
            _place_no_clobber(tmp, dest)
            return {"kind": "dir", "files": len(want), "bytes": total,
                    "manifest_sha256": _manifest_digest(want)}
        src_hash, size = _sha256_file(src)
        shutil.copy2(src, tmp)
        tmp_hash, _ = _sha256_file(tmp)
        if tmp_hash != src_hash:
            raise OSError("copy verification failed: SHA-256 mismatch on temp file")
        _place_no_clobber(tmp, dest)
        return {"kind": "file", "bytes": size, "sha256": src_hash}
    except BaseException:
        _remove_quiet(tmp)
        raise


def _manifest_digest(manifest: dict[str, str]) -> str:
    h = hashlib.sha256()
    for k in sorted(manifest):
        h.update(k.encode("utf-8") + b"\0" + manifest[k].encode() + b"\n")
    return h.hexdigest()


def _src_dest(ctx: ToolContext, kwargs: dict[str, Any]) -> tuple[Path, Path]:
    src = _guard(ctx, kwargs.get("source"), "source")
    dest = _guard(ctx, kwargs.get("destination"), "destination")
    if not src.exists():
        raise ValueError(f"source does not exist: {src}")
    if src.is_symlink():
        raise ValueError(f"source is a symlink; refusing to follow it: {src}")
    if dest.exists() or dest.is_symlink():
        raise ValueError(f"destination already exists (no overwrite): {dest}")
    if _is_allowed_root(ctx, src):
        raise PermissionError(f"refused: source is an allowed root itself: {src}")
    if _norm(src) == _norm(dest) or _within(dest, src):
        raise ValueError("destination cannot be the source or inside it")
    if not dest.parent.is_dir():
        raise ValueError(f"destination folder does not exist: {dest.parent} "
                         "(create it with files.mkdir)")
    return src, dest


def _err(exc: BaseException) -> ToolResult:
    if isinstance(exc, (ValueError, PermissionError, FileExistsError)):
        refused = isinstance(exc, PermissionError) and str(exc).startswith("refused")
        return ToolResult(ok=False, error=str(exc),
                          evidence={"refused": True} if refused else {})
    return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}", certainty="unknown")


def _verify_hash(path: Path, expected: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not path.exists():
        return {"verified": False, "method": "sha256-readback", "path": str(path),
                "note": "destination missing"}
    if expected.get("kind") == "dir":
        got = _manifest_digest(_manifest(path, ctx))
        return {"verified": got == expected.get("manifest_sha256"),
                "method": "manifest-sha256-readback", "path": str(path),
                "manifest_sha256": got}
    digest, size = _sha256_file(path)
    return {"verified": digest == expected.get("sha256"), "method": "sha256-readback",
            "path": str(path), "sha256": digest, "size": size}


# ---------------------------------------------------------------------------
# copy / move / mkdir
# ---------------------------------------------------------------------------

_SRC = ToolParam("source", "string", "Existing file or folder inside the allowed roots.",
                 required=True)
_DST = ToolParam("destination", "string",
                 "New path (must not exist; its parent folder must exist).", required=True)


@register(["cp", "copy_file"])
class FilesCopyTool(BaseTool):
    spec = ToolSpec(
        name="files.copy",
        summary="Copy a file or folder to a new path (never overwrites; SHA-256 verified).",
        category="files",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(_SRC, _DST, _common.DRY_RUN_PARAM),
        returns="object: {source, destination, kind, bytes, sha256|manifest_sha256}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            src, dest = _src_dest(ctx, kwargs)
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target={"source": str(src), "destination": str(dest)},
                    operation="files.copy",
                    effect=f"create {dest} as a verified copy of {src}; source unchanged",
                    reversible=True, rollback=f"files.delete path={dest}")
            info = await asyncio.to_thread(_copy_verified, src, dest, ctx)
            return ToolResult(ok=True, data={"source": str(src), "destination": str(dest),
                                             **info},
                              evidence={"method": "temp+sha256+atomic-rename"},
                              certainty="unverified")
        except Exception as exc:
            log.warning("files.copy failed: %s", exc)
            return _err(exc)

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        return await asyncio.to_thread(_verify_hash, Path(result.data["destination"]),
                                       result.data, ctx)


def _move(src: Path, dest: Path, ctx: ToolContext) -> dict[str, Any]:
    is_dir = src.is_dir()
    if is_dir:
        before = _manifest(src, ctx)
        info: dict[str, Any] = {"kind": "dir", "files": len(before),
                                "manifest_sha256": _manifest_digest(before)}
    else:
        digest, size = _sha256_file(src)
        info = {"kind": "file", "bytes": size, "sha256": digest}
    same_volume = os.stat(src).st_dev == os.stat(dest.parent).st_dev
    if same_volume:
        _place_no_clobber(src, dest)        # atomic rename: no window where data is lost
        info["method"] = "atomic-rename"
    else:
        copied = _copy_verified(src, dest, ctx)
        check = _verify_hash(dest, {**info, **copied}, ctx)
        if not check["verified"]:
            raise OSError("destination hash does not match; source kept")
        if is_dir:
            shutil.rmtree(src)
        else:
            src.unlink()
        info["method"] = "copy+sha256+delete-source"
    return info


@register(["mv", "move_file"])
class FilesMoveTool(BaseTool):
    spec = ToolSpec(
        name="files.move",
        summary=("Move/rename a file or folder (never overwrites; the source is only removed "
                 "after the destination hash matches)."),
        category="files",
        # Data is preserved at the destination and nothing is overwritten, so
        # this is a write, not a destructive act.
        side_effect=SideEffect.LOCAL_WRITE,
        params=(_SRC, _DST, _common.DRY_RUN_PARAM),
        returns="object: {source, destination, kind, method, sha256|manifest_sha256}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            src, dest = _src_dest(ctx, kwargs)
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target={"source": str(src), "destination": str(dest)},
                    operation="files.move",
                    effect=f"{src} will exist only at {dest}",
                    reversible=True,
                    rollback=f"files.move source={dest} destination={src}")
            info = await asyncio.to_thread(_move, src, dest, ctx)
            return ToolResult(ok=True, data={"source": str(src), "destination": str(dest),
                                             **info},
                              evidence={"method": info["method"]}, certainty="unverified")
        except Exception as exc:
            log.warning("files.move failed: %s", exc)
            return _err(exc)

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        out = await asyncio.to_thread(_verify_hash, Path(result.data["destination"]),
                                      result.data, ctx)
        src = Path(result.data["source"])
        source_gone = not (src.exists() or src.is_symlink())
        out["source_gone"] = source_gone
        out["verified"] = bool(out["verified"] and source_gone)
        return out


@register(["mkdir", "make_dir"])
class FilesMkdirTool(BaseTool):
    spec = ToolSpec(
        name="files.mkdir",
        summary="Create a folder (and missing parents) inside the allowed roots.",
        category="files",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("path", "string", "Folder to create.", required=True),
            ToolParam("parents", "boolean", "Create missing parent folders.", default=True),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {path, created}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            path = _guard(ctx, kwargs.get("path"), "path")
            parents = _win.bool_arg(kwargs, "parents", True)
            if path.exists() and not path.is_dir():
                return ToolResult(ok=False, error=f"a file already exists at {path}")
            if path.is_dir():
                return ToolResult(ok=True, data={"path": str(path), "created": False,
                                                 "note": "already existed"})
            if _common.dry_run_requested(kwargs):
                return _common.preview(target=str(path), operation="files.mkdir",
                                       effect=f"create folder {path}", reversible=True,
                                       rollback=f"files.delete path={path}")
            path.mkdir(parents=bool(parents), exist_ok=False)
            return ToolResult(ok=True, data={"path": str(path), "created": True},
                              certainty="unverified")
        except FileNotFoundError:
            return ToolResult(ok=False, error="parent folder missing; pass parents=true")
        except Exception as exc:
            return _err(exc)

    def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        p = Path(result.data["path"])
        return {"verified": p.is_dir(), "method": "fs-readback", "path": str(p)}


# ---------------------------------------------------------------------------
# hash
# ---------------------------------------------------------------------------

_ALGOS = ("sha256", "sha1", "md5", "sha512", "blake2b")


@register(["hash", "checksum", "sha256"])
class FilesHashTool(BaseTool):
    spec = ToolSpec(
        name="files.hash",
        summary="Compute a file's checksum (SHA-256 by default).",
        category="files",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", "File to hash.", required=True),
            ToolParam("algorithm", "string", "Hash algorithm.", default="sha256", enum=_ALGOS),
        ),
        returns="object: {path, algorithm, hexdigest, size}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            path = _guard(ctx, kwargs.get("path"), "path")
            algo = str(kwargs.get("algorithm") or "sha256").lower()
            if algo not in _ALGOS:
                return ToolResult(ok=False, error=f"algorithm must be one of {', '.join(_ALGOS)}")
            if not path.is_file():
                return ToolResult(ok=False, error=f"not a file: {path}")

            def _hash() -> tuple[str, int]:
                h = hashlib.new(algo)
                size = 0
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                        size += len(chunk)
                return h.hexdigest(), size
            digest, size = await asyncio.to_thread(_hash)
            return ToolResult(ok=True, data={"path": str(path), "algorithm": algo,
                                             "hexdigest": digest, "size": size})
        except Exception as exc:
            return _err(exc)


# ---------------------------------------------------------------------------
# zip / unzip
# ---------------------------------------------------------------------------

def _zip_build(sources: list[Path], dest: Path, ctx: ToolContext) -> dict[str, Any]:
    tmp = _tmp_sibling(dest)
    names: list[str] = []
    skipped = 0
    total = 0
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for src in sources:
                if src.is_dir():
                    files, sk, truncated = _walk_files(src, ctx)
                    skipped += sk
                    if truncated:
                        raise ValueError(f"{src} has more than {MAX_TREE_FILES} files")
                    entries = [(p, f"{src.name}/{p.relative_to(src).as_posix()}") for p in files]
                else:
                    entries = [(src, src.name)]
                for p, arc in entries:
                    if arc in names:
                        raise ValueError(f"two sources produce the same archive name {arc!r}")
                    total += p.stat().st_size
                    if total > MAX_TREE_BYTES:
                        raise ValueError(f"sources exceed {MAX_TREE_BYTES} bytes")
                    zf.write(p, arc)
                    names.append(arc)
        with zipfile.ZipFile(tmp) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise OSError(f"archive self-test failed on {bad}")
        _place_no_clobber(tmp, dest)
    except BaseException:
        _remove_quiet(tmp)
        raise
    digest, size = _sha256_file(dest)
    return {"entries": len(names), "uncompressed_bytes": total, "size": size,
            "sha256": digest, "skipped": skipped}


@register(["zip", "compress"])
class FilesZipTool(BaseTool):
    spec = ToolSpec(
        name="files.zip",
        summary="Create a .zip from files/folders inside the allowed roots (never overwrites).",
        category="files",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("sources", "array", "Files or folders to include.", required=True),
            ToolParam("destination", "string", "New .zip path (must not exist).", required=True),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {destination, entries, size, sha256, skipped}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            raw = kwargs.get("sources")
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, (list, tuple)) or not raw:
                return ToolResult(ok=False, error="sources must be a non-empty list of paths")
            sources = [_guard(ctx, s, "source") for s in raw]
            for s in sources:
                if not s.exists() or s.is_symlink():
                    return ToolResult(ok=False, error=f"source missing or a symlink: {s}")
            dest = _guard(ctx, kwargs.get("destination"), "destination")
            if dest.suffix.lower() != ".zip":
                return ToolResult(ok=False, error="destination must end in .zip")
            if dest.exists():
                return ToolResult(ok=False, error=f"destination already exists (no overwrite): {dest}")
            if not dest.parent.is_dir():
                return ToolResult(ok=False, error=f"destination folder does not exist: {dest.parent}")
            if any(_within(dest, s) for s in sources if s.is_dir()):
                return ToolResult(ok=False, error="destination cannot be inside a source folder")
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target={"sources": [str(s) for s in sources], "destination": str(dest)},
                    operation="files.zip", effect=f"create archive {dest}; sources unchanged",
                    reversible=True, rollback=f"files.delete path={dest}")
            info = await asyncio.to_thread(_zip_build, sources, dest, ctx)
            return ToolResult(ok=True, data={"destination": str(dest), **info},
                              certainty="unverified")
        except Exception as exc:
            log.warning("files.zip failed: %s", exc)
            return _err(exc)

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None

        def _check() -> dict[str, Any]:
            p = Path(result.data["destination"])
            try:
                with zipfile.ZipFile(p) as zf:
                    bad = zf.testzip()
                    count = len(zf.infolist())
            except (OSError, zipfile.BadZipFile) as exc:
                return {"verified": False, "method": "zip-reopen", "note": str(exc)}
            digest, _ = _sha256_file(p)
            return {"verified": bad is None and count == result.data["entries"]
                    and digest == result.data["sha256"],
                    "method": "zip-reopen+testzip+sha256", "entries": count, "crc_bad": bad}
        return await asyncio.to_thread(_check)


_DRIVE = re.compile(r"^[A-Za-z]:")
_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}


def check_zip_member(info: zipfile.ZipInfo, dest: Path) -> tuple[Path | None, str | None]:
    """Return (target_path, None) for a safe member or (None, reason) to refuse."""
    name = info.filename.replace("\\", "/")
    if "\x00" in name:
        return None, "NUL byte in name"
    if name.startswith("/") or _DRIVE.match(name):
        return None, "absolute path"
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        return None, "empty name"
    if any(p == ".." for p in parts):
        return None, "parent-directory traversal ('..')"
    if any(":" in p for p in parts):
        return None, "colon in name (drive or alternate data stream)"
    if any(p.split(".")[0].lower() in _WIN_RESERVED for p in parts):
        return None, "reserved device name"
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        return None, "symlink member"
    if info.flag_bits & 0x1:
        return None, "encrypted member"
    target = (dest / Path(*parts)).resolve(strict=False)
    if not _within(target, dest):
        return None, "resolves outside the destination"
    return target, None


def _unzip_plan(archive: Path, dest: Path) -> list[tuple[zipfile.ZipInfo, Path]]:
    plan: list[tuple[zipfile.ZipInfo, Path]] = []
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_UNZIP_MEMBERS:
            raise ValueError(f"archive has {len(infos)} members; cap is {MAX_UNZIP_MEMBERS}")
        total = 0
        seen: set[str] = set()
        for info in infos:
            target, why = check_zip_member(info, dest)
            if why:
                raise PermissionError(f"refused: unsafe member {info.filename!r}: {why}")
            assert target is not None
            key = _norm(target)
            if key in seen:
                raise ValueError(f"duplicate member target {info.filename!r}")
            seen.add(key)
            total += info.file_size
            if total > MAX_UNZIP_BYTES:
                raise ValueError(f"archive expands beyond {MAX_UNZIP_BYTES} bytes; refusing")
            if (not info.is_dir() and info.compress_size > 0
                    and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                    and info.file_size > 10 * 1024 * 1024):
                raise PermissionError(f"refused: member {info.filename!r} has a compression "
                                      f"ratio above {MAX_COMPRESSION_RATIO} (zip bomb guard)")
            if target.exists() and not (info.is_dir() and target.is_dir()):
                raise ValueError(f"would overwrite existing {target}; refusing")
            plan.append((info, target))
    return plan


def _crc_file(path: Path) -> int:
    crc = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def _unzip_run(archive: Path, dest: Path, plan: list[tuple[zipfile.ZipInfo, Path]]
               ) -> dict[str, Any]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive) as zf:
        for info, target in plan:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = _tmp_sibling(target)
            try:
                with zf.open(info) as src, tmp.open("wb") as out:
                    remaining = info.file_size + 1
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ValueError(f"{info.filename!r} is larger than declared")
                        out.write(chunk)
                _place_no_clobber(tmp, target)
            except BaseException:
                _remove_quiet(tmp)
                raise
            written.append({"path": str(target), "size": info.file_size, "crc": info.CRC})
    return {"files": written, "count": len(written)}


@register(["unzip", "extract"])
class FilesUnzipTool(BaseTool):
    spec = ToolSpec(
        name="files.unzip",
        summary="Extract a .zip into a folder (zip-slip safe, size-capped, never overwrites).",
        category="files",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("archive", "string", "The .zip file to extract.", required=True),
            ToolParam("destination", "string", "Folder to extract into (created if missing).",
                      required=True),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {destination, count, files:[{path,size,crc}]}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            archive = _guard(ctx, kwargs.get("archive"), "archive")
            dest = _guard(ctx, kwargs.get("destination"), "destination")
            if not archive.is_file():
                return ToolResult(ok=False, error=f"archive not found: {archive}")
            if dest.exists() and not dest.is_dir():
                return ToolResult(ok=False, error=f"destination is a file: {dest}")
            try:
                plan = await asyncio.to_thread(_unzip_plan, archive, dest)
            except zipfile.BadZipFile as exc:
                return ToolResult(ok=False, error=f"not a valid zip: {exc}")
            if _common.dry_run_requested(kwargs):
                return _common.preview(
                    target={"archive": str(archive), "destination": str(dest)},
                    operation="files.unzip",
                    effect=(f"write {sum(1 for i, _ in plan if not i.is_dir())} files "
                            f"({sum(i.file_size for i, _ in plan)} bytes) under {dest}; "
                            "nothing existing is overwritten"),
                    reversible=True,
                    rollback="files.delete each extracted path (listed in the result)",
                    members=[i.filename for i, _ in plan[:50]])
            info = await asyncio.to_thread(_unzip_run, archive, dest, plan)
            return ToolResult(ok=True, data={"destination": str(dest), **info},
                              certainty="unverified")
        except Exception as exc:
            log.warning("files.unzip failed: %s", exc)
            return _err(exc)

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None

        def _check() -> dict[str, Any]:
            bad = []
            for f in result.data.get("files", []):
                p = Path(f["path"])
                if not p.is_file() or p.stat().st_size != f["size"] or _crc_file(p) != f["crc"]:
                    bad.append(f["path"])
            return {"verified": not bad, "method": "size+crc32-readback",
                    "checked": len(result.data.get("files", [])), "mismatched": bad[:20]}
        return await asyncio.to_thread(_check)


# ---------------------------------------------------------------------------
# duplicates / disk usage
# ---------------------------------------------------------------------------

def _find_dups(root: Path, ctx: ToolContext, min_size: int, max_files: int) -> dict[str, Any]:
    files, skipped, truncated = _walk_files(root, ctx, max_files=max_files)
    by_size: dict[int, list[Path]] = {}
    for p in files:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size >= min_size:
            by_size.setdefault(size, []).append(p)
    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[str]] = {}
        for p in paths:
            try:
                by_hash.setdefault(_sha256_file(p)[0], []).append(str(p))
            except OSError:
                continue
        for digest, same in by_hash.items():
            if len(same) > 1:
                groups.append({"sha256": digest, "size": size, "paths": sorted(same),
                               "wasted_bytes": size * (len(same) - 1)})
    groups.sort(key=lambda g: g["wasted_bytes"], reverse=True)
    return {"root": str(root), "scanned": len(files), "skipped": skipped,
            "truncated": truncated, "groups": groups[:200], "group_count": len(groups),
            "wasted_bytes": sum(g["wasted_bytes"] for g in groups)}


@register(["dupes", "find_duplicates"])
class FilesDuplicatesTool(BaseTool):
    spec = ToolSpec(
        name="files.duplicates",
        summary="Find duplicate files (same size and SHA-256) under a folder.",
        category="files",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("root", "string", "Folder to scan.", required=True),
            ToolParam("min_size", "integer", "Ignore files smaller than this (bytes).", default=1),
            ToolParam("max_files", "integer", f"Scan cap (max {MAX_TREE_FILES}).",
                      default=MAX_TREE_FILES),
        ),
        returns="object: {groups:[{sha256,size,paths,wasted_bytes}], wasted_bytes, truncated}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            root = _guard(ctx, kwargs.get("root"), "root")
            if not root.is_dir():
                return ToolResult(ok=False, error=f"not a directory: {root}")
            min_size = max(0, _win.int_arg(kwargs, "min_size", 1) or 0)
            max_files = max(1, min(_win.int_arg(kwargs, "max_files", MAX_TREE_FILES)
                                   or MAX_TREE_FILES, MAX_TREE_FILES))
            data = await asyncio.to_thread(_find_dups, root, ctx, min_size, max_files)
            return ToolResult(ok=True, data=data)
        except Exception as exc:
            return _err(exc)


def _usage(path: Path, ctx: ToolContext, walk: bool, top: int) -> dict[str, Any]:
    du = shutil.disk_usage(path if path.is_dir() else path.parent)
    out: dict[str, Any] = {
        "path": str(path),
        "volume": {"total_bytes": du.total, "used_bytes": du.used, "free_bytes": du.free,
                   "percent_used": round(du.used / du.total * 100, 1) if du.total else None},
    }
    if path.is_file():
        out["size_bytes"] = path.stat().st_size
        return out
    if walk:
        files, skipped, truncated = _walk_files(path, ctx, max_files=200000)
        children: dict[str, int] = {}
        total = 0
        for p in files:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            total += size
            first = p.relative_to(path).parts[0]
            children[first] = children.get(first, 0) + size
        out.update({
            "size_bytes": total, "files": len(files), "skipped": skipped,
            "truncated": truncated,
            "largest": [{"name": k, "bytes": v} for k, v in
                        sorted(children.items(), key=lambda kv: kv[1], reverse=True)[:top]],
        })
    return out


@register(["du", "disk_usage"])
class FilesDiskUsageTool(BaseTool):
    spec = ToolSpec(
        name="files.disk_usage",
        summary="Volume free/used space and the size of a folder (largest children first).",
        category="files",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", "File or folder inside the allowed roots.", required=True),
            ToolParam("walk", "boolean", "Sum the folder's contents (can be slow).", default=True),
            ToolParam("top", "integer", "How many largest children to list.", default=10),
        ),
        returns="object: {volume, size_bytes, files, largest, truncated}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        try:
            path = _guard(ctx, kwargs.get("path"), "path")
            if not path.exists():
                return ToolResult(ok=False, error=f"no such path: {path}")
            walk = bool(_win.bool_arg(kwargs, "walk", True))
            top = max(1, min(_win.int_arg(kwargs, "top", 10) or 10, 100))
            return ToolResult(ok=True, data=await asyncio.to_thread(_usage, path, ctx, walk, top))
        except Exception as exc:
            return _err(exc)
