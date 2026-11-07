/* =========================================================================
   speech.js — browser-side speech plumbing for the HUD (no network here).
   * splitSentences / SentenceStream: port of jarvis/voice/text.py (keep in step)
   * ReplyChunker: turns a streaming markdown reply into speakable sentences
   * AudioQueue: ordered, interruptible playback of synthesized WAV urls
   * MicCapture: microphone → float32 frames (AudioWorklet, ScriptProcessor fallback)
   * EnergyVad: adaptive energy voice-activity detector with start/end events
   * encodeWav16k: float32 frames → 16 kHz mono 16-bit WAV Blob
   ========================================================================= */

/* ── sentence splitting (mirror of jarvis/voice/text.py) ──────────────── */
const ABBREVIATIONS = new Set([
  "mr", "mrs", "ms", "mx", "dr", "prof", "sr", "jr", "st", "rev", "hon", "gen",
  "col", "capt", "lt", "sgt", "cmdr", "gov", "pres", "sen", "rep",
  "e.g", "i.e", "cf", "vs", "approx", "dept", "est", "fig", "no", "nos", "vol",
  "ch", "pp", "inc", "ltd", "co", "corp", "mt", "ft", "ave", "rd", "blvd",
  "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
  "u.s", "u.k", "a.m", "p.m", "ph.d", "b.sc", "m.sc",
]);
const SOFT_ABBREVIATIONS = new Set(["etc", "al", "a.m", "p.m", "u.s", "u.k", "inc", "ltd", "co"]);
const TERMINATORS = ".!?…";
const CLOSERS = "\"')]}”’";
export const MAX_CHARS = 240;

const isSpace = (c) => /\s/.test(c);
const isUpper = (c) => c !== c.toLowerCase() && c === c.toUpperCase();
const isLower = (c) => c !== c.toUpperCase() && c === c.toLowerCase();
const isDigit = (c) => c >= "0" && c <= "9";
const isAlpha = (c) => isUpper(c) || isLower(c);

function wordBefore(text, end) {
  let start = end;
  while (start > 0 && !isSpace(text[start - 1]) && !"([{\"'".includes(text[start - 1])) start--;
  return text.slice(start, end).toLowerCase().replace(/\.+$/, "");
}

/** true / false, or null when more text is needed (mirrors _is_boundary). */
function isBoundary(text, i) {
  const n = text.length;
  const j = i + 1;
  if (j >= n) return null;
  if (!isSpace(text[j])) return false;
  let k = j;
  while (k < n && isSpace(text[k])) {
    if (text[k] === "\n" && k + 1 < n && text[k + 1] === "\n") return true;
    k++;
  }
  if (k >= n) return null;
  const nxt = text[k];
  let p = i;
  while (p >= 0 && CLOSERS.includes(text[p])) p--;
  if (p < 0) return false;
  const term = text[p];
  let runStart = p;
  while (runStart > 0 && TERMINATORS.includes(text[runStart - 1])) runStart--;
  const run = text.slice(runStart, p + 1);
  if (term === "!" || term === "?") return true;
  if (run.endsWith("...") || term === "…") {
    if (nxt === "I" && (k + 1 >= n || !isAlpha(text[k + 1]))) return false;
    return isUpper(nxt) || isDigit(nxt) || "\"'“".includes(nxt);
  }
  const bare = wordBefore(text, runStart + 1).replace(/^["'([{]+|["'([{]+$/g, "");
  if (SOFT_ABBREVIATIONS.has(bare)) return isUpper(nxt);
  if (ABBREVIATIONS.has(bare)) return false;
  if (bare.length === 1 && isAlpha(bare)) return false;
  if (isLower(nxt)) return false;
  return true;
}

function hardCut(buf) {
  const win = buf.slice(0, MAX_CHARS);
  for (const mark of ["; ", ": ", ", ", " - ", " "]) {
    const idx = win.lastIndexOf(mark);
    if (idx > Math.floor(MAX_CHARS / 3)) return mark.trim() ? idx + mark.trimEnd().length : idx;
  }
  return MAX_CHARS;
}

function scan(buf, final) {
  const out = [];
  let start = 0;
  let i = 0;
  const n = buf.length;
  while (i < n) {
    const ch = buf[i];
    if (TERMINATORS.includes(ch)) {
      let e = i;
      while (e + 1 < n && TERMINATORS.includes(buf[e + 1])) e++;
      while (e + 1 < n && CLOSERS.includes(buf[e + 1])) e++;
      let decision = isBoundary(buf, e);
      if (decision === null) {
        if (final) decision = true;
        else break;
      }
      if (decision) {
        const piece = buf.slice(start, e + 1).trim();
        if (piece) out.push(piece);
        start = e + 1;
      }
      i = e + 1;
      continue;
    }
    if (ch === "\n" && i + 1 < n && buf[i + 1] === "\n") {
      const piece = buf.slice(start, i).trim();
      if (piece) out.push(piece);
      start = i + 2;
      i += 2;
      continue;
    }
    i++;
  }
  let rest = buf.slice(start);
  while (rest.trim().length > MAX_CHARS) {
    const stripped = rest.trimStart();
    const cut = hardCut(stripped);
    out.push(stripped.slice(0, cut).trim());
    rest = stripped.slice(cut);
  }
  if (final) {
    const tail = rest.trim();
    if (tail) out.push(tail);
    rest = "";
  }
  return [out, rest];
}

export function splitSentences(text) { return scan(text || "", true)[0]; }

export class SentenceStream {
  constructor() { this.buf = ""; }
  feed(chunk) { this.buf += chunk || ""; const [out, rest] = scan(this.buf, false); this.buf = rest; return out; }
  flush() { const [out] = scan(this.buf, true); this.buf = ""; return out; }
}

/* ── streaming reply → speakable sentences ─────────────────────────────── */
const CODE_NOTE = "I've put the code on screen.";

/** Markdown prefix → speakable prefix. Unclosed code fences hold the rest back. */
function speakablePrefix(raw, final) {
  let text = raw;
  const parts = text.split("```");
  let out = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 0) out += parts[i];
    else if (i < parts.length - 1) out += `\n\n${CODE_NOTE}\n\n`;        // closed fence
    else out += final ? `\n\n${CODE_NOTE}\n\n` : "";                      // open fence: wait
  }
  // Headings, bullets, quotes start new sentences.
  out = out.replace(/^[ \t]*(?:#{1,6}|[-*+•]|\d+[.)]|>)[ \t]+/gm, "\n\n");
  // Capitalise sentence starts after a paragraph break (server does the same).
  out = out.replace(/\n\n\s*([a-z])/g, (_m, c) => `\n\n${c.toUpperCase()}`);
  return out;
}

export class ReplyChunker {
  constructor() { this.raw = ""; this.emitted = 0; }
  /** Append streamed text; returns newly completed sentences. */
  push(token) {
    this.raw += token || "";
    const [sents] = scan(speakablePrefix(this.raw, false), false);
    const fresh = sents.slice(this.emitted);
    this.emitted = sents.length;
    return fresh.filter(hasWords);
  }
  /** End of reply; returns the remaining sentences. */
  finish(finalText) {
    if (!this.emitted && finalText && !this.raw.trim()) this.raw = finalText;
    const sents = splitSentences(speakablePrefix(this.raw, true));
    const fresh = sents.slice(this.emitted);
    this.emitted = sents.length;
    return fresh.filter(hasWords);
  }
}
const hasWords = (s) => /[A-Za-z0-9]/.test(s);

/* ── ordered, interruptible audio playback ─────────────────────────────── */
export class AudioQueue extends EventTarget {
  constructor({ gapMs = 120 } = {}) {
    super();
    this.gapMs = gapMs;
    this.items = [];
    this.playing = false;
    this.gen = 0;
    this.audio = new Audio();
    this.audio.preload = "auto";
    this.volume = 1;
    this._finishCurrent = null;
  }
  get busy() { return this.playing || this.items.length > 0; }
  setVolume(v) { this.volume = Math.max(0, Math.min(1, Number(v) || 0)); this.audio.volume = this.volume; }
  _emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }

  /** Play a silent sample inside a user gesture so later async speech is allowed. */
  unlock() {
    if (this._unlocked) return;
    this._unlocked = true;
    try {
      const a = this.audio;
      const silent = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YQAAAAA=";
      a.src = silent;
      a.volume = 0;
      const p = a.play();
      if (p && p.then) {
        p.then(() => { a.pause(); a.removeAttribute("src"); a.volume = this.volume; })
          .catch(() => { a.removeAttribute("src"); a.volume = this.volume; });
      }
    } catch { /* autoplay still blocked; the next gesture will retry nothing */ }
  }

  /** Queue a promise resolving to {url, text} (or null to skip). Order is preserved. */
  enqueue(promise, text = "") {
    this.items.push({ promise, text });
    if (!this.playing) this._pump();
  }

  async _pump() {
    const gen = this.gen;
    this.playing = true;
    this._emit("start");
    while (this.items.length && gen === this.gen) {
      const it = this.items.shift();
      let res = null;
      try { res = await it.promise; } catch { res = null; }
      if (gen !== this.gen) return;
      if (!res || !res.url) { if (res && res.error) this._emit("error", res); continue; }
      this._emit("sentence", { text: res.text || it.text });
      const ok = await this._play(res.url);
      if (gen !== this.gen) return;
      if (!ok) this._emit("error", { error: "browser could not play the audio" });
      if (this.items.length) await new Promise((r) => setTimeout(r, this.gapMs));
    }
    if (gen !== this.gen) return;
    this.playing = false;
    this._emit("end");
  }

  _play(url) {
    return new Promise((resolve) => {
      const a = this.audio;
      const done = (ok) => { a.onended = a.onerror = null; this._finishCurrent = null; resolve(ok); };
      this._finishCurrent = () => done(false);
      a.onended = () => done(true);
      a.onerror = () => done(false);
      a.src = url;
      a.volume = this.volume;
      const p = a.play();
      if (p && p.catch) p.catch(() => done(false));
    });
  }

  /** Stop now (barge-in / mute): drop everything queued. */
  stop() {
    const wasBusy = this.busy;
    this.gen++;
    this.items = [];
    try { this.audio.pause(); } catch { /* ignore */ }
    this.audio.removeAttribute("src");
    this._finishCurrent?.();
    this.playing = false;
    if (wasBusy) this._emit("end", { interrupted: true });
  }

  /** Resolves when playback has drained (or was stopped). */
  idle() {
    if (!this.busy) return Promise.resolve();
    return new Promise((resolve) => {
      const h = () => { this.removeEventListener("end", h); resolve(); };
      this.addEventListener("end", h);
    });
  }
}

/* ── microphone capture ────────────────────────────────────────────────── */
export class MicCapture extends EventTarget {
  constructor() { super(); this.stream = null; this.ctx = null; this.node = null; this.src = null; this.sink = null; }
  get isOpen() { return !!this.stream; }
  get sampleRate() { return this.ctx ? this.ctx.sampleRate : 48000; }

  async open() {
    if (this.stream) return;
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("this browser has no microphone API");
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    try {
      const src = ctx.createMediaStreamSource(stream);
      let node;
      try {
        await ctx.audioWorklet.addModule("/static/pcm-worklet.js");
        node = new AudioWorkletNode(ctx, "pcm-tap");
        node.port.onmessage = (e) => this._frame(e.data);
      } catch {
        node = ctx.createScriptProcessor(2048, 1, 1);
        node.onaudioprocess = (e) => this._frame(new Float32Array(e.inputBuffer.getChannelData(0)));
      }
      // A muted sink keeps the graph pulled without making any sound.
      const sink = ctx.createGain();
      sink.gain.value = 0;
      src.connect(node);
      node.connect(sink);
      sink.connect(ctx.destination);
      Object.assign(this, { stream, ctx, node, src, sink });
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop());
      try { await ctx.close(); } catch { /* ignore */ }
      throw err;
    }
    this.dispatchEvent(new CustomEvent("open"));
  }

  _frame(data) { this.dispatchEvent(new CustomEvent("frame", { detail: data })); }

  async close() {
    const { stream, ctx, node, src, sink } = this;
    this.stream = this.ctx = this.node = this.src = this.sink = null;
    if (!stream) return;
    try { src?.disconnect(); node?.disconnect(); sink?.disconnect(); } catch { /* ignore */ }
    if (node && node.port) node.port.onmessage = null;
    stream.getTracks().forEach((t) => t.stop());
    try { await ctx?.close(); } catch { /* ignore */ }
    this.dispatchEvent(new CustomEvent("close"));
  }
}

/* ── energy VAD ────────────────────────────────────────────────────────── */
export function rms(frame) {
  let s = 0;
  for (let i = 0; i < frame.length; i++) s += frame[i] * frame[i];
  return frame.length ? Math.sqrt(s / frame.length) : 0;
}

export class EnergyVad {
  /**
   * @param {object} o  minLevel: absolute floor; factor: × noise floor;
   *                    startMs / endMs: hysteresis durations.
   */
  constructor({ minLevel = 0.012, factor = 3, startMs = 160, endMs = 800 } = {}) {
    Object.assign(this, { minLevel, factor, startMs, endMs });
    this.floor = 0.005;
    this.inSpeech = false;
    this.aboveMs = 0;
    this.belowMs = 0;
  }
  get threshold() { return Math.max(this.minLevel, this.floor * this.factor); }
  reset() { this.inSpeech = false; this.aboveMs = 0; this.belowMs = 0; }
  /** Returns "start" | "end" | "speech" | "silence". */
  feed(frame, rate) {
    const ms = (frame.length / rate) * 1000;
    const level = rms(frame);
    const loud = level >= this.threshold;
    if (!this.inSpeech && !loud) {
      // Track the room's noise floor only while nobody is speaking.
      this.floor = this.floor * 0.95 + Math.min(level, 0.05) * 0.05;
    }
    if (loud) {
      this.belowMs = 0;
      this.aboveMs += ms;
      if (!this.inSpeech && this.aboveMs >= this.startMs) { this.inSpeech = true; return "start"; }
      return this.inSpeech ? "speech" : "silence";
    }
    this.aboveMs = 0;
    if (this.inSpeech) {
      this.belowMs += ms;
      if (this.belowMs >= this.endMs) { this.inSpeech = false; this.belowMs = 0; return "end"; }
      return "speech";
    }
    return "silence";
  }
}

/* ── WAV encoding ──────────────────────────────────────────────────────── */
export function encodeWav16k(frames, rate) {
  let total = 0;
  for (const f of frames) total += f.length;
  const src = new Float32Array(total);
  let off = 0;
  for (const f of frames) { src.set(f, off); off += f.length; }
  const target = 16000;
  let pcm;
  if (rate === target) pcm = src;
  else {
    const ratio = rate / target;
    const n = Math.floor(src.length / ratio);
    pcm = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      // box-average the source span: cheap anti-aliasing before decimation
      const a = Math.floor(i * ratio);
      const b = Math.min(src.length, Math.max(a + 1, Math.floor((i + 1) * ratio)));
      let s = 0;
      for (let j = a; j < b; j++) s += src[j];
      pcm[i] = s / (b - a);
    }
  }
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); w(8, "WAVE");
  w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, target, true); v.setUint32(28, target * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  w(36, "data"); v.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) {
    const x = Math.max(-1, Math.min(1, pcm[i]));
    v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

/* ── hotkey parsing ("ctrl+alt+space") ─────────────────────────────────── */
export function parseHotkey(spec) {
  const parts = String(spec || "").toLowerCase().split("+").map((s) => s.trim()).filter(Boolean);
  if (!parts.length) return null;
  const key = parts.pop();
  const code = key === "space" ? "Space" : key.length === 1 ? (/[a-z]/.test(key) ? `Key${key.toUpperCase()}` : /[0-9]/.test(key) ? `Digit${key}` : null) : key.replace(/^\w/, (c) => c.toUpperCase());
  if (!code) return null;
  return { code, ctrl: parts.includes("ctrl") || parts.includes("control"), alt: parts.includes("alt"), shift: parts.includes("shift"), meta: parts.includes("meta") || parts.includes("win") || parts.includes("cmd"), label: spec };
}
export function matchesHotkey(hk, e) {
  return !!hk && e.code === hk.code && e.ctrlKey === hk.ctrl && e.altKey === hk.alt && e.shiftKey === hk.shift && e.metaKey === hk.meta;
}
