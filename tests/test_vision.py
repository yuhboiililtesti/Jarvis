"""Screen and image understanding: local OCR, no upload.

A synthetic image is generated in the test, so the OCR is exercised for real
without depending on what happens to be on the desktop. The live-screen path uses
a substituted grab, so no real screen is captured during tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jarvis.tools import REGISTRY, load_builtin_tools
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import vision_tool

load_builtin_tools()
pytest.importorskip("rapidocr_onnxruntime")


@pytest.fixture
def ws(cfg):
    root = Path(cfg.data_path) / "ws"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ctx(cfg) -> ToolContext:
    return ToolContext(config=cfg, db=None, bus=None)


def _text_image(path: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    font_path = Path(r"C:\Windows\Fonts\arial.ttf")
    if not font_path.is_file():
        pytest.skip("no TrueType font available to draw a test image")
    image = Image.new("RGB", (760, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 60), "JARVIS ONLINE", fill="black",
              font=ImageFont.truetype(str(font_path), 56))
    image.save(path)
    return path


async def test_health_reports_the_engine():
    assert vision_tool.health()["available"] is True


async def test_ocr_reads_a_known_image(cfg, ws):
    image = _text_image(ws / "ocr.png")
    result = await REGISTRY.call("vision.ocr", _ctx(cfg), path=str(image))
    assert result.ok, result.error
    assert "JARVIS" in result.data["text"].upper()
    assert result.data["line_count"] >= 1
    assert result.data["engine"] == "rapidocr"


async def test_ocr_refuses_outside_the_allowed_roots(cfg):
    outside = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "jarvis-ocr-nope.png"
    result = await REGISTRY.call("vision.ocr", _ctx(cfg), path=str(outside))
    assert not result.ok
    assert ("outside the allowed roots" in (result.error or "")
            or "protected" in (result.error or ""))


async def test_ocr_rejects_a_non_image(cfg, ws):
    junk = ws / "not-an-image.bin"
    junk.write_bytes(b"not an image")
    result = await REGISTRY.call("vision.ocr", _ctx(cfg), path=str(junk))
    assert not result.ok and "unsupported image type" in result.error


async def test_screen_ocr_uses_an_in_memory_capture(cfg, ws, monkeypatch):
    from PIL import Image
    image = Image.open(_text_image(ws / "screen.png"))
    monkeypatch.setattr(vision_tool, "_grab_screen", lambda all_screens: image)
    result = await REGISTRY.call("vision.screen", _ctx(cfg))
    assert result.ok, result.error
    assert "JARVIS" in result.data["text"].upper()
    assert result.data["width"] == 760 and result.data["height"] == 200
