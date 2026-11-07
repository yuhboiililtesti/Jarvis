"""Document tools: ``docs.read_pdf``, ``docs.create_docx`` / ``docs.read_docx``,
``docs.create_xlsx`` / ``docs.read_xlsx``.

Optional dependencies (free/open source, imported lazily):
  pypdf (BSD-3-Clause), python-docx (MIT), openpyxl (MIT).
A missing one produces an actionable "pip install ..." error; the other tools
keep working.

File boundary: allowed roots + protected roots, shared with ``file_ops``.
Creation never overwrites, writes to a temp sibling, and places the file with
a no-clobber rename. ``verify`` reopens the produced file with the same library
and compares the content it reads back to what was requested.

Text extracted from documents is returned as data fields; it is never
interpreted. Spreadsheet strings that start with ``=`` are stored as literal
text unless ``allow_formulas=true``, so untrusted text cannot become a formula.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common, _win
from .file_ops import _err, _guard, _place_no_clobber, _remove_quiet, _tmp_sibling
from .files import _sha256_file

log = get_logger("jarvis.tools.docs")

__all__ = ["DocsReadPdfTool", "DocsCreateDocxTool", "DocsReadDocxTool", "DocsCreateXlsxTool",
           "DocsReadXlsxTool", "health"]

_DEPS = {"pdf": ("pypdf", "pypdf"), "docx": ("docx", "python-docx"),
         "xlsx": ("openpyxl", "openpyxl")}
MAX_PARAGRAPHS = 5000
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_CELLS = 200_000


def health(kind: str | None = None) -> dict[str, Any]:
    kinds = [kind] if kind else list(_DEPS)
    missing = []
    for k in kinds:
        mod, pkg = _DEPS[k]
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        return _common.health_fail(f"missing package(s): {', '.join(missing)} "
                                   f"(pip install {' '.join(missing)})")
    return _common.health_ok("document libraries importable")


def _gate(kind: str) -> ToolResult | None:
    return _common.unavailable_result(health(kind))


def _new_file(ctx: ToolContext, kwargs: dict[str, Any], ext: str) -> Path:
    path = _guard(ctx, kwargs.get("path"), "path")
    if path.suffix.lower() != ext:
        raise ValueError(f"path must end in {ext}")
    if path.exists():
        raise ValueError(f"file already exists (no overwrite): {path}")
    if not path.parent.is_dir():
        raise ValueError(f"folder does not exist: {path.parent}")
    return path


def _existing(ctx: ToolContext, kwargs: dict[str, Any], ext: str) -> Path:
    path = _guard(ctx, kwargs.get("path"), "path")
    if path.suffix.lower() != ext:
        raise ValueError(f"path must end in {ext}")
    if not path.is_file():
        raise ValueError(f"file not found: {path}")
    return path


def _save_atomic(path: Path, save) -> tuple[str, int]:
    tmp = _tmp_sibling(path)
    try:
        save(str(tmp))
        _place_no_clobber(tmp, path)
    except BaseException:
        _remove_quiet(tmp)
        raise
    return _sha256_file(path)


# ---------------------------------------------------------------- PDF ---

def _read_pdf(path: Path, max_pages: int, max_chars: int) -> dict[str, Any]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ValueError("PDF is password-protected")
        except Exception as exc:
            raise ValueError(f"PDF is encrypted and cannot be opened: {exc}") from None
    pages = []
    total = 0
    truncated = False
    for i, page in enumerate(reader.pages):
        if i >= max_pages:
            truncated = True
            break
        text = page.extract_text() or ""
        if total + len(text) > max_chars:
            text = text[: max(0, max_chars - total)]
            truncated = True
        pages.append({"page": i + 1, "text": text})
        total += len(text)
        if truncated:
            break
    meta = reader.metadata or {}
    return {"path": str(path), "page_count": len(reader.pages), "pages": pages,
            "chars": total, "truncated": truncated,
            "title": str(meta.get("/Title")) if meta.get("/Title") else None}


@register(["read_pdf", "pdf.read"])
class DocsReadPdfTool(BaseTool):
    spec = ToolSpec(
        name="docs.read_pdf",
        summary="Extract text from a PDF inside the allowed roots (pypdf).",
        category="documents",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", "PDF file.", required=True),
            ToolParam("max_pages", "integer", "Page cap (1-1000).", default=50),
            ToolParam("max_chars", "integer", "Character cap.", default=100000),
        ),
        returns="object: {page_count, pages:[{page,text}], truncated}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _gate("pdf")
        if gate:
            return gate
        try:
            path = _existing(ctx, kwargs, ".pdf")
            max_pages = max(1, min(_win.int_arg(kwargs, "max_pages", 50) or 50, 1000))
            max_chars = max(100, min(_win.int_arg(kwargs, "max_chars", 100000) or 100000,
                                     2_000_000))
            return ToolResult(ok=True, data=await asyncio.to_thread(_read_pdf, path, max_pages,
                                                                     max_chars))
        except Exception as exc:
            if type(exc).__module__.startswith("pypdf"):
                return ToolResult(ok=False, error=f"could not parse PDF: {exc}")
            return _err(exc)


# --------------------------------------------------------------- DOCX ---

def _paragraphs(kwargs: dict[str, Any]) -> list[str]:
    paras = kwargs.get("paragraphs")
    text = kwargs.get("text")
    if paras is not None and text:
        raise ValueError("give paragraphs or text, not both")
    if isinstance(paras, str):
        paras = [paras]
    if paras is None:
        if not text:
            raise ValueError("paragraphs or text is required")
        paras = [p.strip() for p in str(text).replace("\r\n", "\n").split("\n\n")]
    if not isinstance(paras, (list, tuple)):
        raise ValueError("paragraphs must be a list of strings")
    out = [str(p) for p in paras]
    if len(out) > MAX_PARAGRAPHS:
        raise ValueError(f"at most {MAX_PARAGRAPHS} paragraphs")
    if sum(len(p.encode("utf-8")) for p in out) > MAX_TEXT_BYTES:
        raise ValueError(f"text exceeds {MAX_TEXT_BYTES} bytes")
    return out


def _docx_texts(path: Path) -> tuple[list[str], list[list[list[str]]]]:
    import docx
    d = docx.Document(str(path))
    paras = [p.text for p in d.paragraphs]
    tables = [[[c.text for c in row.cells] for row in t.rows] for t in d.tables]
    return paras, tables


@register(["create_docx", "docx.create"])
class DocsCreateDocxTool(BaseTool):
    spec = ToolSpec(
        name="docs.create_docx",
        summary="Create a new Word .docx with an optional title and paragraphs.",
        category="documents",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("path", "string", "New .docx path (must not exist).", required=True),
            ToolParam("title", "string", "Optional heading at the top."),
            ToolParam("paragraphs", "array", "Paragraph strings."),
            ToolParam("text", "string", "Alternative to paragraphs: blank-line separated text."),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {path, paragraphs, sha256}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _gate("docx")
        if gate:
            return gate
        try:
            path = _new_file(ctx, kwargs, ".docx")
            paras = _paragraphs(kwargs)
            title = str(kwargs.get("title") or "").strip()
            if len(title) > 500:
                return ToolResult(ok=False, error="title is too long (max 500 chars)")
            expected = ([title] if title else []) + paras
            if _common.dry_run_requested(kwargs):
                return _common.preview(target=str(path), operation="docs.create_docx",
                                       effect=f"create {path} with {len(expected)} paragraphs",
                                       reversible=True, rollback=f"files.delete path={path}")

            def _build() -> tuple[str, int]:
                import docx
                d = docx.Document()
                if title:
                    d.add_heading(title, level=1)
                for p in paras:
                    d.add_paragraph(p)
                return _save_atomic(path, d.save)
            digest, size = await asyncio.to_thread(_build)
            return ToolResult(ok=True, data={"path": str(path), "paragraphs": expected,
                                             "sha256": digest, "size": size},
                              certainty="unverified")
        except Exception as exc:
            return _err(exc)

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        try:
            paras, _ = await asyncio.to_thread(_docx_texts, Path(result.data["path"]))
        except Exception as exc:
            return {"verified": False, "method": "docx-reopen", "note": str(exc)}
        return {"verified": paras == result.data["paragraphs"], "method": "docx-reopen",
                "paragraphs_read": len(paras)}


@register(["read_docx", "docx.read"])
class DocsReadDocxTool(BaseTool):
    spec = ToolSpec(
        name="docs.read_docx",
        summary="Read paragraphs and tables from a Word .docx.",
        category="documents",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", ".docx file.", required=True),
            ToolParam("max_chars", "integer", "Character cap.", default=100000),
        ),
        returns="object: {paragraphs, tables, truncated}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _gate("docx")
        if gate:
            return gate
        try:
            path = _existing(ctx, kwargs, ".docx")
            cap = max(100, min(_win.int_arg(kwargs, "max_chars", 100000) or 100000, 2_000_000))
            paras, tables = await asyncio.to_thread(_docx_texts, path)
            out, total, truncated = [], 0, False
            for p in paras:
                if total + len(p) > cap:
                    truncated = True
                    break
                out.append(p)
                total += len(p)
            return ToolResult(ok=True, data={"path": str(path), "paragraphs": out,
                                             "paragraph_count": len(paras),
                                             "tables": tables[:20], "truncated": truncated})
        except Exception as exc:
            if "docx" in type(exc).__module__ or "zipfile" in type(exc).__module__:
                return ToolResult(ok=False, error=f"could not parse .docx: {exc}")
            return _err(exc)


# --------------------------------------------------------------- XLSX ---

_SCALARS = (str, int, float, bool, type(None))


def _rows(kwargs: dict[str, Any]) -> list[list[Any]]:
    rows = kwargs.get("rows")
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("rows must be a non-empty list of lists")
    out, cells = [], 0
    for r in rows:
        if not isinstance(r, (list, tuple)):
            raise ValueError("each row must be a list")
        for v in r:
            if not isinstance(v, _SCALARS):
                raise ValueError("cells must be strings, numbers, booleans or null")
        cells += len(r)
        out.append(list(r))
    if cells > MAX_CELLS:
        raise ValueError(f"at most {MAX_CELLS} cells")
    return out


def _xlsx_read(path: Path, sheet: str | None, max_rows: int) -> dict[str, Any]:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        if sheet:
            if sheet not in wb.sheetnames:
                raise ValueError(f"no sheet named {sheet!r}; sheets: {wb.sheetnames}")
            ws = wb[sheet]
        else:
            ws = wb.active
        rows, truncated = [], False
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                truncated = True
                break
            rows.append([v if isinstance(v, _SCALARS) else str(v) for v in row])
        return {"path": str(path), "sheet": ws.title, "sheets": wb.sheetnames, "rows": rows,
                "truncated": truncated}
    finally:
        wb.close()


@register(["create_xlsx", "xlsx.create"])
class DocsCreateXlsxTool(BaseTool):
    spec = ToolSpec(
        name="docs.create_xlsx",
        summary="Create a new Excel .xlsx from rows of values.",
        category="documents",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("path", "string", "New .xlsx path (must not exist).", required=True),
            ToolParam("rows", "array", "List of rows; each row a list of cell values.",
                      required=True),
            ToolParam("sheet", "string", "Sheet title.", default="Sheet1"),
            ToolParam("allow_formulas", "boolean",
                      "Treat strings starting with '=' as formulas (default: literal text).",
                      default=False),
            _common.DRY_RUN_PARAM,
        ),
        returns="object: {path, sheet, rows, sha256}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _gate("xlsx")
        if gate:
            return gate
        try:
            path = _new_file(ctx, kwargs, ".xlsx")
            rows = _rows(kwargs)
            sheet = str(kwargs.get("sheet") or "Sheet1").strip()[:31] or "Sheet1"
            if any(c in sheet for c in "[]:*?/\\"):
                return ToolResult(ok=False, error="sheet title contains an invalid character")
            formulas = bool(_win.bool_arg(kwargs, "allow_formulas", False))
            if _common.dry_run_requested(kwargs):
                return _common.preview(target=str(path), operation="docs.create_xlsx",
                                       effect=f"create {path} with {len(rows)} rows in '{sheet}'",
                                       reversible=True, rollback=f"files.delete path={path}")

            def _build() -> tuple[str, int]:
                import openpyxl
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = sheet
                for r_i, row in enumerate(rows, start=1):
                    for c_i, v in enumerate(row, start=1):
                        cell = ws.cell(row=r_i, column=c_i, value=v)
                        if isinstance(v, str) and v.startswith("=") and not formulas:
                            cell.data_type = "s"
                return _save_atomic(path, wb.save)
            digest, size = await asyncio.to_thread(_build)
            return ToolResult(ok=True, data={"path": str(path), "sheet": sheet, "rows": rows,
                                             "sha256": digest, "size": size,
                                             "formulas": formulas},
                              certainty="unverified")
        except Exception as exc:
            return _err(exc)

    async def verify(self, ctx: ToolContext, result: ToolResult):
        if _common.is_preview(result):
            return _common.preview_verify()
        if not result.ok or not isinstance(result.data, dict):
            return None
        want = result.data["rows"]
        try:
            got = await asyncio.to_thread(_xlsx_read, Path(result.data["path"]),
                                          result.data["sheet"], len(want) + 1)
        except Exception as exc:
            return {"verified": False, "method": "xlsx-reopen", "note": str(exc)}
        if result.data.get("formulas"):
            # Formula cells have no cached value until Excel recalculates.
            return {"verified": len(got["rows"]) == len(want), "method": "xlsx-reopen(rowcount)"}
        width = max((len(r) for r in want), default=0)
        norm_want = [list(r) + [None] * (width - len(r)) for r in want]
        norm_got = [list(r) + [None] * (width - len(r)) for r in got["rows"]]
        return {"verified": norm_got == norm_want, "method": "xlsx-reopen",
                "rows_read": len(got["rows"])}


@register(["read_xlsx", "xlsx.read"])
class DocsReadXlsxTool(BaseTool):
    spec = ToolSpec(
        name="docs.read_xlsx",
        summary="Read cell values from an Excel .xlsx sheet (cached formula results).",
        category="documents",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", ".xlsx file.", required=True),
            ToolParam("sheet", "string", "Sheet title (default: active sheet)."),
            ToolParam("max_rows", "integer", "Row cap (1-10000).", default=500),
        ),
        returns="object: {sheet, sheets, rows, truncated}",
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        gate = _gate("xlsx")
        if gate:
            return gate
        try:
            path = _existing(ctx, kwargs, ".xlsx")
            max_rows = max(1, min(_win.int_arg(kwargs, "max_rows", 500) or 500, 10000))
            sheet = str(kwargs.get("sheet") or "").strip() or None
            return ToolResult(ok=True, data=await asyncio.to_thread(_xlsx_read, path, sheet,
                                                                     max_rows))
        except Exception as exc:
            if "openpyxl" in type(exc).__module__ or "zipfile" in type(exc).__module__:
                return ToolResult(ok=False, error=f"could not parse .xlsx: {exc}")
            return _err(exc)
