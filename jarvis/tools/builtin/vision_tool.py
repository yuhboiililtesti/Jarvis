"""``vision.ocr`` / ``vision.screen`` — read text from an image or the screen.

Local, CPU-only OCR using RapidOCR (ONNX). Nothing is uploaded: the engine and
its models run in-process. Both tools are read-only (`side_effect=none`).

``vision.screen`` captures in memory and never writes a file; ``vision.ocr``
reads an existing image inside the allowed roots, reusing the same file boundary
as ``files.*`` (and its protected-location refusal).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger
from . import _common
from .file_ops import _guard

log = get_logger("jarvis.tools.vision")

__all__ = ["VisionOcrTool", "VisionScreenTool", "health", "IMAGE_EXTS", "MAX_LINES"]

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")
MAX_LINES = 500
_ENGINE: Any = None


def health() -> dict[str, Any]:
    try:
        import rapidocr_onnxruntime  # noqa: F401  (optional dependency, lazy by design)
    except Exception:
        return _common.health_fail(
            "RapidOCR is not installed; install it with "
            "'pip install rapidocr_onnxruntime' (ONNX, CPU, offline models)")
    return _common.health_ok("RapidOCR (ONNX, CPU); local, no upload")


def _engine() -> Any:
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # optional dependency, lazy
        _ENGINE = RapidOCR()
    return _ENGINE


def _normalise(result: Any) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for row in result or []:
        try:
            box, text, score = row[0], str(row[1]), float(row[2])
        except (IndexError, TypeError, ValueError):
            continue
        lines.append({"text": text, "confidence": round(score, 3), "box": box})
    return lines


def _ocr(image: Any) -> dict[str, Any]:
    engine = _engine()
    result, _elapsed = engine(image)
    lines = _normalise(result)
    return {"text": "\n".join(line["text"] for line in lines), "lines": lines}


def _grab_screen(all_screens: bool):
    """Capture the screen with Pillow. Kept separate so tests can substitute it."""
    from PIL import ImageGrab
    if all_screens:
        try:
            return ImageGrab.grab(all_screens=True)
        except TypeError:  # older Pillow
            return ImageGrab.grab()
    return ImageGrab.grab()


@register(["vision.ocr", "ocr.image"])
class VisionOcrTool(BaseTool):
    spec = ToolSpec(
        name="vision.ocr",
        summary="Read text from an image file inside the allowed roots (local OCR, no upload).",
        version="1.0",
        category="vision",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("path", "string", "Image file inside the allowed roots.", required=True),
            ToolParam("max_lines", "integer", f"Maximum text lines to return (default 200, "
                                              f"max {MAX_LINES}).", default=200),
        ),
        returns="object: {path, text, lines:[{text,confidence,box}], line_count, engine}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            path = _guard(ctx, kwargs.get("path"), "path")
            if not path.is_file():
                return ToolResult(ok=False, error=f"not a file: {path}", certainty="settled")
            if path.suffix.lower() not in IMAGE_EXTS:
                return ToolResult(ok=False,
                                  error=f"unsupported image type '{path.suffix}'; "
                                        f"expected one of {', '.join(IMAGE_EXTS)}",
                                  certainty="settled")
            try:
                out = await asyncio.to_thread(_ocr, str(path))
            except ImportError as exc:
                return ToolResult(ok=False, error=health().get("reason") or str(exc),
                                  certainty="settled")
            limit = max(1, min(int(kwargs.get("max_lines") or 200), MAX_LINES))
            lines = out["lines"][:limit]
            text = "\n".join(line["text"] for line in lines)
            return ToolResult(
                ok=True,
                data={"path": str(path), "text": text, "lines": lines,
                      "line_count": len(lines), "engine": "rapidocr",
                      "duration_ms": round((time.perf_counter() - t0) * 1000.0, 1)},
                evidence={"method": "local-ocr", "lines": len(lines)},
            )
        except PermissionError as exc:
            return ToolResult(ok=False, error=str(exc), certainty="settled")
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc), certainty="settled")
        except Exception as exc:  # never raise out of run()
            log.exception("vision.ocr failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")


@register(["vision.screen", "screen.read"])
class VisionScreenTool(BaseTool):
    spec = ToolSpec(
        name="vision.screen",
        summary=("Read the text currently on screen (in-memory capture + local OCR). "
                 "Use for 'what does this error say?' / 'what's wrong here?'."),
        version="1.0",
        category="vision",
        side_effect=SideEffect.NONE,
        params=(
            ToolParam("all_screens", "boolean", "Capture every monitor when supported.",
                      default=False),
            ToolParam("max_lines", "integer", f"Maximum text lines to return (default 200, "
                                              f"max {MAX_LINES}).", default=200),
        ),
        returns="object: {text, lines:[{text,confidence,box}], width, height, engine}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            try:
                image = await asyncio.to_thread(_grab_screen, bool(kwargs.get("all_screens")))
            except ImportError as exc:
                return ToolResult(
                    ok=False,
                    error=("screen capture needs Pillow, which is not installed "
                           f"({exc}); install it with 'pip install Pillow'"),
                    certainty="settled")
            except Exception as exc:
                return ToolResult(ok=False,
                                  error=f"screen grab failed: {type(exc).__name__}: {exc}",
                                  certainty="unverified")
            try:
                import numpy as np
            except Exception as exc:  # pragma: no cover - numpy is a voice dependency
                return ToolResult(ok=False, error=f"numpy is required for OCR: {exc}",
                                  certainty="settled")
            # RapidOCR expects BGR; Pillow gives RGB.
            array = np.array(image.convert("RGB"))[:, :, ::-1]
            try:
                out = await asyncio.to_thread(_ocr, array)
            except ImportError as exc:
                return ToolResult(ok=False, error=health().get("reason") or str(exc),
                                  certainty="settled")
            limit = max(1, min(int(kwargs.get("max_lines") or 200), MAX_LINES))
            lines = out["lines"][:limit]
            text = "\n".join(line["text"] for line in lines)
            return ToolResult(
                ok=True,
                data={"text": text, "lines": lines, "line_count": len(lines),
                      "width": image.width, "height": image.height,
                      "engine": "rapidocr",
                      "duration_ms": round((time.perf_counter() - t0) * 1000.0, 1)},
                evidence={"method": "screen-ocr", "lines": len(lines)},
            )
        except Exception as exc:  # never raise out of run()
            log.exception("vision.screen failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")
