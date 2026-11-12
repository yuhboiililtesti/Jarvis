"""Code navigation tools: read with line numbers, content search, symbol
index, diff and read-only git inspection.

This is the "search and read code" capability for a general-purpose operator:
every path is resolved through the same allow-list boundary as the file tools
(``_safe_path``), every tool is read-only (``SideEffect.NONE``) and all output
is explicitly capped.

Search is deliberately regex-over-text (stdlib), not a semantic index: it is
predictable, works on any language, and reports exactly what matched.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from .files import _safe_path

__all__ = ["CodeReadTool", "CodeSearchTool", "CodeSymbolsTool",
           "CodeDiffTool", "GitInfoTool"]

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
              "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
              ".tox", "target", ".next", "coverage"}
_TEXT_SNIFF_BYTES = 4096
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_RESULTS = 500
_MAX_FILES = 20_000

_SYMBOL_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    ".py": [("class", re.compile(r"^\s*class\s+([A-Za-z_]\w*)")),
            ("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"))],
    ".js": [("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
            ("function", re.compile(
                r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")),
            ("function", re.compile(
                r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("))],
    ".ts": [("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
            ("function", re.compile(
                r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")),
            ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
            ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*="))],
    ".go": [("function", re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")),
            ("type", re.compile(r"^type\s+([A-Za-z_]\w*)"))],
    ".rs": [("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)")),
            ("struct", re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_]\w*)")),
            ("enum", re.compile(r"^\s*(?:pub\s+)?enum\s+([A-Za-z_]\w*)")),
            ("trait", re.compile(r"^\s*(?:pub\s+)?trait\s+([A-Za-z_]\w*)")),
            ("impl", re.compile(r"^\s*impl(?:<[^>]*>)?\s+([A-Za-z_]\w*)"))],
    ".java": [("class", re.compile(
        r"^\s*(?:public|private|protected|abstract|final|\s)*\s*(?:class|interface|enum|record)\s+([A-Za-z_]\w*)")),
        ("method", re.compile(
            r"^\s*(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\],\s]+\s+([A-Za-z_]\w*)\s*\("))],
    ".sh": [("function", re.compile(r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{?"))],
    ".c": [("function", re.compile(
        r"^\s*(?:static\s+|inline\s+|extern\s+|const\s+)*[A-Za-z_][\w\s\*&:<>]*?"
        r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{?\s*$"))],
}


def _is_text(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(_TEXT_SNIFF_BYTES)
        return b"\x00" not in chunk
    except OSError:
        return False


def _walk_files(root: Path, include: str) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in _SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if include and not fnmatch.fnmatch(name, include):
                continue
            path = Path(dirpath) / name
            out.append(path)
            if len(out) >= _MAX_FILES:
                return out
    return out


@register(["code.read"])
class CodeReadTool(BaseTool):
    spec = ToolSpec(
        name="code.read",
        summary="Read a text file with line numbers; optional start/end line range.",
        category="code",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", "File to read. Required.", required=True),
            ToolParam("start_line", "integer", "First line (1-based).", default=1),
            ToolParam("end_line", "integer", "Last line (inclusive; 0 = EOF).", default=0),
            ToolParam("encoding", "string", "Text encoding.", default="utf-8"),
        ),
        returns="object with numbered lines, total line count and a truncation flag",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        try:
            target = _safe_path(ctx, kwargs["path"])
        except (ValueError, PermissionError) as exc:
            return ToolResult(ok=False, error=str(exc))
        if not target.is_file():
            return ToolResult(ok=False, error=f"not a file: {target}")
        try:
            start = max(1, int(kwargs.get("start_line") or 1))
            end = int(kwargs.get("end_line") or 0)
            encoding = str(kwargs.get("encoding") or "utf-8")
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="line numbers must be integers")
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return ToolResult(ok=False, error=str(exc))
        if b"\x00" in raw[:_TEXT_SNIFF_BYTES]:
            return ToolResult(ok=False, error="binary file: use files.hash or a hex tool")
        text = raw.decode(encoding, errors="replace")
        lines = text.splitlines()
        total = len(lines)
        end = total if end <= 0 else min(end, total)
        if start > total:
            return ToolResult(ok=True, data={"path": str(target), "lines": [],
                                             "total_lines": total,
                                             "start_line": start,
                                             "end_line": end})
        window = [{"n": i + 1, "text": lines[i]}
                  for i in range(start - 1, end)]
        return ToolResult(ok=True, data={
            "path": str(target), "lines": window, "total_lines": total,
            "start_line": start, "end_line": end,
            "truncated": end < total,
        }, evidence={"method": "file-read", "path": str(target)})


@register(["code.search"])
class CodeSearchTool(BaseTool):
    spec = ToolSpec(
        name="code.search",
        summary="Regex content search across a directory (file:line:match), skipping build/vendor dirs.",
        category="code",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", "Directory (or file) to search. Required.",
                      required=True),
            ToolParam("pattern", "string", "Regular expression to find. Required.",
                      required=True),
            ToolParam("include", "string", "Filename glob filter.", default="*"),
            ToolParam("case_sensitive", "boolean", "Match case.", default=False),
            ToolParam("max_results", "integer", "Maximum matches.", default=200),
            ToolParam("max_file_bytes", "integer", "Skip files larger than this.",
                      default=1048576),
        ),
        returns="object with matches (path, line, column, text) and scan statistics",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        try:
            root = _safe_path(ctx, kwargs["path"])
        except (ValueError, PermissionError) as exc:
            return ToolResult(ok=False, error=str(exc))
        try:
            flags = 0 if kwargs.get("case_sensitive") else re.IGNORECASE
            regex = re.compile(str(kwargs["pattern"]), flags)
            max_results = min(int(kwargs.get("max_results") or 200), _MAX_RESULTS)
            max_file = min(int(kwargs.get("max_file_bytes") or 1048576),
                           _MAX_FILE_BYTES)
        except (re.error, TypeError, ValueError) as exc:
            return ToolResult(ok=False, error=f"bad pattern: {exc}")
        include = str(kwargs.get("include") or "*")
        if root.is_file():
            files = [root]
        elif root.is_dir():
            files = _walk_files(root, include)
        else:
            return ToolResult(ok=False, error=f"not found: {root}")
        matches: list[dict[str, Any]] = []
        scanned = 0
        for path in files:
            if len(matches) >= max_results:
                break
            try:
                if path.stat().st_size > max_file or not _is_text(path):
                    continue
            except OSError:
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                m = regex.search(line)
                if m:
                    matches.append({"path": str(path), "line": lineno,
                                    "column": m.start() + 1,
                                    "text": line[:400]})
                    if len(matches) >= max_results:
                        break
        return ToolResult(ok=True, data={
            "root": str(root), "matches": matches,
            "match_count": len(matches), "files_scanned": scanned,
            "truncated": len(matches) >= max_results,
        }, evidence={"method": "regex-scan", "files_scanned": scanned})


@register(["code.symbols"])
class CodeSymbolsTool(BaseTool):
    spec = ToolSpec(
        name="code.symbols",
        summary="Find definitions (class/function/type) across a code tree.",
        category="code",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", "Directory to scan. Required.", required=True),
            ToolParam("include", "string", "Filename glob filter.", default="*"),
            ToolParam("max_results", "integer", "Maximum symbols.", default=300),
        ),
        returns="object with symbols (name, kind, path, line) and scan statistics",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        try:
            root = _safe_path(ctx, kwargs["path"])
        except (ValueError, PermissionError) as exc:
            return ToolResult(ok=False, error=str(exc))
        if not root.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {root}")
        try:
            max_results = min(int(kwargs.get("max_results") or 300), _MAX_RESULTS)
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="max_results must be an integer")
        include = str(kwargs.get("include") or "*")
        symbols: list[dict[str, Any]] = []
        files = _walk_files(root, include)
        scanned = 0
        for path in files:
            if len(symbols) >= max_results:
                break
            patterns = _SYMBOL_PATTERNS.get(path.suffix.lower())
            if not patterns:
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES or not _is_text(path):
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), 1):
                for kind, regex in patterns:
                    m = regex.match(line)
                    if m:
                        name = next((g for g in m.groups() if g), None)
                        if name:
                            symbols.append({"name": name, "kind": kind,
                                            "path": str(path), "line": lineno})
                            break
                if len(symbols) >= max_results:
                    break
        return ToolResult(ok=True, data={
            "root": str(root), "symbols": symbols,
            "symbol_count": len(symbols), "files_scanned": scanned,
            "truncated": len(symbols) >= max_results,
        }, evidence={"method": "symbol-scan", "files_scanned": scanned})


@register(["code.diff"])
class CodeDiffTool(BaseTool):
    spec = ToolSpec(
        name="code.diff",
        summary="Unified diff between two text files (read-only).",
        category="code",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path_a", "string", "First file. Required.", required=True),
            ToolParam("path_b", "string", "Second file. Required.", required=True),
            ToolParam("context", "integer", "Context lines.", default=3),
            ToolParam("max_bytes", "integer", "Cap on diff output.", default=200000),
        ),
        returns="object with the unified diff and a changes flag",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        try:
            a = _safe_path(ctx, kwargs["path_a"])
            b = _safe_path(ctx, kwargs["path_b"])
        except (ValueError, PermissionError) as exc:
            return ToolResult(ok=False, error=str(exc))
        for p in (a, b):
            if not p.is_file():
                return ToolResult(ok=False, error=f"not a file: {p}")
        try:
            context = max(0, min(int(kwargs.get("context") or 3), 20))
            cap = max(1024, min(int(kwargs.get("max_bytes") or 200000), 4 * 1024 * 1024))
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="context/max_bytes must be integers")
        la = a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        lb = b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        diff = list(difflib.unified_diff(la, lb, fromfile=str(a), tofile=str(b),
                                         n=context))
        text = "".join(diff)
        truncated = len(text) > cap
        return ToolResult(ok=True, data={
            "path_a": str(a), "path_b": str(b),
            "changed": bool(diff), "diff": text[:cap], "truncated": truncated,
        }, evidence={"method": "difflib", "changed": bool(diff)})


@register(["git.info"])
class GitInfoTool(BaseTool):
    spec = ToolSpec(
        name="git.info",
        summary="Read-only git inspection (status/log/diff/show/branch/remote).",
        category="code",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("repo", "string", "Repository directory. Required.", required=True),
            ToolParam("action", "string", "What to inspect.", default="status",
                      enum=("status", "log", "diff", "show", "branch", "remote")),
            ToolParam("ref", "string", "Ref for diff/show.", default=None),
            ToolParam("path", "string", "File path for diff.", default=None),
            ToolParam("count", "integer", "Log entries.", default=20),
            ToolParam("max_bytes", "integer", "Output cap.", default=200000),
        ),
        returns="object with stdout, returncode and a truncation flag",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        err = self._check_required(self.spec, kwargs)
        if err:
            return ToolResult(ok=False, error=err)
        if shutil.which("git") is None:
            return ToolResult(ok=False, error="unavailable: git is not on PATH")
        try:
            repo = _safe_path(ctx, kwargs["repo"])
        except (ValueError, PermissionError) as exc:
            return ToolResult(ok=False, error=str(exc))
        if not repo.is_dir():
            return ToolResult(ok=False, error=f"not a directory: {repo}")
        action = str(kwargs.get("action") or "status")
        if action not in ("status", "log", "diff", "show", "branch", "remote"):
            return ToolResult(ok=False, error=f"unsupported action: {action}")
        ref = kwargs.get("ref")
        rel_path = kwargs.get("path")
        try:
            count = max(1, min(int(kwargs.get("count") or 20), 200))
            cap = max(1024, min(int(kwargs.get("max_bytes") or 200000),
                                4 * 1024 * 1024))
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="count/max_bytes must be integers")
        if action == "log":
            argv = ["git", "-C", str(repo), "log", "--oneline", f"-n{count}"]
        elif action == "diff":
            argv = ["git", "-C", str(repo), "diff"]
            if ref:
                argv.append(str(ref))
            if rel_path:
                argv.extend(["--", str(rel_path)])
        elif action == "show":
            argv = ["git", "-C", str(repo), "show", "--stat", "--oneline"]
            if ref:
                argv.append(str(ref))
            else:
                argv.append("HEAD")
        else:
            argv = ["git", "-C", str(repo), action]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=60,
                                  shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ToolResult(ok=False, error=f"git failed: {exc}")
        out = proc.stdout.decode("utf-8", "replace")
        errout = proc.stderr.decode("utf-8", "replace")
        return ToolResult(ok=proc.returncode == 0, data={
            "repo": str(repo), "action": action,
            "stdout": out[:cap], "stderr": errout[:4096],
            "returncode": proc.returncode,
            "truncated": len(out) > cap,
        }, error=None if proc.returncode == 0 else f"git exited {proc.returncode}",
            evidence={"method": "git", "argv": argv[1:]})


def health() -> dict[str, Any]:
    return {"available": shutil.which("git") is not None,
            "reason": "ok" if shutil.which("git") else "git not on PATH"}
