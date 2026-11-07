"""``screen.capture`` - grab the screen to a PNG under ``data/artifacts``.

Pillow is the only supported backend. If it is not installed (or the platform
grab is unsupported) the tool returns ``ok=False`` with an actionable message;
it never fabricates a capture or a path. ``verify`` confirms the effect by
stat-ing the file that was written.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from ..base import BaseTool, SideEffect, ToolContext, ToolParam, ToolResult, ToolSpec
from ..registry import register
from ...logging_setup import get_logger

log = get_logger("jarvis.tools.screen")

__all__ = ["ScreenCaptureTool"]

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _artifacts_dir(ctx: ToolContext) -> Path:
    cfg = getattr(ctx, "config", None)
    base = getattr(cfg, "data_path", None)
    if base is None:
        return Path.cwd() / "data" / "artifacts"
    return Path(base) / "artifacts"


def _grab(all_screens: bool):
    """Import Pillow lazily and return an Image. Raises on failure."""
    from PIL import ImageGrab  # noqa: PLC0415 - optional dependency, lazy by design
    if all_screens:
        try:
            return ImageGrab.grab(all_screens=True)
        except TypeError:
            # Older Pillow without the all_screens kwarg.
            return ImageGrab.grab()
    return ImageGrab.grab()


@register(["screen.capture", "screen.screenshot"])
class ScreenCaptureTool(BaseTool):
    spec = ToolSpec(
        name="screen.capture",
        summary="Capture the screen to a PNG artefact under data/artifacts.",
        version="1.0",
        category="system",
        side_effect=SideEffect.LOCAL_WRITE,
        params=(
            ToolParam("filename", "string", "Optional base filename (no directories)."),
            ToolParam("all_screens", "boolean", "Capture every monitor when supported.",
                      default=False),
        ),
        returns="object: {path, width, height, size_bytes}",
        cheap=False,
    )

    async def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        try:
            all_screens = bool(kwargs.get("all_screens") or False)
            try:
                image = await asyncio.to_thread(_grab, all_screens)
            except ImportError as exc:
                return ToolResult(
                    ok=False,
                    error=("screen capture needs Pillow, which is not installed "
                           f"({exc}); install it with 'pip install Pillow'"),
                    certainty="settled",
                )
            except Exception as exc:
                return ToolResult(
                    ok=False,
                    error=f"screen grab failed: {type(exc).__name__}: {exc}",
                    certainty="unverified",
                )

            out_dir = _artifacts_dir(ctx)
            out_dir.mkdir(parents=True, exist_ok=True)

            raw_name = str(kwargs.get("filename") or "").strip()
            if raw_name:
                base = _SAFE_NAME.sub("_", Path(raw_name).name) or "screen"
                if not base.lower().endswith(".png"):
                    base += ".png"
            else:
                base = time.strftime("screen_%Y%m%d-%H%M%S.png")
            path = out_dir / base

            def _save() -> None:
                image.save(str(path), format="PNG")

            await asyncio.to_thread(_save)
            size = path.stat().st_size
            width, height = getattr(image, "size", (None, None))
            log.info("screen captured to %s (%dx%d, %d bytes)", path, width or 0,
                     height or 0, size)
            return ToolResult(
                ok=True,
                data={"path": str(path), "width": width, "height": height,
                      "size_bytes": size,
                      "duration_ms": round((time.perf_counter() - t0) * 1000.0, 1)},
                evidence={"method": "file", "path": str(path), "size_bytes": size},
                certainty="settled",
            )
        except Exception as exc:  # never raise out of run()
            log.exception("screen.capture failed")
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}",
                              certainty="unverified")

    def verify(self, ctx: ToolContext, result: ToolResult):
        """Confirm the PNG exists and is non-empty (real read-back)."""
        if not result.ok or not isinstance(result.data, dict):
            return None
        path = result.data.get("path")
        if not path:
            return None
        try:
            p = Path(path)
            size = p.stat().st_size
        except OSError:
            return {"verified": False, "method": "file-exists", "path": str(path)}
        if size <= 0:
            return {"verified": False, "method": "file-exists", "path": str(path),
                    "size_bytes": size}
        return {"verified": True, "method": "file-exists", "path": str(path),
                "size_bytes": size}
