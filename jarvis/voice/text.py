"""Text helpers for speech: sentence splitting (batch + streaming) and cleanup.

* :func:`split_sentences` splits a finished reply into speakable sentences.
* :class:`SentenceStream` does the same incrementally over streamed tokens, so
  the first sentence can be synthesised while the model is still writing.
* :func:`clean_for_speech` strips markdown, code and URLs that should never be
  read aloud.

The splitter is rule-based and conservative: it does not split after common
abbreviations ("Mr.", "Dr.", "e.g.", "i.e.", "etc." before a lowercase word),
single initials ("J. R. R."), inside decimals ("3.14"), or on an ellipsis that
continues in lowercase. The HUD carries a JavaScript port of the same rules
(``jarvis/ui/speech.js``); keep the two in step.
"""

from __future__ import annotations

import re

__all__ = ["split_sentences", "SentenceStream", "clean_for_speech", "ABBREVIATIONS"]

# Abbreviations that are (almost) never the end of a sentence.
ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "mx", "dr", "prof", "sr", "jr", "st", "rev", "hon", "gen",
    "col", "capt", "lt", "sgt", "cmdr", "gov", "pres", "sen", "rep",
    "e.g", "i.e", "cf", "vs", "approx", "dept", "est", "fig", "no", "nos", "vol",
    "ch", "pp", "inc", "ltd", "co", "corp", "mt", "ft", "ave", "rd", "blvd",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "u.s", "u.k", "a.m", "p.m", "ph.d", "b.sc", "m.sc",
})
# Abbreviations that may legitimately end a sentence; split only if the next
# word starts with an uppercase letter.
_SOFT_ABBREVIATIONS = frozenset({"etc", "al", "a.m", "p.m", "u.s", "u.k", "inc", "ltd", "co"})

_TERMINATORS = ".!?…"
_CLOSERS = "\"')]}”’"
# Hard cap: a run-on without a terminator is cut at a clause boundary so the
# first audio is never held hostage by a very long sentence.
MAX_CHARS = 240


def _word_before(text: str, end: int) -> str:
    """The token ending at ``end`` (exclusive), lowercased, without the final dot."""
    start = end
    while start > 0 and not text[start - 1].isspace() and text[start - 1] not in "([{\"'":
        start -= 1
    return text[start:end].lower().rstrip(".")


def _is_boundary(text: str, i: int) -> bool | None:
    """Decide if the terminator run ending at ``i`` ends a sentence.

    Returns True/False, or None when more text is needed to decide.
    ``text[i]`` is the last terminator/closer character of the run.
    """
    j = i + 1
    n = len(text)
    if j >= n:
        return None                       # need lookahead
    if not text[j].isspace():
        return False                      # "3.14", "e.g.x", "end.Next" (rare) -> no
    k = j
    while k < n and text[k].isspace():
        if text[k] == "\n" and k + 1 < n and text[k + 1] == "\n":
            return True                   # paragraph break always ends it
        k += 1
    if k >= n:
        return None                       # only whitespace so far
    nxt = text[k]

    # Find the terminator run (skip closing quotes/brackets backwards).
    p = i
    while p >= 0 and text[p] in _CLOSERS:
        p -= 1
    if p < 0:
        return False
    term = text[p]
    run_start = p
    while run_start > 0 and text[run_start - 1] in _TERMINATORS:
        run_start -= 1
    run = text[run_start:p + 1]

    if term in "!?":
        return True
    if run.endswith("...") or term == "…":
        # Ellipsis: trailing off mid-thought continues in lowercase, or with the
        # pronoun "I" ("Well... I suppose so.").
        if nxt == "I" and (k + 1 >= n or not text[k + 1].isalpha()):
            return False
        return nxt.isupper() or nxt.isdigit() or nxt in "\"'“"

    word = _word_before(text, run_start + 1)
    bare = word.strip("\"'([{")
    if bare in _SOFT_ABBREVIATIONS:
        return nxt.isupper()
    if bare in ABBREVIATIONS:
        return False
    if len(bare) == 1 and bare.isalpha():
        return False                      # initials: "J. R. R. Tolkien"
    if nxt.islower():
        return False                      # "approx. five" style continuations
    return True


def _hard_cut(buf: str) -> int:
    """Index after which to cut an over-long buffer (at a clause boundary)."""
    window = buf[:MAX_CHARS]
    for mark in ("; ", ": ", ", ", " - ", " "):
        idx = window.rfind(mark)
        if idx > MAX_CHARS // 3:
            return idx + len(mark.rstrip()) if mark.strip() else idx
    return MAX_CHARS


def _scan(buf: str, final: bool) -> tuple[list[str], str]:
    out: list[str] = []
    start = 0
    i = 0
    n = len(buf)
    while i < n:
        ch = buf[i]
        if ch in _TERMINATORS:
            # extend over the terminator run and any closers
            e = i
            while e + 1 < n and buf[e + 1] in _TERMINATORS:
                e += 1
            while e + 1 < n and buf[e + 1] in _CLOSERS:
                e += 1
            decision = _is_boundary(buf, e)
            if decision is None:
                if final:
                    decision = True
                else:
                    break                 # wait for more text
            if decision:
                piece = buf[start:e + 1].strip()
                if piece:
                    out.append(piece)
                start = e + 1
            i = e + 1
            continue
        if ch == "\n" and i + 1 < n and buf[i + 1] == "\n":
            piece = buf[start:i].strip()
            if piece:
                out.append(piece)
            start = i + 2
            i += 2
            continue
        i += 1
    rest = buf[start:]
    # Over-long run-on with no terminator: cut at a clause boundary.
    while len(rest.strip()) > MAX_CHARS:
        stripped = rest.lstrip()
        cut = _hard_cut(stripped)
        out.append(stripped[:cut].strip())
        rest = stripped[cut:]
    if final:
        tail = rest.strip()
        if tail:
            out.append(tail)
        rest = ""
    return out, rest


def split_sentences(text: str) -> list[str]:
    """Split finished ``text`` into sentences (whitespace-trimmed, non-empty)."""
    sentences, _ = _scan(text or "", final=True)
    return sentences


class SentenceStream:
    """Incremental splitter: feed streamed text, receive completed sentences.

    >>> s = SentenceStream()
    >>> s.feed("Good evening, sir. All sys")
    ['Good evening, sir.']
    >>> s.feed("tems online.")
    []
    >>> s.flush()
    ['All systems online.']
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> list[str]:
        self._buf += chunk or ""
        out, self._buf = _scan(self._buf, final=False)
        return out

    def flush(self) -> list[str]:
        out, self._buf = _scan(self._buf, final=True)
        return out

    @property
    def pending(self) -> str:
        return self._buf


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

_CODE_BLOCK = re.compile(r"```.*?(```|$)", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)(]|\([^)]*\))*\)")
_URL = re.compile(r"\b(?:https?://|www\.)([^\s/<>()]+)[^\s<>()]*", re.I)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_QUOTE = re.compile(r"^\s*>\s?", re.M)
_EMPH = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}.*$", re.M)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
# Emoji / pictographs / dingbats: not speakable.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def clean_for_speech(text: str) -> str:
    """Turn assistant markdown into plain, speakable prose."""
    if not text:
        return ""
    t = _CODE_BLOCK.sub(" I've put the code on screen. ", text)
    t = _IMAGE.sub(lambda m: m.group(1), t)
    t = _LINK.sub(lambda m: m.group(1), t)
    t = _URL.sub(lambda m: "a link to " + m.group(1).removeprefix("www.").rstrip(".,;:"), t)
    t = _INLINE_CODE.sub(lambda m: m.group(1), t)
    t = _TABLE_RULE.sub("", t)
    t = _HTML_TAG.sub("", t)
    t = _HEADING.sub("", t)
    t = _QUOTE.sub("", t)
    # A bullet line becomes its own sentence so items are not run together.
    t = _BULLET.sub("\n\n", t)
    for _ in range(2):                    # nested **_x_**
        t = _EMPH.sub(lambda m: m.group(2), t)
    t = t.replace("|", ", ")
    t = _EMOJI.sub("", t)
    t = t.replace("&", " and ").replace("->", " to ").replace("=>", " to ")
    # Collapse whitespace but keep paragraph breaks as sentence boundaries.
    paragraphs = [re.sub(r"[ \t\r\f\v\n]+", " ", p).strip() for p in re.split(r"\n\s*\n", t)]
    out = []
    for p in paragraphs:
        if not p:
            continue
        if p[-1] not in _TERMINATORS + _CLOSERS + ":,;":
            p += "."
        # Each paragraph/bullet starts a sentence; capitalise so the splitter
        # (which treats a lowercase start as a continuation) keeps them apart.
        p = p[0].upper() + p[1:]
        out.append(p)
    return " ".join(out).replace(" ,", ",").strip()
