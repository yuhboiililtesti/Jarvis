"""File tools constrained to an allow-list of roots.

This module is a security boundary. Every path a caller supplies is resolved
(symlinks and junctions followed) and then checked to be inside one of the
allowed roots; anything else is refused with a clear error. The default roots
are the user's home directory and `config.data_path`; extra roots may be added
by an optional `config.files.allowed_roots` list, by the
`JARVIS_FILES_ALLOWED_ROOTS` environment variable, or by the trusted
controller via ``ToolContext.services["allowed_roots"]``.

The boundary is **not** a model argument. The old ``allowed_roots`` parameter
was visible to the model, which let a prompt-injected call widen its own file
boundary (for example to read ``data/api-token`` or ``~/.ssh``). It is gone.

Credential stores and JARVIS runtime secrets are refused even when they sit
inside an allowed root (home / data): ``~/.ssh``, browser profiles, the
Windows credential stores, ``data/secrets.json``, token files, the audit anchor
and the database. The same protected-root guard is shared with ``file_ops`` and
``vision.ocr``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any, Iterable

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.files")

__all__ = [
    "FilesListTool", "FilesReadTool", "FilesWriteTool", "FilesSearchTool",
    "FilesDeleteTool", "protected_roots", "PROTECTED_DATA_NAMES",
]

# Runtime state in the data directory that must never be reachable through a
# general-purpose file tool: bearer tokens, the secret store, the audit anchor
# and the database (including its WAL/SHM sidecars).
PROTECTED_DATA_NAMES: tuple[str, ...] = (
    "secrets.json", "api-token", "node-token", "anton-token",
    "pairing-codes.json", "audit-anchor.json",
)

# files.read hashes the whole file before returning a capped prefix. That is
# wasteful (and a cheap disk-I/O DoS) for a multi-GB file, so the full hash is
# skipped above this size; the read itself is already capped by max_bytes.
MAX_HASH_BYTES = 32 * 1024 * 1024


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------

def _norm(path: Path | str) -> str:
    """Absolute, case-folded (on Windows), separators-normalised string."""
    return os.path.normcase(os.path.abspath(str(path)))


def _within(child: Path, root: Path) -> bool:
    c, r = _norm(child), _norm(root)
    return c == r or c.startswith(r.rstrip("\\/") + os.sep)


def _config_roots(config: Any) -> list[Path]:
    out: list[Path] = []
    files_cfg = getattr(config, "files", None)
    for source in (files_cfg, config):
        for attr in ("allowed_roots", "extra_roots"):
            val = getattr(source, attr, None) if source is not None else None
            if isinstance(val, (str, os.PathLike)):
                out.append(Path(val))
            elif isinstance(val, Iterable) and not isinstance(val, (bytes, dict)):
                out.extend(Path(v) for v in val if v)
    env = os.environ.get("JARVIS_FILES_ALLOWED_ROOTS", "")
    out.extend(Path(p) for p in env.split(os.pathsep) if p.strip())
    return out


def protected_roots(ctx: ToolContext) -> list[Path]:
    """Locations refused even when they are inside an allowed root.

    These are credential stores and JARVIS runtime secrets, not ordinary user
    files. Keeping the list here (instead of only in ``file_ops``) means the
    older ``files.*`` tools and ``vision.ocr`` enforce the same rule.
    """
    home = Path.home()
    roots: list[Path] = [
        home / ".ssh", home / ".gnupg", home / ".aws", home / ".azure", home / ".kube",
        home / ".password-store", home / ".docker", home / ".local" / "share" / "keyrings",
        home / ".config" / "gcloud",
    ]
    if os.name == "nt":
        sysroot = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        drive = Path(sysroot.drive + "\\") if sysroot.drive else Path("C:\\")
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roots += [
            sysroot,
            Path(os.environ.get("ProgramFiles", drive / "Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", drive / "Program Files (x86)")),
            Path(os.environ.get("ProgramData", drive / "ProgramData")),
            drive / "$Recycle.Bin", drive / "System Volume Information", drive / "Recovery",
            drive / "Boot", drive / "EFI", drive / "bootmgr", drive / "pagefile.sys",
            drive / "hiberfil.sys", drive / "swapfile.sys",
            appdata / "Microsoft" / "Credentials", local / "Microsoft" / "Credentials",
            appdata / "Microsoft" / "Protect", appdata / "Microsoft" / "Crypto",
            appdata / "Microsoft" / "SystemCertificates", local / "Microsoft" / "Vault",
            local / "Google" / "Chrome" / "User Data",
            local / "Microsoft" / "Edge" / "User Data",
            appdata / "Mozilla" / "Firefox" / "Profiles",
            local / "BraveSoftware" / "Brave-Browser" / "User Data",
        ]
    else:
        roots += [Path(p) for p in ("/etc", "/boot", "/bin", "/sbin", "/usr", "/lib",
                                    "/lib64", "/proc", "/sys", "/dev", "/var/lib", "/root",
                                    "/efi")]
        roots += [home / ".mozilla", home / ".config" / "google-chrome",
                  home / ".config" / "chromium"]
    cfg = getattr(ctx, "config", None)
    data_path = getattr(cfg, "data_path", None) if cfg is not None else None
    if data_path:
        data_path = Path(data_path)
        roots += [data_path / name for name in PROTECTED_DATA_NAMES]
        db_path = getattr(cfg, "db_path", None) if cfg is not None else None
        if db_path:
            roots += [Path(str(db_path) + suffix) for suffix in ("", "-wal", "-shm")]
    out = []
    for root in roots:
        try:
            out.append(Path(root).expanduser().resolve(strict=False))
        except OSError:
            continue
    return out


def _protected_hit(path: Path, protected: list[Path]) -> Path | None:
    for root in protected:
        if _within(path, root):
            return root
    return None


def _allowed_roots(ctx: ToolContext, extra: Iterable[Any] = ()) -> list[Path]:
    roots: list[Path] = [Path.home()]
    data_path = getattr(ctx.config, "data_path", None) if ctx.config is not None else None
    if data_path:
        roots.append(Path(data_path))
    # Controller-supplied roots only. Model-supplied kwargs are deliberately
    # ignored: a tool argument must never widen the file boundary.
    services = getattr(ctx, "services", None) or {}
    trusted = services.get("allowed_roots") if isinstance(services, dict) else None
    if isinstance(trusted, (str, os.PathLike)):
        roots.append(Path(trusted))
    elif isinstance(trusted, Iterable) and not isinstance(trusted, (bytes, dict)):
        roots.extend(Path(r) for r in trusted if r)
    roots.extend(Path(r) for r in extra if r)
    roots.extend(_config_roots(ctx.config))
    # Resolve + de-duplicate; keep order stable.
    seen: set[str] = set()
    resolved: list[Path] = []
    for root in roots:
        try:
            rr = root.expanduser().resolve(strict=False)
        except OSError:
            continue
        key = _norm(rr)
        if key not in seen:
            seen.add(key)
            resolved.append(rr)
    return resolved


def _safe_path(ctx: ToolContext, raw: Any, *, extra_roots: Iterable[Any] = ()) -> Path:
    """Resolve `raw` and guarantee it sits inside an allowed root.

    Raises `ValueError` for malformed input and `PermissionError` for a path
    outside every allowed root. Callers convert these into `ToolResult(ok=False)`.
    """
    if raw is None or not str(raw).strip():
        raise ValueError("path is required")
    text = str(raw).strip().strip('"').strip("'")
    if "\x00" in text:
        raise ValueError("path contains a NUL byte")
    path = Path(text).expanduser()
    if not path.is_absolute():
        # Relative paths are anchored at the user's home, never the process CWD.
        path = Path.home() / path
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"cannot resolve path: {exc}") from exc

    roots = _allowed_roots(ctx, extra_roots)
    inside = any(_within(resolved, root) for root in roots)
    if not inside:
        allowed = ", ".join(str(r) for r in roots) or "(none)"
        raise PermissionError(
            f"path '{raw}' resolves to '{resolved}', which is outside the allowed "
            f"roots [{allowed}]"
        )
    hit = _protected_hit(resolved, protected_roots(ctx))
    if hit is not None:
        raise PermissionError(
            f"refused: '{resolved}' is inside protected location '{hit}' "
            f"(credentials and JARVIS runtime secrets are not file-tool targets)"
        )
    return resolved


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _mtime_iso(path: Path) -> str | None:
    try:
        import datetime
        return datetime.datetime.fromtimestamp(
            path.stat().st_mtime, tz=datetime.timezone.utc).isoformat()
    except OSError:  # pragma: no cover - race
        return None


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

@register(["ls", "list_files", "files_ls"])
class FilesListTool(BaseTool):
    spec = ToolSpec(
        name="files.list",
        summary="List the entries of a directory inside the allowed roots.",
        category="files",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", "Directory to list. Required.", required=True),
            ToolParam("include_hidden", "boolean", "Include dotfiles.", default=False),
        ),
        returns="object with a list of entries (name, type, size, mtime)",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        try:
            target = _safe_path(ctx, kwargs["path"])
        except (ValueError, PermissionError) as exc:
            return ToolResult(ok=False, error=str(exc))
        try:
            if not target.is_dir():
                return ToolResult(ok=False, error=f"not a directory: {target}")
            include_hidden = bool(kwargs.get("include_hidden") or False)
            entries: list[dict[str, Any]] = []
            with os.scandir(target) as it:
                for entry in it:
                    name = entry.name
                    if not include_hidden and name.startswith("."):
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_link = entry.is_symlink()
                        st = entry.stat(follow_symlinks=False)
                        size = None if is_dir else st.st_size
                    except OSError:  # pragma: no cover - race/permission
                        is_dir, is_link, size = False, False, None
                    entries.append({
                        "name": name,
                        "path": str(target / name),
                        "type": "dir" if is_dir else ("link" if is_link else "file"),
                        "size": size,
                        "mtime": _mtime_iso(Path(entry.path)),
                    })
            entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
            return ToolResult(ok=True, data={"path": str(target), "count": len(entries),
                                             "entries": entries})
        except Exception as exc:
            log.warning("files.list failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["cat", "read_file", "files_cat"])
class FilesReadTool(BaseTool):
    spec = ToolSpec(
        name="files.read",
        summary="Read a text file inside the allowed roots (with a byte cap).",
        category="files",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", "File to read. Required.", required=True),
            ToolParam("max_bytes", "integer", "Maximum bytes to read.", default=262144),
            ToolParam("encoding", "string", "Text encoding.", default="utf-8"),
        ),
        returns="object with the decoded text, size, sha256 and a truncation flag",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        try:
            target = _safe_path(ctx, kwargs["path"])
        except (ValueError, PermissionError) as exc:
            return ToolResult(ok=False, error=str(exc))
        try:
            if not target.is_file():
                return ToolResult(ok=False, error=f"not a file: {target}")
            try:
                max_bytes = int(kwargs.get("max_bytes") or 262144)
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="max_bytes must be an integer")
            max_bytes = max(1, min(max_bytes, 8 * 1024 * 1024))
            encoding = str(kwargs.get("encoding") or "utf-8")
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            if size <= MAX_HASH_BYTES:
                digest, size = _sha256_file(target)
            else:
                digest = None
            with target.open("rb") as fh:
                raw = fh.read(max_bytes)
            truncated = size > len(raw)
            binary = b"\x00" in raw
            if binary:
                text = raw[:64].hex()
            else:
                try:
                    text = raw.decode(encoding)
                except (UnicodeDecodeError, LookupError):
                    text = raw.decode("latin-1", "replace")
                    encoding = "latin-1"
            return ToolResult(ok=True, data={
                "path": str(target), "size": size, "sha256": digest,
                "sha256_skipped": digest is None,
                "encoding": encoding, "binary": binary, "truncated": truncated,
                "text": text, "preview_hex": raw[:64].hex() if binary else None,
            })
        except Exception as exc:
            log.warning("files.read failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["write_file", "files_write", "files_put"])
class FilesWriteTool(BaseTool):
    spec = ToolSpec(
        name="files.write",
        summary="Write (or append) a text file inside the allowed roots.",
        category="files",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("path", "string", "File to write. Required.", required=True),
            ToolParam("content", "string", "Content to write. Required.", required=True),
            ToolParam("append", "boolean", "Append instead of overwrite.", default=False),
            ToolParam("create_dirs", "boolean", "Create missing parent directories.",
                      default=True),
            ToolParam("encoding", "string", "Text encoding.", default="utf-8"),
        ),
        returns="object with the final path, bytes written and sha256",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        try:
            target = _safe_path(ctx, kwargs["path"])
        except (ValueError, PermissionError) as exc:
            return ToolResult(ok=False, error=str(exc))
        try:
            if target.is_dir():
                return ToolResult(ok=False, error=f"refusing to write over a directory: {target}")
            content = str(kwargs.get("content"))
            encoding = str(kwargs.get("encoding") or "utf-8")
            append = bool(kwargs.get("append") or False)
            create_dirs = kwargs.get("create_dirs")
            create_dirs = True if create_dirs is None else bool(create_dirs)
            if create_dirs and not target.parent.is_dir():
                target.parent.mkdir(parents=True, exist_ok=True)
            payload = content.encode(encoding)
            mode = "ab" if append else "wb"
            with target.open(mode) as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            digest, size = _sha256_file(target)
            return ToolResult(ok=True, data={
                "path": str(target), "bytes_written": len(payload), "append": append,
                "size": size, "sha256": digest, "encoding": encoding,
            })
        except Exception as exc:
            log.warning("files.write failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def verify(self, ctx: ToolContext, result: ToolResult) -> dict[str, Any] | None:
        if not result.ok or not isinstance(result.data, dict):
            return {"verified": False, "method": "skipped"}
        path = Path(result.data["path"])
        if not path.is_file():
            return {"verified": False, "method": "sha256-readback",
                    "path": str(path), "note": "file missing after write"}
        digest, size = _sha256_file(path)
        expected = result.data.get("sha256")
        return {
            "verified": digest == expected,
            "method": "sha256-readback",
            "path": str(path),
            "size": size,
            "sha256": digest,
            "expected_sha256": expected,
        }


@register(["find", "glob", "files_find"])
class FilesSearchTool(BaseTool):
    spec = ToolSpec(
        name="files.search",
        summary="Find files by filename glob under an allowed root.",
        category="files",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("root", "string", "Directory to search under. Required.",
                      required=True),
            ToolParam("pattern", "string", "Filename glob, e.g. '*.md'. Required.",
                      required=True),
            ToolParam("recursive", "boolean", "Search subdirectories.", default=True),
            ToolParam("max_results", "integer", "Maximum matches.", default=100),
        ),
        returns="object with the matching paths and their sizes",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        try:
            root = _safe_path(ctx, kwargs["root"])
        except (ValueError, PermissionError) as exc:
            return ToolResult(ok=False, error=str(exc))
        try:
            if not root.is_dir():
                return ToolResult(ok=False, error=f"not a directory: {root}")
            pattern = str(kwargs["pattern"]).strip()
            if not pattern or os.sep in pattern or "/" in pattern:
                return ToolResult(ok=False,
                                  error="pattern must be a filename glob, not a path")
            try:
                max_results = int(kwargs.get("max_results") or 100)
            except (TypeError, ValueError):
                return ToolResult(ok=False, error="max_results must be an integer")
            max_results = max(1, min(max_results, 2000))
            recursive = kwargs.get("recursive")
            recursive = True if recursive is None else bool(recursive)

            matches: list[dict[str, Any]] = []
            protected = protected_roots(ctx)
            allowed = _allowed_roots(ctx)
            walker = root.rglob(pattern) if recursive else root.glob(pattern)
            for entry in walker:
                try:
                    if entry.is_dir():
                        continue
                    resolved = entry.resolve(strict=False)
                    if not any(_within(resolved, allowed_root) for allowed_root in allowed):
                        continue
                    if _protected_hit(resolved, protected) is not None:
                        continue
                    matches.append({"path": str(entry), "name": entry.name,
                                    "size": entry.stat().st_size})
                except OSError:  # pragma: no cover - race/permission
                    continue
                if len(matches) >= max_results:
                    break
            matches.sort(key=lambda m: m["name"].lower())
            return ToolResult(ok=True, data={"root": str(root), "pattern": pattern,
                                             "count": len(matches), "matches": matches})
        except Exception as exc:
            log.warning("files.search failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@register(["rm", "files_rm", "rm_file"])
class FilesDeleteTool(BaseTool):
    spec = ToolSpec(
        name="files.delete",
        summary="Delete a file (or directory with recursive=true) inside the allowed roots.",
        category="files",
        side_effect=SideEffect.DESTRUCTIVE,
        params=(
            ToolParam("path", "string", "Explicit path to delete. Required.",
                      required=True),
            ToolParam("recursive", "boolean",
                      "Allow deleting a directory and its contents.", default=False),
        ),
        returns="object confirming the path no longer exists",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        try:
            target = _safe_path(ctx, kwargs["path"])
        except (ValueError, PermissionError) as exc:
            return ToolResult(ok=False, error=str(exc))
        try:
            # Never delete an allowed root itself, even if it is empty.
            for root in _allowed_roots(ctx):
                if _norm(target) == _norm(root):
                    return ToolResult(ok=False,
                                      error=f"refusing to delete an allowed root: {target}")
            if not target.exists() and not target.is_symlink():
                return ToolResult(ok=False, error=f"no such path: {target}")
            recursive = bool(kwargs.get("recursive") or False)
            if target.is_dir() and not target.is_symlink():
                if not recursive:
                    return ToolResult(
                        ok=False,
                        error=(f"'{target}' is a directory; pass recursive=true to delete "
                               f"it and its contents"),
                    )
                shutil.rmtree(target)
            else:
                target.unlink()
            return ToolResult(ok=True, data={"path": str(target),
                                             "recursive": recursive,
                                             "deleted": True})
        except Exception as exc:
            log.warning("files.delete failed: %s", exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def verify(self, ctx: ToolContext, result: ToolResult) -> dict[str, Any] | None:
        if not result.ok or not isinstance(result.data, dict):
            return {"verified": False, "method": "skipped"}
        path = Path(result.data["path"])
        still_there = path.exists() or path.is_symlink()
        return {"verified": not still_there, "method": "fs-readback",
                "path": str(path), "exists": still_there}
