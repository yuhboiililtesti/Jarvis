"""Sentence splitter (batch + streaming) and speech cleanup oracles."""

from __future__ import annotations

import pytest

from jarvis.voice.text import MAX_CHARS, SentenceStream, clean_for_speech, split_sentences


@pytest.mark.parametrize("text, expected", [
    ("Good evening, sir. All systems are online.",
     ["Good evening, sir.", "All systems are online."]),
    ("Mr. Stark is here. He is early.", ["Mr. Stark is here.", "He is early."]),
    ("Dr. Banner and Mrs. Potts arrived. Shall I admit them?",
     ["Dr. Banner and Mrs. Potts arrived.", "Shall I admit them?"]),
    ("Use a compiled language, e.g. Rust. Then benchmark it.",
     ["Use a compiled language, e.g. Rust.", "Then benchmark it."]),
    ("That is, i.e. the second one. Understood.",
     ["That is, i.e. the second one.", "Understood."]),
    ("Pi is roughly 3.14 today. Version 2.5.1 is out.",
     ["Pi is roughly 3.14 today.", "Version 2.5.1 is out."]),
    ("Well... I suppose so. Fine.", ["Well... I suppose so.", "Fine."]),
    ("Wait... Then go.", ["Wait...", "Then go."]),
    ("Really?! That is odd.", ["Really?!", "That is odd."]),
    ('He asked "why?" Then he left.', ['He asked "why?"', "Then he left."]),
    ("J. R. R. Tolkien wrote it. Indeed.", ["J. R. R. Tolkien wrote it.", "Indeed."]),
    ("Bring tools, cables, etc. and snacks. Then leave.",
     ["Bring tools, cables, etc. and snacks.", "Then leave."]),
    ("We met at 10 a.m. It went well.", ["We met at 10 a.m.", "It went well."]),
    ("No terminator at the end", ["No terminator at the end"]),
    ("First paragraph without a stop\n\nSecond one.",
     ["First paragraph without a stop", "Second one."]),
    ("", []),
    ("   ", []),
])
def test_split_sentences(text, expected):
    assert split_sentences(text) == expected


def _stream(text: str, step: int) -> list[str]:
    s = SentenceStream()
    out: list[str] = []
    for i in range(0, len(text), step):
        out.extend(s.feed(text[i:i + step]))
    out.extend(s.flush())
    return out


@pytest.mark.parametrize("text", [
    "Good evening, sir. Mr. Stark called at 3.30 p.m. He said... nothing much. Shall I ring back?",
    "Use e.g. the second server. It is 2.5 times faster! Really? Yes.",
    "One. Two. Three.",
])
@pytest.mark.parametrize("step", [1, 3, 7, 1000])
def test_streaming_matches_batch(text, step):
    assert _stream(text, step) == split_sentences(text)


def test_stream_emits_first_sentence_before_reply_finishes():
    s = SentenceStream()
    assert s.feed("Good evening, sir") == []
    assert s.feed(". All sys") == ["Good evening, sir."]
    assert s.pending.strip() == "All sys"
    assert s.feed("tems online.") == []          # needs lookahead to confirm
    assert s.flush() == ["All systems online."]


def test_stream_does_not_split_on_pending_abbreviation():
    s = SentenceStream()
    assert s.feed("Ask Mr.") == []
    assert s.feed(" Stark") == []
    assert s.flush() == ["Ask Mr. Stark"]


def test_run_on_sentence_is_cut_at_a_clause():
    text = ("This is a deliberately long clause that keeps going, " * 8).strip()
    parts = split_sentences(text)
    assert len(parts) >= 2
    assert all(len(p) <= MAX_CHARS for p in parts)
    assert " ".join(parts).replace("  ", " ") == text


def test_clean_for_speech_strips_markdown_code_and_urls():
    md = ("## Status\n\n**All good**, sir. See [the docs](https://example.com/a?b=1) "
          "or https://www.python.org/downloads/.\n\n- first item\n- second item\n\n"
          "```python\nprint('hi')\n```\nRun `jarvis serve` now.")
    out = clean_for_speech(md)
    assert "**" not in out and "#" not in out and "`" not in out
    assert "http" not in out and "(" not in out
    assert "the docs" in out
    assert "a link to python.org" in out
    assert "print(" not in out and "I've put the code on screen." in out
    assert "jarvis serve" in out
    sentences = split_sentences(out)
    assert "First item." in sentences or "first item." in sentences
    assert "Status." in sentences


def test_clean_for_speech_plain_text_is_unchanged_apart_from_whitespace():
    assert clean_for_speech("Good evening, sir.  All   systems online.") == \
        "Good evening, sir. All systems online."
    assert clean_for_speech("") == ""


# ---------------------------------------------------------------------------
# The HUD carries a JavaScript port (jarvis/ui/speech.js). Keep them in step.
# ---------------------------------------------------------------------------

_PARITY_TEXTS = [
    "Good evening, sir. Mr. Stark called at 3.30 p.m. He said... nothing much. Shall I ring back?",
    "Use e.g. the second server. It is 2.5 times faster! Really? Yes.",
    "Well... I suppose so. Wait... Then go.",
    'He asked "why?" Then he left. J. R. R. Tolkien wrote it.',
    "Bring tools, cables, etc. and snacks. We met at 10 a.m. It went well.",
    "First paragraph without a stop\n\nSecond one.",
    ("A very long clause that keeps going and going, " * 7).strip(),
]


def test_js_port_matches_python():
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed; JS parity not checked")
    speech = (Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "speech.js").as_uri()
    script = (
        f"import {{ splitSentences, SentenceStream, ReplyChunker }} from {json.dumps(speech)};\n"
        "const texts = JSON.parse(process.argv[1]);\n"
        "const out = texts.map((t) => {\n"
        "  const s = new SentenceStream(); const streamed = [];\n"
        "  for (const ch of t) streamed.push(...s.feed(ch));\n"
        "  streamed.push(...s.flush());\n"
        "  const c = new ReplyChunker(); const spoken = [];\n"
        "  for (const ch of t) spoken.push(...c.push(ch));\n"
        "  spoken.push(...c.finish(t));\n"
        "  return { batch: splitSentences(t), streamed, spoken };\n"
        "});\n"
        "console.log(JSON.stringify(out));\n"
    )
    proc = subprocess.run([node, "--input-type=module", "-e", script, json.dumps(_PARITY_TEXTS)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    results = json.loads(proc.stdout)
    for text, res in zip(_PARITY_TEXTS, results):
        expected = split_sentences(text)
        assert res["batch"] == expected, text
        assert res["streamed"] == expected, text
        assert res["spoken"] == expected, text


def test_js_chunker_skips_code_fences():
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    speech = (Path(__file__).resolve().parents[1] / "jarvis" / "ui" / "speech.js").as_uri()
    reply = "Here you are, sir.\n\n```python\nprint('a. b. c.')\n```\n\n- run it\n- then rest. Done."
    script = (
        f"import {{ ReplyChunker }} from {json.dumps(speech)};\n"
        "const t = JSON.parse(process.argv[1]); const c = new ReplyChunker(); const out = [];\n"
        "for (let i = 0; i < t.length; i += 3) out.push(...c.push(t.slice(i, i + 3)));\n"
        "out.push(...c.finish(t)); console.log(JSON.stringify(out));\n"
    )
    proc = subprocess.run([node, "--input-type=module", "-e", script, json.dumps(reply)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["Here you are, sir.", "I've put the code on screen.",
                                       "Run it", "Then rest.", "Done."]
