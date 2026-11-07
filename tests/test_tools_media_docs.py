"""media.probe / media.convert (ffmpeg) and docs.* (pypdf, python-docx, openpyxl).

ffmpeg tests run only when ffmpeg/ffprobe are on PATH and operate on a 0.5 s
synthetic tone inside the test's temp data dir. Docs tests skip per missing
package. Both create files only under tmp dirs.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from jarvis.tools import REGISTRY
from jarvis.tools.base import SideEffect, ToolContext
from jarvis.tools.builtin import docs_tool, ffmpeg_tool

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture
def ctx(cfg):
    return ToolContext(config=cfg, db=None, bus=None)


@pytest.fixture
def ws(cfg):
    root = Path(cfg.data_path) / "ws"
    root.mkdir(parents=True)
    return root


def test_specs():
    expect = {"media.probe": SideEffect.NONE, "media.convert": SideEffect.LOCAL_WRITE,
              "docs.read_pdf": SideEffect.NONE, "docs.create_docx": SideEffect.LOCAL_WRITE,
              "docs.read_docx": SideEffect.NONE, "docs.create_xlsx": SideEffect.LOCAL_WRITE,
              "docs.read_xlsx": SideEffect.NONE}
    for name, side in expect.items():
        spec = REGISTRY.get(name).spec
        assert spec.side_effect == side
        assert ("dry_run" in [p.name for p in spec.params]) == (side != SideEffect.NONE)


# ---------------------------------------------------------------- ffmpeg ---

def test_convert_argv_is_fixed_and_uses_file_protocol():
    argv = ffmpeg_tool.build_convert_argv(
        Path("/in/a.wav"), Path("/out/b.mp3"),
        {"video_codec": "none", "audio_codec": "libmp3lame", "audio_bitrate": "128k",
         "max_duration_s": 2.5, "crf": None})
    assert argv[:2] == ["ffmpeg", "-hide_banner"] and "-n" in argv
    assert argv[argv.index("-i") + 1].startswith("file:")
    assert argv[-1].startswith("file:")
    assert ["-t", "2.5"] == argv[argv.index("-t"):argv.index("-t") + 2]
    assert "-vn" in argv and ["-c:a", "libmp3lame"] == argv[argv.index("-c:a"):argv.index("-c:a") + 2]


async def test_convert_validation(ctx, ws, monkeypatch):
    monkeypatch.setattr(ffmpeg_tool, "health", lambda: {"available": True, "reason": "t"})
    src = ws / "a.wav"
    src.write_bytes(b"RIFF")
    tool = REGISTRY.get("media.convert")
    cases = [
        {"input": str(src), "output": str(ws / "b.exe")},
        {"input": str(src), "output": str(ws / "b.mp3"), "audio_bitrate": "128k; rm"},
        {"input": str(src), "output": str(ws / "b.mp3"), "crf": 99},
        {"input": str(src), "output": str(ws / "b.mp3"), "video_codec": "none",
         "audio_codec": "none"},
        {"input": str(src), "output": str(ws / "b.mp3"), "video_codec": "evil"},
        {"input": str(ws / "missing.wav"), "output": str(ws / "b.mp3")},
        {"input": str(src), "output": str(src)},
    ]
    for kw in cases:
        r = await tool.run(ctx, **kw)
        assert not r.ok, kw


async def test_ffmpeg_unavailable_message(ctx, monkeypatch):
    monkeypatch.setattr(ffmpeg_tool.shutil, "which", lambda _x: None)
    r = await REGISTRY.call("media.probe", ctx, path="x.wav")
    assert not r.ok and "FFmpeg" in r.error


@pytest.fixture
def tone(ws):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    out = ws / "tone.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=0.5", str(out)], check=True, timeout=60)
    return out


async def test_probe_real_file(ctx, tone):
    r = await REGISTRY.call("media.probe", ctx, path=str(tone))
    assert r.ok and r.data["format"] == "wav"
    assert abs(r.data["duration_s"] - 0.5) < 0.05
    assert r.data["streams"][0]["codec_type"] == "audio"


async def test_convert_real_file_and_verify(ctx, tone, ws):
    tool = REGISTRY.get("media.convert")
    out = ws / "tone.flac"
    r = await tool.run(ctx, input=str(tone), output=str(out), audio_codec="flac")
    assert r.ok, r.error
    assert out.is_file() and r.data["probe"]["streams"][0]["codec_name"] == "flac"
    assert not [p for p in ws.iterdir() if p.name.startswith(".jarvis-tmp-")]
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["streams"] == 1
    r = await tool.run(ctx, input=str(tone), output=str(out))
    assert not r.ok and "no overwrite" in r.error
    r = await tool.run(ctx, input=str(tone), output=str(ws / "d.mp3"), dry_run=True)
    assert r.ok and r.data["dry_run"] and not (ws / "d.mp3").exists()


async def test_convert_failure_cleans_temp(ctx, ws):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    junk = ws / "junk.wav"
    junk.write_bytes(b"this is not audio")
    r = await REGISTRY.get("media.convert").run(ctx, input=str(junk), output=str(ws / "o.mp3"))
    assert not r.ok and "ffmpeg failed" in r.error
    assert not (ws / "o.mp3").exists()
    assert not [p for p in ws.iterdir() if p.name.startswith(".jarvis-tmp-")]


# ------------------------------------------------------------------ docs ---

def _minimal_pdf(path: Path, text: str) -> None:
    """Hand-built one-page PDF with a Helvetica text run (independent of pypdf)."""
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


async def test_read_pdf(ctx, ws):
    pytest.importorskip("pypdf")
    pdf = ws / "doc.pdf"
    _minimal_pdf(pdf, "Hello JARVIS")
    r = await REGISTRY.call("docs.read_pdf", ctx, path=str(pdf))
    assert r.ok, r.error
    assert r.data["page_count"] == 1 and "Hello JARVIS" in r.data["pages"][0]["text"]
    bad = ws / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")
    r = await REGISTRY.call("docs.read_pdf", ctx, path=str(bad))
    assert not r.ok
    r = await REGISTRY.call("docs.read_pdf", ctx, path=str(ws / "x.txt"))
    assert not r.ok and ".pdf" in r.error


async def test_docx_create_read_verify(ctx, ws):
    pytest.importorskip("docx")
    tool = REGISTRY.get("docs.create_docx")
    path = ws / "memo.docx"
    r = await tool.run(ctx, path=str(path), title="Memo", paragraphs=["First.", "Second."])
    assert r.ok, r.error
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["paragraphs_read"] == 3
    with zipfile.ZipFile(path) as zf:                      # independent oracle: raw XML
        assert "Second." in zf.read("word/document.xml").decode("utf-8")
    r2 = await REGISTRY.call("docs.read_docx", ctx, path=str(path))
    assert r2.ok and r2.data["paragraphs"] == ["Memo", "First.", "Second."]
    r = await tool.run(ctx, path=str(path), text="a")
    assert not r.ok and "no overwrite" in r.error
    r = await tool.run(ctx, path=str(ws / "t.docx"), text="p1\n\np2")
    assert r.ok and r.data["paragraphs"] == ["p1", "p2"]
    r = await tool.run(ctx, path=str(ws / "n.docx"))
    assert not r.ok
    r = await tool.run(ctx, path=str(ws / "dry.docx"), text="x", dry_run=True)
    assert r.ok and r.data["dry_run"] and not (ws / "dry.docx").exists()


async def test_docx_verify_detects_tamper(ctx, ws):
    docx = pytest.importorskip("docx")
    tool = REGISTRY.get("docs.create_docx")
    path = ws / "t.docx"
    r = await tool.run(ctx, path=str(path), paragraphs=["original"])
    d = docx.Document(str(path))
    d.paragraphs[0].text = "changed"
    d.save(str(path))
    assert (await tool.verify(ctx, r))["verified"] is False


async def test_xlsx_create_read_verify_and_formula_safety(ctx, ws):
    openpyxl = pytest.importorskip("openpyxl")
    tool = REGISTRY.get("docs.create_xlsx")
    path = ws / "t.xlsx"
    rows = [["name", "qty", "ok"], ["apple", 3, True], ["=HYPERLINK(\"http://x\")", 1.5, None]]
    r = await tool.run(ctx, path=str(path), rows=rows, sheet="Stock")
    assert r.ok, r.error
    v = await tool.verify(ctx, r)
    assert v["verified"] is True and v["rows_read"] == 3
    wb = openpyxl.load_workbook(str(path))                 # independent oracle
    cell = wb["Stock"]["A3"]
    assert cell.data_type == "s" and cell.value.startswith("=HYPERLINK")
    r2 = await REGISTRY.call("docs.read_xlsx", ctx, path=str(path))
    assert r2.ok and r2.data["sheet"] == "Stock" and r2.data["rows"][1] == ["apple", 3, True]
    for bad in ({"rows": []}, {"rows": [1, 2]}, {"rows": [[{"a": 1}]]},
                {"rows": [["a"]], "sheet": "bad/name"}):
        r = await tool.run(ctx, path=str(ws / "bad.xlsx"), **bad)
        assert not r.ok, bad
    r = await REGISTRY.call("docs.read_xlsx", ctx, path=str(path), sheet="Nope")
    assert not r.ok and "no sheet" in r.error


async def test_docs_missing_dependency_message(ctx, ws, monkeypatch):
    import importlib as _il
    real = _il.import_module

    def fake(name, *a, **k):
        if name == "openpyxl":
            raise ImportError("no openpyxl")
        return real(name, *a, **k)
    monkeypatch.setattr(docs_tool.importlib, "import_module", fake)
    r = await REGISTRY.call("docs.read_xlsx", ctx, path=str(ws / "a.xlsx"))
    assert not r.ok and "pip install openpyxl" in r.error


async def test_docs_respect_protected_roots(ctx, cfg):
    pytest.importorskip("pypdf")
    r = await REGISTRY.call("docs.read_pdf", ctx,
                            path=str(Path.home() / ".ssh" / "keys.pdf"))
    assert not r.ok and "protected" in r.error
