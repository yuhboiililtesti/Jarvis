/* =========================================================================
   voice.js — the HUD's voice controller.
   * push-to-talk (button, Space-hold, configured hotkey) → /api/voice/transcribe
   * progressive reply speech: sentences → /api/tts → ordered browser playback
   * hands-free Conversation mode: energy VAD, auto-send, speak, listen again,
     barge-in, ends after conversation_timeout_s of silence
   * live microphone indicator (browser AND server mic) with one-click stop
   * captions (what was heard / what is being said) and voice settings
   Privacy: the microphone is opened only by an explicit owner action (holding
   push-to-talk or switching Conversation mode on) and never on page load.
   ========================================================================= */

import { AudioQueue, EnergyVad, MicCapture, ReplyChunker, encodeWav16k, matchesHotkey, parseHotkey } from "./speech.js";

const $ = (id) => document.getElementById(id);
const REVIEW_KEY = "jarvis.voice.review";

function storageGet(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch { return fallback; }
}
function storageSet(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } }

export class VoiceController {
  /**
   * @param {object} o
   * @param {import("./transport.js").Api} o.api
   * @param {(text:string, opts:{source:string})=>Promise<any>} o.send  sends a chat turn
   * @param {()=>boolean} o.canSpeak   false when muted / TTS disabled
   * @param {(msg:string, kind?:string)=>void} o.toast
   * @param {(state:string)=>void} o.onState  idle|listening|recording|transcribing|thinking|speaking
   * @param {(text:string)=>void} o.fillInput  put text in the composer (review mode)
   */
  constructor({ api, send, canSpeak, toast, onState, fillInput }) {
    Object.assign(this, { api, send, canSpeak, toast, onStateCb: onState, fillInput });
    this.info = null;                  // /api/voice/voices payload
    this.queue = new AudioQueue({ gapMs: 110 });
    this.mic = new MicCapture();
    this.state = "idle";
    this.convo = false;                // conversation mode on?
    this.ptt = false;                  // push-to-talk held?
    this.pttFrames = [];
    this.serverListening = false;
    this.hotkey = null;
    this.review = storageGet(REVIEW_KEY, "0") === "1";
    this.timeoutS = 8;
    this._reply = null;
    this._turnId = 0;
    this._utter = null;                // {frames, ms} while capturing an utterance
    this._preroll = [];
    this._silentMs = 0;
    this._vad = new EnergyVad({ minLevel: 0.012, factor: 3, startMs: 160, endMs: 800 });
    this._bargeVad = new EnergyVad({ minLevel: 0.05, factor: 6, startMs: 400, endMs: 500 });

    this.mic.addEventListener("frame", (e) => this._onFrame(e.detail));
    this.mic.addEventListener("open", () => this._renderMic());
    this.mic.addEventListener("close", () => this._renderMic());
    this.queue.addEventListener("start", () => { if (this.state !== "recording") this._setState("speaking"); });
    this.queue.addEventListener("sentence", (e) => this.caption("jarvis", e.detail.text));
    this.queue.addEventListener("end", () => this._onQueueEnd());
    this.queue.addEventListener("error", (e) => this.note(`Speech playback problem: ${e.detail?.error || "unknown"}`));
  }

  /* ── state / indicators ─────────────────────────────────────────────── */
  _setState(s) {
    if (this.state === s) return;
    this.state = s;
    this.onStateCb?.(s);
    const el = $("voice-state");
    if (el) el.textContent = s === "idle" ? (this.convo ? "conversation on" : "") : s;
  }

  note(msg) {
    const n = $("ptt-note");
    if (!n) return;
    n.textContent = msg || "";
    n.hidden = !msg;
  }

  caption(who, text) {
    const el = $(who === "you" ? "caption-you" : "caption-jarvis");
    if (!el) return;
    el.textContent = text || "";
    el.parentElement.hidden = !text;
    $("captions").hidden = !$("caption-you").textContent && !$("caption-jarvis").textContent;
  }

  setServerListening(on) {
    this.serverListening = !!on;
    this._renderMic();
  }

  _renderMic() {
    const browser = this.mic.isOpen;
    const server = this.serverListening;
    const live = browser || server;
    const box = $("mic-live");
    if (box) {
      box.hidden = !live;
      $("mic-live-text").textContent = browser && server ? "MIC LIVE — browser + server"
        : browser ? (this.convo ? "MIC LIVE — conversation" : "MIC LIVE — push-to-talk")
        : "MIC LIVE — server wake word";
    }
    document.body.classList.toggle("mic-live", live);
    const c = $("convo-btn");
    if (c) {
      c.setAttribute("aria-pressed", this.convo ? "true" : "false");
      c.textContent = this.convo ? "End conversation" : "Conversation";
    }
  }

  /* ── settings / info ────────────────────────────────────────────────── */
  async load() {
    const r = await this.api.get("/api/voice/voices");
    if (!r.ok || !r.data) {
      this.info = null;
      this._renderSettings(r.unavailable ? "Voice routes not available on this server (404)." : `Could not load voices: ${r.error || r.status}`);
      return null;
    }
    this.info = r.data;
    this.timeoutS = Number(r.data.conversation_timeout_s) || 8;
    this.hotkey = parseHotkey(r.data.push_to_talk_hotkey);
    this.queue.setVolume(r.data.active?.volume ?? 1);
    this.setServerListening(!!r.data.wake?.listening);
    this._renderSettings();
    return r.data;
  }

  _renderSettings(error) {
    const status = $("voice-settings-status");
    const info = this.info;
    const engineSel = $("set-tts-engine");
    if (!engineSel) return;
    if (!info) { if (status) status.textContent = error || "voice unavailable"; return; }
    const a = info.active || {};
    const engines = info.engines || {};
    engineSel.replaceChildren(...["kokoro", "piper", "null"].map((name) => {
      const e = engines[name] || {};
      const o = document.createElement("option");
      o.value = name;
      o.textContent = name === "null" ? "null (no speech)" : `${name}${e.available ? "" : " — unavailable"}`;
      return o;
    }));
    engineSel.value = a.engine || "kokoro";
    this._fillVoices();
    $("set-tts-effect").value = a.effect || "jarvis";
    $("set-tts-strength").value = String(Math.round((a.effect_strength ?? 0.5) * 100));
    $("set-tts-strength-out").textContent = `${Math.round((a.effect_strength ?? 0.5) * 100)}%`;
    $("set-tts-speed").value = String(a.speed ?? 1);
    $("set-tts-speed-out").textContent = `${Number(a.speed ?? 1).toFixed(2)}×`;
    $("set-tts-output").value = a.output || "browser";
    $("set-voice-review").checked = this.review;
    const active = info.active_engine;
    const e = engines[a.engine] || {};
    const parts = [`speaking with ${active}${active !== a.engine ? ` (selected ${a.engine} — ${e.reason || "unavailable"})` : ""}`];
    if (active === "null") parts.push("no speech engine available — replies will be text only");
    if (this.hotkey) parts.push(`push-to-talk hotkey: ${info.push_to_talk_hotkey}`);
    if (status) status.textContent = parts.join(" · ");
  }

  _fillVoices() {
    const engine = $("set-tts-engine").value;
    const sel = $("set-tts-voice");
    const voices = this.info?.engines?.[engine]?.voices || [];
    sel.replaceChildren(...voices.map((v) => {
      const o = document.createElement("option");
      o.value = v.id;
      o.textContent = v.label && v.label !== v.id ? `${v.label} (${v.id})` : v.id;
      return o;
    }));
    sel.disabled = !voices.length;
    const a = this.info?.active || {};
    const current = engine === "kokoro" ? a.kokoro_voice : engine === "piper" ? a.piper_voice : "";
    if (voices.some((v) => v.id === current)) sel.value = current;
  }

  async select(patch) {
    const r = await this.api.post("/api/voice/select", patch);
    if (!r.ok) { this.toast(`Voice change rejected: ${r.error || r.status}`, "err"); await this.load(); return false; }
    await this.load();
    return true;
  }

  wireSettings() {
    $("set-tts-engine").addEventListener("change", async (e) => {
      this._fillVoices();
      const voice = $("set-tts-voice").value;
      await this.select(voice ? { engine: e.target.value, voice } : { engine: e.target.value });
    });
    $("set-tts-voice").addEventListener("change", (e) => this.select({ voice: e.target.value }));
    $("set-tts-effect").addEventListener("change", (e) => this.select({ effect: e.target.value }));
    $("set-tts-strength").addEventListener("input", (e) => { $("set-tts-strength-out").textContent = `${e.target.value}%`; });
    $("set-tts-strength").addEventListener("change", (e) => this.select({ effect_strength: Number(e.target.value) / 100 }));
    $("set-tts-speed").addEventListener("input", (e) => { $("set-tts-speed-out").textContent = `${Number(e.target.value).toFixed(2)}×`; });
    $("set-tts-speed").addEventListener("change", (e) => this.select({ speed: Number(e.target.value) }));
    $("set-tts-output").addEventListener("change", (e) => this.select({ output: e.target.value }));
    $("set-voice-review").addEventListener("change", (e) => { this.review = e.target.checked; storageSet(REVIEW_KEY, this.review ? "1" : "0"); });
    $("voice-audition").addEventListener("click", () => this.audition());
  }

  async audition() {
    const body = {
      engine: $("set-tts-engine").value,
      voice: $("set-tts-voice").value || undefined,
      effect: $("set-tts-effect").value,
      effect_strength: Number($("set-tts-strength").value) / 100,
    };
    this.queue.stop();
    $("voice-settings-status").textContent = "synthesising sample…";
    const p = this.api.post("/api/voice/audition", body).then((r) => {
      if (!r.ok) return { error: r.error || String(r.status) };
      const d = r.data || {};
      const used = `${d.engine}${d.voice ? ` · ${d.voice}` : ""} · effect ${d.effect}`;
      $("voice-settings-status").textContent = d.ok ? `audition: ${used} (${d.duration_s}s)` : `audition failed: ${d.reason || "no speech"}`;
      return d.ok && d.url ? { url: d.url, text: d.text } : { error: d.reason || "no speech" };
    });
    this.queue.enqueue(p, "");
  }

  /* ── speech output ──────────────────────────────────────────────────── */
  _browserPlays() {
    const out = this.info?.active?.output || "browser";
    return out === "browser" || out === "both";
  }

  _tts(text) {
    return this.api.post("/api/tts", { text }).then((r) => {
      if (r.unavailable) return { error: "TTS endpoint not available (404)" };
      if (!r.ok) return { error: r.error || String(r.status) };
      const d = r.data || {};
      if (!d.ok || !d.speech) return { error: d.reason || "no speech produced" };
      if (d.fallback_reason) this.note(`Voice fell back to ${d.engine}: ${d.fallback_reason}`);
      return this._browserPlays() ? { url: d.url, text } : null;
    });
  }

  speakSentence(text) {
    if (!text || !this.canSpeak()) return;
    this.queue.enqueue(this._tts(text), text);
  }

  /** Begin a reply: returns {push(token), finish(finalText)} that speaks progressively. */
  beginReply() {
    const chunker = new ReplyChunker();
    this._reply = chunker;
    return {
      push: (token) => { if (this._reply === chunker) for (const s of chunker.push(token)) this.speakSentence(s); },
      finish: (finalText) => { if (this._reply === chunker) for (const s of chunker.finish(finalText)) this.speakSentence(s); this._reply = null; },
    };
  }

  stopSpeaking() { this._reply = null; this.queue.stop(); }

  _onQueueEnd() {
    if (this.state === "speaking") this._setState(this.convo ? "listening" : "idle");
    this.caption("jarvis", "");
  }

  /* ── microphone frames ──────────────────────────────────────────────── */
  _onFrame(frame) {
    const rate = this.mic.sampleRate;
    if (this.ptt) { this.pttFrames.push(frame); return; }
    if (!this.convo) return;
    const ms = (frame.length / rate) * 1000;

    if (this.state === "speaking" || this.queue.busy) {
      // Barge-in: a stricter gate so JARVIS's own voice (echo) does not trigger it.
      this._keepPreroll(frame, rate);
      if (this._bargeVad.feed(frame, rate) === "start") {
        this.stopSpeaking();
        this.caption("jarvis", "");
        this._beginUtterance();
        this._vad.reset();
        this._vad.inSpeech = true;
      }
      return;
    }
    if (this.state === "thinking" || this.state === "transcribing") return;

    const ev = this._vad.feed(frame, rate);
    if (this._utter) {
      this._utter.frames.push(frame);
      this._utter.ms += ms;
      if (ev === "end" || this._utter.ms > 15000) this._endUtterance();
      return;
    }
    this._keepPreroll(frame, rate);
    if (ev === "start") { this._beginUtterance(); return; }
    this._silentMs += ms;
    if (this._silentMs >= this.timeoutS * 1000) {
      this.toast(`Conversation ended after ${this.timeoutS}s of silence.`, "info");
      this.setConversation(false);
    }
  }

  _keepPreroll(frame, rate) {
    this._preroll.push(frame);
    let ms = 0;
    for (const f of this._preroll) ms += (f.length / rate) * 1000;
    while (ms > 450 && this._preroll.length > 1) { ms -= (this._preroll[0].length / rate) * 1000; this._preroll.shift(); }
  }

  _beginUtterance() {
    this._utter = { frames: [...this._preroll], ms: 0 };
    this._preroll = [];
    this._silentMs = 0;
    this._bargeVad.reset();
    this._setState("recording");
    this.caption("you", "listening…");
  }

  async _endUtterance() {
    const frames = this._utter?.frames || [];
    this._utter = null;
    this._setState("transcribing");
    const text = await this._transcribe(frames);
    if (!this.convo) return;
    if (!text) { this._silentMs = 0; this._setState("listening"); return; }
    await this._runTurn(text);
  }

  async _runTurn(text) {
    const turn = ++this._turnId;
    this._setState("thinking");
    this.caption("you", text);
    try { await this.send(text, { source: "voice" }); } catch (err) { this.note(`Send failed: ${err}`); }
    await this.queue.idle();
    // A barge-in may already have started the next utterance/turn: leave it alone.
    if (turn !== this._turnId || this._utter || this.state === "recording" || this.state === "transcribing") return;
    this._silentMs = 0;
    this._vad.reset();
    this._setState(this.convo ? "listening" : "idle");
  }

  async _transcribe(frames) {
    if (!frames.length) return "";
    const blob = encodeWav16k(frames, this.mic.sampleRate);
    const fd = new FormData();
    fd.append("file", blob, "utterance.wav");
    const r = await this.api.post("/api/voice/transcribe", fd);
    if (r.unavailable) { this.note("Speech-to-text endpoint is not available on this server. Type instead."); return ""; }
    if (!r.ok) { this.note(`Transcription failed: ${r.error || r.status}`); return ""; }
    const d = r.data || {};
    if (!d.ok || !d.text) {
      this.caption("you", "");
      if (d.reason && !/no speech/.test(d.reason)) this.note(`Didn't catch that: ${d.reason}`);
      return "";
    }
    this.note("");
    return d.text;
  }

  /* ── push-to-talk ───────────────────────────────────────────────────── */
  async startPTT() {
    if (this.ptt || this.convo) return;
    this.ptt = true;
    this.pttFrames = [];
    $("ptt-btn")?.setAttribute("aria-pressed", "true");
    this.stopSpeaking();                         // talking over JARVIS = barge-in
    try {
      await this.mic.open();
    } catch (err) {
      this.ptt = false;
      $("ptt-btn")?.setAttribute("aria-pressed", "false");
      const reason = err?.name === "NotAllowedError" ? "microphone permission denied" : String(err?.message || err);
      this.note(`Could not open the microphone: ${reason}. Type your message instead.`);
      return;
    }
    if (!this.ptt) { await this.mic.close(); return; }   // released while opening
    this._setState("recording");
    this.caption("you", "listening…");
    this.note("Recording… release to send.");
  }

  async stopPTT() {
    if (!this.ptt) return;
    this.ptt = false;
    $("ptt-btn")?.setAttribute("aria-pressed", "false");
    const frames = this.pttFrames;
    this.pttFrames = [];
    if (!this.convo) await this.mic.close();
    if (!frames.length) { this._setState("idle"); this.note(""); this.caption("you", ""); return; }
    this._setState("transcribing");
    this.note("Transcribing…");
    const text = await this._transcribe(frames);
    if (!text) { this._setState("idle"); return; }
    if (this.review) {
      this.caption("you", text);
      this.fillInput(text);
      this.note("Transcript ready — review it, then press Enter to send.");
      this._setState("idle");
      return;
    }
    await this._runTurn(text);
  }

  /* ── conversation mode ──────────────────────────────────────────────── */
  async setConversation(on) {
    if (on && !this.convo) {
      try {
        await this.mic.open();
      } catch (err) {
        const reason = err?.name === "NotAllowedError" ? "microphone permission denied" : String(err?.message || err);
        this.toast(`Conversation mode needs the microphone: ${reason}`, "err");
        return;
      }
      this.convo = true;
      this._silentMs = 0;
      this._preroll = [];
      this._vad.reset();
      this._setState("listening");
      this.note(`Conversation mode: just talk. It ends after ${this.timeoutS}s of silence, or press Esc.`);
    } else if (!on && this.convo) {
      this.convo = false;
      this._utter = null;
      if (!this.ptt) await this.mic.close();
      this.stopSpeaking();
      this._setState("idle");
      this.caption("you", "");
      this.note("");
    }
    this._renderMic();
  }

  toggleConversation() { return this.setConversation(!this.convo); }

  /** One-click stop for every microphone (browser + server). */
  async stopAllMics() {
    this.ptt = false;
    $("ptt-btn")?.setAttribute("aria-pressed", "false");
    await this.setConversation(false);
    await this.mic.close();
    if (this.serverListening) {
      const r = await this.api.post("/api/voice/mic/stop", {});
      if (r.ok) this.setServerListening(!!r.data?.running);
      else this.toast(`Could not stop the server microphone: ${r.error || r.status}`, "err");
    }
    this._setState("idle");
    this._renderMic();
  }

  /** Hotkey handling; returns true when the event was consumed. */
  keydown(e) {
    if (this.hotkey && matchesHotkey(this.hotkey, e) && !e.repeat) { e.preventDefault(); this.startPTT(); return true; }
    return false;
  }
  keyup(e) {
    if (this.hotkey && this.ptt && e.code === this.hotkey.code) { e.preventDefault(); this.stopPTT(); return true; }
    return false;
  }
}
