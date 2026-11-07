"""Text extraction and provenance-preserving chunking.

Every chunk records the 1-based inclusive line range it came from (and the PDF
page, when relevant) so a search hit can always be traced back to an exact place
in the owner's file. Markdown is split on ATX headings (ignoring headings inside
fenced code), and each chunk carries its heading path ("Title > Section > Sub").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Chunk", "chunk_markdown", "chunk_plain", "decode_text", "extract_pdf_pages",
           "chunk_file_text"]

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")
MARKDOWN_EXT = {".md", ".markdown", ".mdx"}


@dataclass
class Chunk:
    text: str
    line_start: int
    line_end: int
    heading: str | None = None
    page: int | None = None


def decode_text(data: bytes) -> str | None:
    """Decode bytes as text; None for binary content."""
    if b"\x00" in data[:8192]:
        return None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _pack(lines: list[tuple[int, str]], heading: str | None, max_chars: int,
          page: int | None = None) -> list[Chunk]:
    """Greedy-pack (line_no, text) pairs into chunks, preferring blank-line breaks."""
    out: list[Chunk] = []
    buf: list[tuple[int, str]] = []
    size = 0

    def flush(upto: int | None = None) -> None:
        nonlocal buf, size
        part = buf if upto is None else buf[:upto]
        rest = [] if upto is None else buf[upto:]
        text = "\n".join(t for _, t in part).strip()
        if text:
            out.append(Chunk(text, part[0][0], part[-1][0], heading, page))
        buf = rest
        size = sum(len(t) + 1 for _, t in buf)

    for ln, text in lines:
        # a single over-long line is split by characters but keeps its line number
        while len(text) > max_chars:
            if buf:
                flush()
            out.append(Chunk(text[:max_chars], ln, ln, heading, page))
            text = text[max_chars:]
        if size + len(text) + 1 > max_chars and buf:
            # break at the last blank line in the buffer when it is not too early
            cut = None
            for i in range(len(buf) - 1, 0, -1):
                if not buf[i][1].strip():
                    cut = i
                    break
            if cut is not None and sum(len(t) + 1 for _, t in buf[:cut]) >= max_chars // 3:
                flush(cut)
            else:
                flush()
        buf.append((ln, text))
        size += len(text) + 1
    if buf:
        flush()
    return out


def chunk_markdown(text: str, max_chars: int = 1200) -> list[Chunk]:
    lines = text.splitlines()
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str | None, list[tuple[int, str]]]] = [(None, [])]
    in_fence = False
    fence_tok = ""
    for i, line in enumerate(lines, start=1):
        m_fence = _FENCE.match(line)
        if m_fence:
            tok = m_fence.group(1)
            if not in_fence:
                in_fence, fence_tok = True, tok
            elif tok == fence_tok:
                in_fence = False
        m = None if in_fence else _HEADING.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            sections.append((" > ".join(t for _, t in stack), [(i, line)]))
        else:
            sections[-1][1].append((i, line))
    out: list[Chunk] = []
    for heading, body in sections:
        if any(t.strip() for _, t in body):
            out.extend(_pack(body, heading, max_chars))
    return out


def chunk_plain(text: str, max_chars: int = 1200, *, page: int | None = None,
                heading: str | None = None) -> list[Chunk]:
    lines = list(enumerate(text.splitlines(), start=1))
    return _pack(lines, heading, max_chars, page)


def extract_pdf_pages(data: bytes) -> list[str] | None:
    """Per-page text via pypdf, or None when pypdf is not installed."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    import io
    reader = PdfReader(io.BytesIO(data))
    return [(page.extract_text() or "") for page in reader.pages]


def chunk_file_text(path: Path, text: str, max_chars: int) -> list[Chunk]:
    if path.suffix.lower() in MARKDOWN_EXT:
        return chunk_markdown(text, max_chars)
    return chunk_plain(text, max_chars)
