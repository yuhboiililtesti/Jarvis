/* =========================================================================
   app.js ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â JARVIS HUD controller.
   Modules: DOM utils, state, toasts, reactor, connection, event routing,
   telemetry, panels, chat (SSE), approvals, palette, keyboard, diagnostics.
   Loaded as an ES module:  <script type="module" src="app.js"></script>
   ========================================================================= */

import { Api, BusSocket, streamChat, wsUrl } from "./transport.js?v=20261114b";
import { ArcReactor, Sparkline } from "./reactor.js?v=20261114b";
import { VoiceController } from "./voice.js?v=20261114b";

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ small DOM helpers ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
const $ = (id) => document.getElementById(id);
function h(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = String(v);
    else if (k === "dataset") Object.assign(n.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2).toLowerCase(), v);
    else n.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ state ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
const state = {
  connected: false,
  everConnected: false,
  wsStatus: "idle",
  health: null,
  persona: { name: "JARVIS", mood: "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â", busy: false },
  mode: "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â",
  defcon: null,
  llm: null,
  toolCount: null,
  tools: [],
  voice: null,
  busy: false,
  errorUntil: 0,
  activeTasks: new Map(),
  approvals: new Map(),
  streaming: false,
  abort: null,
  muted: false,
  ttsEnabled: true,
  metrics: { cpu: [], mem: [], disk: [], temp: [] },
  metricUnavailable: {},
  feed: [],
  feedFilter: "all",
  capabilities: { stt: null, tts: null, mediaRecorder: !!window.MediaRecorder, mic: null },
  lastFrameTs: 0,
  lastSeq: null,
  lastMetricTs: 0,
  latency: null,
  lastTts: null,
  voiceState: "idle",   // idle | listening | recording | transcribing | thinking | speaking
};

const api = new Api("");
let reactor, socket, voice;
const sparks = {};

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ toasts ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function toast(msg, kind = "info", timeout = 5000) {
  const node = h("div", { class: `toast ${kind}`, text: msg });
  $("toast-region").append(node);
  setTimeout(() => { node.style.opacity = "0"; setTimeout(() => node.remove(), 220); }, timeout);
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ clock ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function startClock() {
  const tick = () => { $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false }); };
  tick();
  setInterval(tick, 1000);
}

function fmtTime(ts) {
  if (!ts) return "--:--:--";
  const d = typeof ts === "number" ? new Date(ts * (ts < 1e12 ? 1000 : 1)) : new Date(ts);
  if (isNaN(d)) return String(ts);
  return d.toLocaleTimeString([], { hour12: false });
}
function ago(ts) {
  if (!ts) return "never";
  const d = typeof ts === "number" ? new Date(ts * (ts < 1e12 ? 1000 : 1)) : new Date(ts);
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (!isFinite(s)) return "unknown";
  if (s < 2) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ connection ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function setConnected(connected, { status, attempt, delayMs } = {}) {
  state.connected = connected;
  if (status) state.wsStatus = status;
  if (connected) state.everConnected = true;

  const badge = $("conn-badge");
  if (connected) badge.className = "badge badge-live";
  else if (state.wsStatus === "reconnecting") badge.className = "badge badge-recon";
  else if (state.wsStatus === "connecting") badge.className = "badge badge-unknown";
  else badge.className = "badge badge-down";
  badge.textContent = connected ? "LIVE"
    : state.wsStatus === "reconnecting" ? "RECONNECTING"
    : state.wsStatus === "connecting" ? "CONNECTING"
    : "OFFLINE";

  const banner = $("conn-banner");
  if (connected) {
    banner.hidden = true;
    document.body.classList.remove("is-disconnected");
  } else {
    banner.hidden = false;
    banner.classList.toggle("is-stale", state.everConnected);
    $("conn-banner-text").textContent = state.everConnected
      ? `DISCONNECTED ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â reconnecting${attempt ? ` (attempt ${attempt}${delayMs ? `, in ${Math.round(delayMs / 1000)}s` : ""})` : "ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦"} ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â telemetry marked STALE`
      : "CONNECTING ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â no link yet ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â telemetry unavailable";
  }
  $("telemetry-stale").hidden = connected || !state.everConnected;
  // Staleness is about the data, not the socket: metrics arrive over HTTP too.
  updateReactor();
  if (connected) refreshAll();
}

let _staleMetrics = false;
function markTelemetryStale(stale) {
  if (_staleMetrics === stale) return;
  _staleMetrics = stale;
  for (const id of ["metric-cpu", "metric-mem", "metric-disk", "metric-temp", "llm-status", "health-core", "telemetry-updated"]) {
    $(id).classList.toggle("is-stale", stale);
  }
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ reactor state derivation ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function updateReactor() {
  if (!reactor) return;
  let mode = "idle";
  let sub = "idle";
  if (!state.connected) { mode = "offline"; sub = state.everConnected ? "link lost" : "awaiting link"; }
  else if (Date.now() < state.errorUntil) { mode = "error"; sub = "fault detected"; }
  else if (state.defcon !== null && state.defcon <= 3) { mode = "warn"; sub = `defcon ${state.defcon}`; }
  else if (state.voiceState === "recording") { mode = "listening"; sub = "hearing you"; }
  else if (state.voiceState === "speaking") { mode = "speaking"; sub = "speaking"; }
  else if (state.voiceState === "transcribing") { mode = "thinking"; sub = "transcribing"; }
  else if (state.busy || state.voiceState === "thinking") { mode = "thinking"; sub = "working"; }
  else if (state.voiceState === "listening") { mode = "listening"; sub = "listening ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â just talk"; }
  else if (state.llm && state.llm.ok === false) { mode = "warn"; sub = "model offline ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â start Ollama"; }
  else { mode = "idle"; sub = state.persona.mood && state.persona.mood !== "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â" ? state.persona.mood : "standing by"; }
  reactor.setMode(mode);

  const label = $("reactor-state");
  label.textContent = mode === "thinking" ? "THINKING" : mode === "listening" ? "LISTENING" : mode === "speaking" ? "SPEAKING"
    : mode === "error" ? "FAULT" : mode === "warn" ? "ALERT" : mode === "offline" ? "OFFLINE" : "IDLE";
  label.classList.toggle("is-error", mode === "error");
  label.classList.toggle("is-warn", mode === "warn");
  $("reactor-sub").textContent = sub;
}

function recomputeBusy() {
  const active = [...state.activeTasks.values()].some((s) => !TERMINAL.has(s));
  state.busy = state.streaming || active || !!state.persona.busy;
  $("chat-status").textContent = state.streaming ? "streamingÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦" : active ? "workingÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦" : state.connected ? "ready" : "offline";
  $("chat-status").className = "convo-status" + (state.streaming ? " busy" : "");
  updateReactor();
}
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "blocked", "outcome-unknown"]);

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ event feed ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function categoryOf(topic) {
  if (topic.startsWith("task.")) return "task";
  if (topic === "agent.tool_call" || topic === "tool.result" || topic === "agent.step") return "tool";
  if (topic === "verify") return "verify";
  if (topic === "alert") return "alert";
  if (topic.startsWith("voice.")) return "voice";
  if (topic === "error" || topic === "degraded") return "error";
  return "system";
}

function summarize(frame) {
  const d = frame.data || {};
  switch (frame.topic) {
    case "status": return `${d.component || "system"} ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ ${d.state || "?"}`;
    case "degraded": return `${d.component || "system"} degraded: ${d.reason || "reason not given"}`;
    case "error": return `${d.component || "system"} error: ${d.message || "unknown"}`;
    case "task.created": return `task ${short(d.task_id)} created ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${d.request || ""}`;
    case "task.status": return `task ${short(d.task_id)} ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ ${d.status || "?"}${d.error ? ` (${d.error})` : ""}`;
    case "task.blocked": return `task ${short(d.task_id)} blocked ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${d.tool || ""}: ${d.reason || ""}`;
    case "task.reconcile": return `${d.count || 0} interrupted attempt(s) reconciled`;
    case "agent.step": return `task ${short(d.task_id)} step ${d.step ?? "?"}`;
    case "agent.tool_call": return `calling ${d.tool}(${briefArgs(d.args)})`;
    case "tool.result": return `${d.tool} ${d.ok ? "ok" : "failed"}${d.verified === false ? " (unverified)" : ""} ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${d.duration_ms ?? "?"}ms`;
    case "approval.requested": return `authorisation needed: ${d.tool} [${d.side_effect || "?"}] ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${d.reason || ""}`;
    case "approval.resolved": return `approval ${short(d.approval_id)} ${d.decision} by ${d.by || "owner"}`;
    case "verify": return `verify ${d.tool}: ${d.verified ? "confirmed" : "not confirmed"}${d.method ? ` (${d.method})` : ""}`;
    case "memory.recall": return `recalled ${d.count ?? 0} memory item(s)${d.method ? ` via ${d.method}` : ""}`;
    case "alert": return `${(d.severity || "info").toUpperCase()}: ${d.title || d.source || "alert"}`;
    case "defcon": return `DEFCON ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ ${d.level ?? "?"}${d.note ? ` (${d.note})` : ""}`;
    case "voice.tts.start": return "speakingÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦";
    case "voice.tts.done": return `speech ${d.ok ? "ready" : "failed"} (${d.engine || "?"}${d.duration_s ? `, ${d.duration_s}s` : ""})${d.reason ? ` ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${d.reason}` : ""}`;
    case "voice.stt.start": return "transcribingÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦";
    case "voice.stt.done": return `transcribed ${d.chars ?? 0} chars${d.reason ? ` ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${d.reason}` : ""}`;
    case "voice.status": return `server microphone ${d.state || "?"}${d.reason ? ` ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${d.reason}` : ""}`;
    case "voice.wake": return `wake word heard (${d.model || "?"})`;
    case "node.status": return `node ${d.node || d.name || d.id || "?"} ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ ${d.status || "?"}`;
    case "metric": return `${d.name || "metric"} = ${d.value ?? "?"}`;
    default: return JSON.stringify(d).slice(0, 160);
  }
}
const short = (id) => (id ? String(id).slice(-6) : "?");
function briefArgs(args) {
  if (!args || typeof args !== "object") return "";
  const t = JSON.stringify(args);
  return t.length > 60 ? t.slice(0, 57) + "ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦" : t;
}

function pushFeed(frame) {
  // Heartbeats and telemetry belong in their own panels, not the activity feed.
  if (frame.topic === "runtime.tick" || frame.topic === "metrics") return;
  const cat = categoryOf(frame.topic);
  const entry = { ...frame, cat, ts: frame.ts || Date.now(), msg: summarize(frame) };
  state.feed.push(entry);
  if (state.feed.length > 300) state.feed.shift();
  if (state.feedFilter === "all" || state.feedFilter === cat) renderFeedItem(entry);
}

function renderFeedItem(e) {
  const feed = $("feed");
  const li = h("li", { class: `feed-item t-${e.cat}${e.cat === "error" ? " t-error" : ""}` },
    h("div", { class: "fi-top" },
      h("span", { class: "fi-topic", text: e.topic }),
      h("span", { class: "fi-ts", text: fmtTime(e.ts) }),
    ),
    h("div", { class: "fi-msg", text: e.msg }),
  );
  feed.append(li);
  while (feed.children.length > 300) feed.firstChild.remove();
  feed.scrollTop = feed.scrollHeight;
}

function renderFeed() {
  const feed = $("feed");
  feed.replaceChildren();
  const items = state.feedFilter === "all" ? state.feed : state.feed.filter((e) => e.cat === state.feedFilter);
  if (!items.length) {
    feed.append(h("li", {}, h("p", { class: "empty", text: "No events yet." })));
    return;
  }
  for (const e of items) {
    feed.append(h("li", { class: `feed-item t-${e.cat}` },
      h("div", { class: "fi-top" }, h("span", { class: "fi-topic", text: e.topic }), h("span", { class: "fi-ts", text: fmtTime(e.ts) })),
      h("div", { class: "fi-msg", text: e.msg }),
    ));
  }
  feed.scrollTop = feed.scrollHeight;
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ frame routing ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function routeFrame(frame) {
  state.lastFrameTs = Date.now();
  // The bus replays its recent tail on reconnect; drop frames we have seen.
  // A large backwards jump means the Core restarted and reset its sequence.
  if (typeof frame.seq === "number") {
    if (state.lastSeq !== null && frame.seq < state.lastSeq - 50) state.lastSeq = null;
    if (state.lastSeq !== null && frame.seq <= state.lastSeq) return;
    state.lastSeq = frame.seq;
  }
  state.metricUnavailable = state.metricUnavailable || {};
  const d = frame.data || {};

  // error / degraded raise the reactor fault window
  if (frame.topic === "error" || frame.topic === "degraded") {
    state.errorUntil = Date.now() + 25000;
    toast(frame.topic === "error" ? `Error: ${d.component || "system"} ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${d.message || ""}` : `Degraded: ${d.component || "system"} ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${d.reason || ""}`, frame.topic === "error" ? "err" : "warn", 8000);
  }

  switch (frame.topic) {
    case "status":
      if (d.component === "core" && d.state === "ready") { state.errorUntil = 0; }
      break;
    case "task.created": state.activeTasks.set(d.task_id, "received"); refreshTasksSoon(); break;
    case "task.status":
      state.activeTasks.set(d.task_id, d.status);
      if (TERMINAL.has(d.status)) refreshTasksSoon();
      break;
    case "agent.step": state.activeTasks.set(d.task_id, "running"); break;
    case "approval.requested":
      addApproval(d); break;
    case "approval.resolved":
      removeApproval(d.approval_id); break;
    case "defcon":
      if (typeof d.level === "number") { state.defcon = d.level; renderPosture(); }
      break;
    case "alert":
      toast(`ALERT ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${d.title || d.source || "see alerts"}`, d.severity === "critical" ? "err" : "warn", 9000);
      if (canSpeak()) voice?.speakSentence(`${d.title || "Alert"}. ${d.body || ""}`);
      refreshPanel("alerts");
      break;
    case "metric":
      ingestMetric(d.name, d.value); break;
    case "metrics":
      ingestMetricsFrame(d); break;
    case "node.status":
      refreshPanel("nodes"); break;
    case "runtime.tick":
      // The runtime ticks every couple of seconds; only redraw when visible.
      if (currentTab === "runtime") {
        clearTimeout(runtimeTimer);
        runtimeTimer = setTimeout(() => refreshPanel("runtime"), 800);
      }
      break;
    case "voice.tts.done":
      state.lastTts = d.ok ? "ok" : "error"; break;
    case "voice.status":
      voice?.setServerListening(d.state === "listening"); break;
  }
  recomputeBusy();
  pushFeed(frame);
  updateReactor();
}

function ingestMetric(name, value) {
  if (!name || typeof value !== "number") return;
  const key = String(name).toLowerCase();
  const cacheKey = key.includes("cpu") ? "cpu" : key.includes("mem") ? "mem" : key.includes("disk") ? "disk" : key.includes("temp") ? "temp" : key;
  if (state.metrics[cacheKey]) {
    state.metrics[cacheKey].push(value);
    if (state.metrics[cacheKey].length > 120) state.metrics[cacheKey].shift();
  }
  renderMetric(cacheKey, value);
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ telemetry rendering ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function renderMetric(key, value) {
  const valEl = $(`metric-${key}`);
  const barEl = $(`bar-${key}`);
  if (!valEl) return;
  let shown = value;
  let pct = value;
  if (key === "temp") {
    valEl.textContent = `${Number(value).toFixed(1)} Ãƒâ€šÃ‚Â°C`;
    pct = clamp((value / 100) * 100, 0, 100);
    barEl?.classList.toggle("warn", value >= 75);
    barEl?.classList.toggle("crit", value >= 88);
  } else {
    // Backends may report a fraction (0..1) or a percentage (0..100).
    if (value > 0 && value <= 1) { shown = value * 100; }
    pct = clamp(shown, 0, 100);
    valEl.textContent = `${Number(shown).toFixed(0)}%`;
    barEl?.classList.toggle("warn", pct >= 80);
    barEl?.classList.toggle("crit", pct >= 92);
  }
  if (barEl) barEl.style.width = `${pct}%`;
  if (sparks[key]) sparks[key].push(value <= 1 && key !== "temp" ? value * 100 : value);
  state.lastMetricTs = Date.now();
  $("telemetry-updated").textContent = fmtTime(Date.now());
}

function extractMetric(data) {
  if (typeof data === "number") return data;
  if (!data || typeof data !== "object") return null;
  if (typeof data.value === "number") return data.value;
  if (Array.isArray(data)) {
    const last = data[data.length - 1];
    if (typeof last === "number") return last;
    if (last && typeof last.value === "number") return last.value;
  }
  if (Array.isArray(data.series) && data.series.length) {
    // /api/metrics returns {"name": ..., "series": [{ts,name,value}]}
    const last = data.series[data.series.length - 1];
    if (typeof last === "number") return last;
    if (last && typeof last.value === "number") return last.value;
  }
  if (Array.isArray(data.values) && data.values.length) return data.values[data.values.length - 1];
  if (Array.isArray(data.points) && data.points.length) {
    const p = data.points[data.points.length - 1];
    return typeof p === "number" ? p : p?.value ?? null;
  }
  return null;
}

/* The monitoring service stores suffixed names ("cpu.percent"); the gauges do not. */
const METRIC_NAMES = {
  cpu: "cpu.percent",
  mem: "mem.percent",
  disk: "disk.percent",
  temp: "gpu.0.temperature_c",
};

/** Apply a live "metrics" frame from the bus (samples keyed by the real names). */
function ingestMetricsFrame(d) {
  const s = (d && d.samples) || {};
  const pairs = [
    ["cpu", s["cpu.percent"]], ["mem", s["mem.percent"]], ["disk", s["disk.percent"]],
    ["temp", s["gpu.0.temperature_c"] ?? s["temp.gpu0"] ?? s["temp.coretemp"]],
  ];
  for (const [key, value] of pairs) {
    if (typeof value === "number") renderMetric(key, value);
  }
}

async function pollMetrics() {
  if (document.visibilityState === "hidden") return;
  for (const [key, name] of Object.entries(METRIC_NAMES)) {
    if (state.metricUnavailable[key]) continue;
    const r = await api.get(`/api/metrics?name=${encodeURIComponent(name)}`);
    if (r.unavailable) {
      state.metricUnavailable[key] = true;
      const el = $(`metric-${key}`);
      if (el && el.textContent === "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â") el.textContent = "unavailable";
      if (sparks[key]) sparks[key].clear();
      continue;
    }
    if (r.ok) {
      const v = extractMetric(r.data);
      if (typeof v === "number") renderMetric(key, v);
    }
  }
}

function renderPosture() {
  const defcon = state.defcon;
  const badge = $("defcon-badge");
  if (defcon === null || defcon === undefined) {
    badge.className = "badge badge-unknown"; badge.textContent = "DEFCON ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â";
  } else {
    badge.className = `badge badge-defcon${defcon}`; badge.textContent = `DEFCON ${defcon}`;
    $("defcon-select").value = String(defcon);
    $("set-defcon").value = String(defcon);
  }
  const mb = $("mode-badge");
  mb.className = "badge badge-mode"; mb.textContent = `mode: ${state.mode || "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â"}`;
  if (state.mode && state.mode !== "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â") { $("mode-select").value = state.mode; $("set-mode").value = state.mode; }
  const mood = $("mood-badge");
  mood.textContent = `mood: ${state.persona.mood || "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â"}`;
}

function renderHealth() {
  const hel = $("health-core");
  const llmStatus = $("llm-status");
  const llmModel = $("llm-model");
  const llmProv = $("llm-provider");

  const core = state.health?.core ?? state.health?.status ?? null;
  hel.textContent = core ? String(core) : (state.health && Object.keys(state.health).length ? "unknown" : "unavailable");
  hel.className = "kv-v " + (core === "ready" ? "ok" : core ? "" : "warn");

  const llm = state.llm ?? state.health?.llm ?? null;
  if (llm && typeof llm === "object" && Object.keys(llm).length) {
    const ok = llm.ok === true;
    llmStatus.textContent = ok ? "reachable" : (llm.ok === false ? "unreachable" : "unknown");
    llmStatus.className = "kv-v " + (ok ? "ok" : "bad");
    llmProv.textContent = llm.provider || state.health?.llm_provider || "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â";
    const models = Array.isArray(llm.models) ? llm.models : [];
    llmModel.textContent = state.health?.model || (models.length ? models[0] + (models.length > 1 ? ` +${models.length - 1}` : "") : (llm.error ? `error: ${llm.error}` : "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â"));
    if (!ok && llm.error) llmModel.textContent = `unavailable: ${llm.error}`;
  } else {
    llmStatus.textContent = "unavailable"; llmStatus.className = "kv-v bad";
    llmModel.textContent = "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â"; llmProv.textContent = "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â";
  }

  $("tool-count").textContent = state.toolCount === null ? "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â" : String(state.toolCount);
  $("node-role").textContent = `role: ${state.health?.node_role || "unknown"}`;
  renderVoiceSummary();
}

function renderVoiceSummary() {
  const v = state.voice;
  const parts = [];
  if (v?.enabled === false) parts.push("disabled");
  else {
    if (v?.stt_enabled !== undefined) parts.push(`STT ${v.stt_enabled ? "on" : "off"}`);
    if (v?.tts_enabled !== undefined) parts.push(`TTS ${v.tts_enabled ? "on" : "off"}`);
  }
  const stt = state.capabilities.stt;
  if (stt === false) parts.push("STT endpoint unavailable");
  if (state.lastTts === "unavailable") parts.push("TTS endpoint unavailable");
  const vi = voice?.info;
  if (vi) parts.push(`voice: ${vi.active_engine}${vi.active_engine === "kokoro" ? ` ${vi.active?.kokoro_voice}` : vi.active_engine === "piper" ? ` ${vi.active?.piper_voice}` : ""}`);
  $("voice-summary").textContent = parts.length ? parts.join(" Ãƒâ€šÃ‚Â· ") : "unknown";
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ polling: health + state ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
async function refreshHealth() {
  const r = await api.get("/api/health");
  if (r.ok && r.data) {
    state.health = r.data;
    if (typeof r.data.defcon === "number") state.defcon = r.data.defcon;
    if (r.data.mode) state.mode = r.data.mode;
    if (typeof r.data.tools === "number") state.toolCount = r.data.tools;
    if (r.data.llm) state.llm = r.data.llm;
    document.title = `JARVIS ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${state.health.core || "?"} Ãƒâ€šÃ‚Â· DEFCON ${state.defcon ?? "?"}`;
  } else if (r.unavailable) {
    state.health = null;
  }
  renderHealth();
  renderPosture();
}

async function refreshState() {
  const r = await api.get("/api/state");
  if (r.ok && r.data) {
    const d = r.data;
    if (d.persona) {
      state.persona = { ...state.persona, ...d.persona };
      $("persona-name").textContent = (d.persona.name || "JARVIS").toUpperCase();
    }
    if (typeof d.defcon === "number") state.defcon = d.defcon;
    if (d.mode) state.mode = d.mode;
    if (d.voice) {
      state.voice = d.voice;
      if (typeof d.voice.tts_enabled === "boolean") state.ttsEnabled = d.voice.tts_enabled;
      voice?.setServerListening(!!d.voice.listening);
    }
    if (Array.isArray(d.tools) && d.tools.length) { state.tools = normalizeTools(d.tools); state.toolCount = state.tools.length; renderTools(); }
  }
  renderHealth();
  renderPosture();
  renderVoiceSummary();
  recomputeBusy();
}

async function refreshAll() {
  await Promise.allSettled([refreshHealth(), refreshState(), refreshPanel(currentTab),
    pollMetrics(), voice?.load(), refreshSystems(), syncApprovals()]);
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ panels ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
let currentTab = "tools";
const panelLoaders = {
  tools: loadTools,
  memory: loadMemory,
  tasks: loadTasks,
  alerts: loadAlerts,
  runtime: loadRuntime,
  nodes: loadNodes,
  diagnostics: runDiagnostics,
  settings: loadSettings,
  screen: loadScreen,
  evolve: loadEvolve,
};
function refreshPanel(name) { return panelLoaders[name] ? panelLoaders[name]() : Promise.resolve(); }
let tasksTimer = 0;
let runtimeTimer = 0;
function refreshTasksSoon() { clearTimeout(tasksTimer); tasksTimer = setTimeout(() => refreshPanel("tasks"), 700); }

function listOf(data, ...keys) {
  if (Array.isArray(data)) return data;
  if (data && typeof data === "object") {
    for (const k of keys) if (Array.isArray(data[k])) return data[k];
    if (Array.isArray(data.items)) return data.items;
  }
  return null;
}

/** Tool lists may arrive as strings (names) or full descriptor objects. */
function normalizeTools(arr) {
  return (Array.isArray(arr) ? arr : []).map((t) =>
    typeof t === "string" ? { name: t } : (t && typeof t === "object" ? t : { name: String(t) }));
}

async function loadTools() {
  const box = $("tools-list");
  const r = await api.get("/api/tools");
  if (r.unavailable) { box.replaceChildren(h("p", { class: "empty", text: "Tools endpoint not available (/api/tools returned 404)." })); return; }
  if (!r.ok) { box.replaceChildren(h("p", { class: "empty", text: `Could not load tools: ${r.error || r.status}` })); return; }
  const items = normalizeTools(listOf(r.data, "tools"));
  state.tools = items;
  state.toolCount = items.length;
  $("tool-count").textContent = String(items.length);
  renderTools();
}

function renderTools() {
  const box = $("tools-list");
  const q = ($("tools-search").value || "").toLowerCase().trim();
  const items = state.tools.filter((t) => {
    const name = (t.name || t.tool || "").toLowerCase();
    const summary = (t.summary || "").toLowerCase();
    return !q || name.includes(q) || summary.includes(q);
  });
  if (!items.length) { box.replaceChildren(h("p", { class: "empty", text: state.tools.length ? "No tools match that filter." : "No tools registered." })); return; }
  box.replaceChildren(...items.map((t) => {
    const name = t.name || t.tool || "?";
    const se = String(t.side_effect || "none").toLowerCase();
    const params = Array.isArray(t.params) ? t.params : [];
    return h("div", { class: "card" },
      h("div", { class: "card-title" }, h("span", { text: name }),
        h("span", { class: `pill se-${se.replace(/[^a-z_]/g, "")}`, text: se })),
      h("div", { class: "card-sub" }, t.category || "general", " Ãƒâ€šÃ‚Â· v", t.version || "1"),
      t.summary ? h("div", { class: "card-desc", text: t.summary }) : null,
      params.length ? h("div", { class: "card-meta", text: "params: " + params.map((p) => `${p.name}${p.required ? "*" : ""}:${p.type || "?"}`).join(", ") }) : null,
    );
  }));
}

async function loadMemory() {
  const box = $("memory-list");
  box.replaceChildren(h("p", { class: "empty", text: "LoadingÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦" }));
  const r = await api.get("/api/memory");
  if (r.unavailable) { box.replaceChildren(h("p", { class: "empty", text: "Memory endpoint not available (/api/memory returned 404)." })); return; }
  if (!r.ok) { box.replaceChildren(h("p", { class: "empty", text: `Could not load memory: ${r.error || r.status}` })); return; }
  const items = listOf(r.data, "memories", "results") || [];
  if (!items.length) { box.replaceChildren(h("p", { class: "empty", text: "No memory items." })); return; }
  box.replaceChildren(...items.slice(0, 200).map((m) => h("div", { class: "card" },
    h("div", { class: "card-title" }, h("span", { class: "pill", text: m.kind || "fact" }),
      h("span", { class: "card-meta", text: `conf ${m.confidence ?? "?"} Ãƒâ€šÃ‚Â· ${fmtTime(m.created_at)}` })),
    h("div", { class: "card-desc", text: m.text || "(no text)" }),
    m.source ? h("div", { class: "card-meta", text: `source: ${m.source}` }) : null,
  )));
}

async function loadTasks() {
  const box = $("tasks-list");
  const r = await api.get("/api/tasks");
  if (r.unavailable) { box.replaceChildren(h("p", { class: "empty", text: "Tasks endpoint not available (/api/tasks returned 404)." })); return; }
  if (!r.ok) { box.replaceChildren(h("p", { class: "empty", text: `Could not load tasks: ${r.error || r.status}` })); return; }
  const items = listOf(r.data, "tasks") || [];
  for (const t of items) if (t.id) state.activeTasks.set(t.id, t.status || "?");
  recomputeBusy();
  if (!items.length) { box.replaceChildren(h("p", { class: "empty", text: "No tasks." })); return; }
  box.replaceChildren(...items.slice(0, 200).map((t) => {
    const st = String(t.status || "?");
    return h("div", { class: "card" },
      h("div", { class: "card-title" }, h("span", { class: `status-pill ${statusClass(st)}`, text: st }),
        h("span", { class: "card-meta", text: short(t.id) + " Ãƒâ€šÃ‚Â· " + ago(t.updated_at || t.created_at) })),
      h("div", { class: "card-desc", text: t.request || t.goal || "(no request)" }),
      t.error ? h("div", { class: "card-meta", text: "error: " + t.error }) : null,
      h("div", { class: "card-meta", text: `source: ${t.source || "?"}` }),
    );
  }));
}
function statusClass(s) {
  if (s === "succeeded") return "se-none";
  if (s === "failed" || s === "blocked") return "se-destructive";
  if (s === "awaiting-authorization") return "se-local_write";
  if (s === "running" || s === "verifying") return "se-network";
  return "";
}

async function loadAlerts() {
  const box = $("alerts-list");
  const r = await api.get("/api/alerts");
  if (r.unavailable) { box.replaceChildren(h("p", { class: "empty", text: "Alerts endpoint not available (/api/alerts returned 404)." })); return; }
  if (!r.ok) { box.replaceChildren(h("p", { class: "empty", text: `Could not load alerts: ${r.error || r.status}` })); return; }
  const items = listOf(r.data, "alerts") || [];
  if (!items.length) { box.replaceChildren(h("p", { class: "empty", text: "No alerts." })); return; }
  box.replaceChildren(...items.slice(0, 200).map((a) => h("div", { class: `card ${a.severity === "critical" ? "se-destructive" : ""}` },
    h("div", { class: "card-title" }, h("span", { class: "pill", text: a.severity || "info" }),
      h("span", { class: "card-meta", text: `${a.source || "?"} Ãƒâ€šÃ‚Â· ${fmtTime(a.created_at)}` })),
    h("div", { class: "card-desc", text: a.title || "(untitled)" }),
    a.body ? h("div", { class: "card-meta", text: a.body }) : null,
    a.acknowledged ? h("div", { class: "card-meta", text: "acknowledged" }) : null,
  )));
}

async function loadNodes() {
  const box = $("nodes-list");
  const r = await api.get("/api/nodes");
  if (r.unavailable) { box.replaceChildren(h("p", { class: "empty", text: "Nodes endpoint not available (/api/nodes returned 404)." })); return; }
  if (!r.ok) { box.replaceChildren(h("p", { class: "empty", text: `Could not load nodes: ${r.error || r.status}` })); return; }
  const items = listOf(r.data, "nodes") || [];
  if (!items.length) { box.replaceChildren(h("p", { class: "empty", text: "No nodes registered." })); return; }
  box.replaceChildren(...items.map((n) => h("div", { class: "card" },
    h("div", { class: "card-title" }, h("span", { text: n.name || n.id || "?" }),
      h("span", { class: `status-pill ${String(n.status).includes("up") || n.status === "online" ? "se-none" : n.status === "offline" ? "se-destructive" : ""}`, text: n.status || "unknown" })),
    h("div", { class: "card-meta", text: `${n.kind || "node"} Ãƒâ€šÃ‚Â· ${n.base_url || "no url"} Ãƒâ€šÃ‚Â· last seen ${ago(n.last_seen)}` }),
    h("div", { class: "card-meta node-telemetry", "data-node-telemetry": n.id || "",
              text: n.status === "online" ? "telemetryÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦" : "offline ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â no telemetry" }),
    n.capabilities ? h("div", { class: "card-meta", text: "caps: " + (typeof n.capabilities === "string" ? n.capabilities : JSON.stringify(n.capabilities)) }) : null,
  )));
  for (const n of items) {
    if (n.status === "online" && n.id) hydrateNodeTelemetry(n.id).catch(() => {});
  }
}

/** Pull real sysinfo from a node over its tunnel and render it on the card. */
async function hydrateNodeTelemetry(id) {
  const el = document.querySelector(`[data-node-telemetry="${id}"]`);
  if (!el) return;
  const r = await api.get(`/api/nodes/${encodeURIComponent(id)}/sysinfo`);
  if (!r.ok) {
    el.textContent = `telemetry unavailable${r.error ? ": " + r.error : ""}`;
    return;
  }
  const payload = r.data || {};
  const d = payload.data || payload.result || payload;
  if (!d || typeof d !== "object") { el.textContent = "telemetry unavailable"; return; }
  const mem = d.memory || {};
  const disk = d.disk || {};
  const bits = [];
  if (typeof d.cpu_count === "number") bits.push(`cpu ${d.cpu_count} cores`);
  if (typeof mem.used_pct === "number") {
    const used = typeof mem.used_mb === "number" ? `${Math.round(mem.used_mb)} MB` : "";
    const total = typeof mem.total_mb === "number" ? ` / ${Math.round(mem.total_mb)} MB` : "";
    bits.push(`mem ${mem.used_pct}%${used ? ` (${used}${total})` : ""}`);
  }
  if (typeof disk.used_pct === "number") {
    bits.push(`disk ${disk.used_pct}%${typeof disk.total_gb === "number" ? ` of ${disk.total_gb} GB` : ""}`);
  }
  if (Array.isArray(d.load)) bits.push(`load ${d.load.map((x) => Number(x).toFixed(2)).join(" ")}`);
  if (typeof d.uptime_s === "number") bits.push(`up ${(d.uptime_s / 3600).toFixed(0)}h`);
  el.textContent = bits.length ? bits.join(" Ãƒâ€šÃ‚Â· ") : "telemetry returned no readings";
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ operations: persistent runtime ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function runtimeState(d) {
  if (!d || d.enabled === false) return { text: "disabled", cls: "se-destructive" };
  if (d.running && d.owner) return { text: "online", cls: "se-none" };
  if (d.running) return { text: "standby", cls: "se-local_write" };
  return { text: "stopped", cls: "se-destructive" };
}

/** Fill a list box, always rendering a real empty state. */
function fillRuntimeList(id, items, empty, render) {
  const box = $(id);
  if (!box) return;
  if (!items.length) { box.replaceChildren(h("p", { class: "empty", text: empty })); return; }
  box.replaceChildren(...items.slice(0, 100).map(render));
}

async function loadRuntime() {
  const box = $("runtime-status");
  const [r, goals, ops] = await Promise.all([
    api.get("/api/runtime"), api.get("/api/goals?limit=10"), api.get("/api/ops"),
  ]);
  if (r.unavailable) {
    box.replaceChildren(h("p", { class: "empty", text: "Runtime not available (/api/runtime returned 404)." }));
    return;
  }
  if (!r.ok) {
    box.replaceChildren(h("p", { class: "empty", text: `Could not load runtime: ${r.error || r.status}` }));
    return;
  }
  renderRuntime(r.data || {});
  renderGoals(goals.ok ? (goals.data?.goals || []) : null);
  renderOps(ops.ok ? ops.data : null);
}

function renderGoals(goals) {
  if (goals === null) {
    fillRuntimeList("runtime-goals", [], "Goals endpoint unavailable.", () => null);
    return;
  }
  fillRuntimeList("runtime-goals", goals,
    "No goals yet. Ask for something that needs several steps.", (g) =>
      h("div", { class: "card" },
        h("div", { class: "card-title" },
          h("span", { class: `status-pill ${statusClass(g.status)}`, text: g.status || "?" }),
          h("span", { class: "card-meta", text: `${g.steps_done ?? 0}/${g.steps_total ?? 0} steps Ãƒâ€šÃ‚Â· ${ago(g.updated_at || g.created_at)}` })),
        h("div", { class: "card-desc", text: g.request || "(no request)" }),
        g.error ? h("div", { class: "card-meta", text: g.error }) : null));
}

function renderOps(snapshot) {
  const checks = snapshot && Array.isArray(snapshot.checks) ? snapshot.checks : [];
  if (!checks.length) {
    fillRuntimeList("runtime-ops", [], "No self-check has run yet.", () => null);
    return;
  }
  $("runtime-ops").replaceChildren(...checks.map((c) =>
    h("div", { class: "card" },
      h("div", { class: "card-title" },
        h("span", { class: `status-pill ${c.ok ? "se-none" : "se-destructive"}`, text: c.ok ? "ok" : "fail" }),
        h("span", { text: c.name || "?" })),
      h("div", { class: "card-meta", text: c.detail || "" }),
      c.repair ? h("div", { class: "card-meta", text: "fix: " + c.repair }) : null)));
}

function renderRuntime(d) {
  const st = runtimeState(d);
  const flight = d.in_flight || {};
  const rows = [
    ["State", st.text],
    ["Owner", d.owner_id ? short(d.owner_id) : "?"],
    ["Lease", d.owner ? "held by this Core" : (d.lease && d.lease.owner ? "held by " + short(d.lease.owner) : "free")],
    ["Ticks", String(d.ticks ?? 0)],
    ["Last tick", d.last_tick ? ago(d.last_tick) : "never"],
    ["In flight", `${flight.reactions ?? 0} reaction(s) Ãƒâ€šÃ‚Â· ${flight.scheduled ?? 0} scheduled`],
    ["Sessions", String(d.sessions ?? 0)],
    ["Last error", d.last_error || "none"],
  ];
  $("runtime-status").replaceChildren(
    h("div", { class: "runtime-state" },
      h("span", { class: `status-pill ${st.cls}`, text: st.text }),
      h("span", { class: "card-meta", text: `tick every ${d.tick_s ?? "?"}s Ãƒâ€šÃ‚Â· catch-up ${d.catch_up_s ?? "?"}s` })),
    ...rows.map(([k, v]) => h("div", { class: "kv-row" },
      h("span", { class: "kv-k", text: k }), h("span", { class: "kv-v", text: v }))),
  );

  const sched = d.scheduler || {};
  fillRuntimeList("runtime-jobs", Array.isArray(sched.jobs) ? sched.jobs : [],
    "No scheduled jobs. Add one with the scheduler.* tools.", (j) =>
      h("div", { class: "card" },
        h("div", { class: "card-title" },
          h("span", { class: `status-pill ${j.enabled ? "se-none" : "se-destructive"}`, text: j.enabled ? "enabled" : "disabled" }),
          h("span", { class: "card-meta", text: `when ${j.when} Ãƒâ€šÃ‚Â· next ${j.next_run ? fmtTime(j.next_run) : "ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â"}` })),
        h("div", { class: "card-desc", text: j.prompt || "(no prompt)" })));

  fillRuntimeList("runtime-runs", Array.isArray(sched.runs) ? sched.runs : [],
    "No runs yet.", (r) =>
      h("div", { class: "card" },
        h("div", { class: "card-title" },
          h("span", { class: `status-pill ${statusClass(r.status)}`, text: r.status || "?" }),
          h("span", { class: "card-meta", text: `due ${r.due_key || "?"} Ãƒâ€šÃ‚Â· ${ago(r.started_at || r.ended_at)}` })),
        h("div", { class: "card-meta", text: `job ${short(r.job_id)}${r.task_id ? " Ãƒâ€šÃ‚Â· task " + short(r.task_id) : ""}` }),
        r.error ? h("div", { class: "card-meta", text: r.error }) : null));

  const events = d.events || {};
  fillRuntimeList("runtime-rules", Array.isArray(events.rules) ? events.rules : [],
    "No event rules. Add one at the console: jarvis runtime rules add ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦", (r) => {
      const a = r.action || {};
      const what = a.kind === "tool" ? `tool: ${a.tool}`
        : a.kind === "notify" ? `alert: ${a.title}`
        : `request: ${String(a.prompt || "").slice(0, 90)}`;
      return h("div", { class: "card" },
        h("div", { class: "card-title" },
          h("span", { class: `status-pill ${r.enabled ? "se-none" : "se-destructive"}`, text: r.enabled ? "on" : "off" }),
          h("span", { text: r.name || "?" }),
          h("span", { class: "card-meta", text: r.topic || "" })),
        h("div", { class: "card-desc", text: what }),
        r.condition ? h("div", { class: "card-meta", text: "when " + JSON.stringify(r.condition) }) : null);
    });

  fillRuntimeList("runtime-events", Array.isArray(events.events_recent) ? events.events_recent : [],
    "No events yet.", (ev) =>
      h("div", { class: "card" },
        h("div", { class: "card-title" },
          h("span", { class: "pill", text: ev.source || "?" }),
          h("span", { text: ev.topic || "?" }),
          h("span", { class: "card-meta", text: ago(ev.ts) })),
        h("div", { class: "card-meta", text: JSON.stringify(ev.data || {}).slice(0, 240) })));
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ subsystem strip ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function setChip(id, state, text) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle("is-ok", state === "ok");
  el.classList.toggle("is-warn", state === "warn");
  el.classList.toggle("is-bad", state === "bad");
  const label = el.querySelector(".sys-state");
  if (label) label.textContent = text;
}

async function refreshSystems() {
  const [health, runtime, sandbox, audit] = await Promise.all([
    api.get("/api/health"), api.get("/api/runtime"), api.get("/api/sandbox"),
    api.get("/api/audit/verify"),
  ]);
  const coreReady = health.ok && health.data && health.data.core === "ready";
  setChip("sys-core", coreReady ? "ok" : "bad", coreReady ? "ready" : "down");

  const rt = runtime.ok ? runtime.data : null;
  if (!rt) setChip("sys-runtime", "bad", "unavailable");
  else if (!rt.enabled) setChip("sys-runtime", "warn", "off");
  else if (rt.running && rt.owner) setChip("sys-runtime", "ok", "online");
  else if (rt.running) setChip("sys-runtime", "warn", "standby");
  else setChip("sys-runtime", "bad", "stopped");

  const sb = sandbox.ok ? sandbox.data : null;
  if (!sb) setChip("sys-sandbox", "bad", "unavailable");
  else if (sb.contained) setChip("sys-sandbox", "ok", sb.active_tier || "on");
  else setChip("sys-sandbox", "warn", sb.active_tier || "none");

  const au = audit.ok ? audit.data : null;
  if (!au) setChip("sys-audit", "bad", "unavailable");
  else setChip("sys-audit", au.ok ? "ok" : "bad", au.ok ? "intact" : "BROKEN");
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ settings ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
async function loadSettings() {
  const r = await api.get("/api/settings");
  if (r.unavailable) {
    $("settings-status").textContent = "Settings endpoint not available (/api/settings returned 404). Posture controls still write via /api/settings.";
    return;
  }
  if (r.ok && r.data && typeof r.data === "object") {
    const s = r.data.settings || r.data;
    if (s.mode) { state.mode = s.mode; $("set-mode").value = s.mode; $("mode-select").value = s.mode; }
    if (typeof s.defcon === "number") { state.defcon = s.defcon; $("set-defcon").value = String(s.defcon); }
    const v = s.voice || {};
    if (typeof v.enabled === "boolean") $("set-voice-enabled").checked = v.enabled;
    if (typeof v.tts_enabled === "boolean") $("set-tts-enabled").checked = v.tts_enabled;
    if (s.persona?.name) $("set-persona-name").value = s.persona.name;
    if (s.policy?.mode) { state.mode = s.policy.mode; $("set-mode").value = s.policy.mode; }
    $("settings-status").textContent = "loaded";
  } else if (!r.ok) {
    $("settings-status").textContent = `could not load settings: ${r.error || r.status}`;
  }
  renderPosture();
}

/** Mode goes through /api/settings ("policy.mode"); DEFCON has its own endpoint.
    A 200 whose key landed in `rejected` is reported as a failure, not a success. */
async function applyPosture({ mode, defcon }) {
  if (mode) {
    const r = await api.post("/api/settings", { "policy.mode": mode });
    if (r.ok && r.data?.rejected && r.data.rejected["policy.mode"]) {
      return { ...r, ok: false, error: `rejected: ${r.data.rejected["policy.mode"]}` };
    }
    return r;
  }
  return api.post("/api/defcon", { level: Number(defcon) });
}

async function onModeChange(value) {
  const r = await applyPosture({ mode: value });
  if (r.ok || r.status === 0) { /* optimistic only if accepted */ }
  if (!r.ok && r.status !== 0) {
    toast(`Could not set mode: ${r.error || r.status}`, "err");
  } else if (r.status === 0) {
    toast("Mode change not confirmed ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â no response from server.", "warn");
  }
  await refreshState();
}

async function onDefconChange(value) {
  const r = await applyPosture({ defcon: value });
  if (!r.ok && r.status !== 0) toast(`Could not set DEFCON: ${r.error || r.status}`, "err");
  else if (r.status === 0) toast("DEFCON change not confirmed ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â no response from server.", "warn");
  else { state.defcon = Number(value); renderPosture(); updateReactor(); }
  await refreshHealth();
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ security unlock ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
async function doUnlock() {
  const status = $("unlock-status");
  const token = $("set-api-token").value.trim();
  if (!token) { status.textContent = "Paste the API token first."; return; }
  status.textContent = "unlockingÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦";
  const r = await api.unlock(token);
  if (r.ok) { status.textContent = "Unlocked ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â approvals & settings enabled."; $("set-api-token").value = ""; }
  else status.textContent = `Unlock failed: ${r.error || r.status}`;
}

async function doLock() {
  const status = $("unlock-status");
  const r = await api.post("/api/auth/logout");
  status.textContent = r.ok ? "Locked." : `Lock failed: ${r.error || r.status}`;
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ approvals ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
/** Reconcile the approval modal with the server (survives reloads and reconnects). */
async function syncApprovals() {
  const r = await api.get("/api/approvals");
  if (!r.ok) return;
  const pending = (r.data && r.data.pending) || [];
  const next = new Map();
  for (const a of pending) {
    next.set(a.id, {
      approval_id: a.id, tool: a.tool, args: a.args,
      side_effect: a.side_effect, reason: a.reason,
    });
  }
  state.approvals = next;
  renderApprovals();
  $("approval-modal").hidden = next.size === 0;
}

function addApproval(d) {
  if (!d || !d.approval_id) return;
  state.approvals.set(d.approval_id, d);
  renderApprovals();
  $("approval-modal").hidden = false;
}
function removeApproval(id) {
  if (!id) return;
  state.approvals.delete(id);
  renderApprovals();
  if (!state.approvals.size) $("approval-modal").hidden = true;
}

function renderApprovals() {
  const box = $("approval-list");
  const items = [...state.approvals.values()];
  if (!items.length) { box.replaceChildren(h("p", { class: "empty", text: "No pending approvals." })); return; }
  box.replaceChildren(...items.map((d) => {
    const se = String(d.side_effect || "none").toLowerCase();
    const argsText = d.args && Object.keys(d.args).length ? JSON.stringify(d.args, null, 2) : "(no arguments)";
    return h("div", { class: "approval-card" },
      h("div", { class: "ap-row" }, h("span", { class: "ap-k", text: "Tool" }), h("span", { class: "ap-v", text: d.tool || "?" })),
      h("div", { class: "ap-row" }, h("span", { class: "ap-k", text: "Side effect" }), h("span", { class: `ap-v pill se-${se.replace(/[^a-z_]/g, "")}`, text: se })),
      h("div", { class: "ap-row" }, h("span", { class: "ap-k", text: "Task" }), h("span", { class: "ap-v", text: short(d.task_id) })),
      h("div", { class: "approval-reason", text: d.reason ? `Reason: ${d.reason}` : "Reason: not given" }),
      h("pre", { class: "approval-args", text: argsText }),
      h("div", { class: "approval-actions" },
        h("button", { class: "btn btn-primary", onclick: () => decide(d.approval_id, "allow") }, "Allow once"),
        h("button", { class: "btn", onclick: () => decide(d.approval_id, "allow", 3600) }, "Allow for 1 hour"),
        h("button", { class: "btn", onclick: () => decide(d.approval_id, "allow", 0, true) }, "Allow for this session"),
        h("button", { class: "btn", onclick: () => decide(d.approval_id, "deny") }, "Deny"),
      ),
    );
  }));
}

async function decide(id, decision, rememberS, rememberSession) {
  const body = { decision };
  if (rememberS) body.remember_s = rememberS;
  if (rememberSession) body.remember_session = true;
  const r = await api.post(`/api/approvals/${encodeURIComponent(id)}`, body);
  if (r.unavailable) { toast(`Approvals endpoint not available (404).`, "err"); return; }
  if (!r.ok) { toast(`Approval failed: ${r.error || r.status}`, "err"); return; }
  let label = "granted once";
  if (rememberSession) label = "granted for this session";
  else if (rememberS) label = "granted for 1 hour";
  if (decision === "deny") label = "denied";
  toast(`Approval ${label}`, decision === "deny" ? "warn" : "ok");
  removeApproval(id);
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ chat (SSE) ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function appendMessage(kind, text, who) {
  const t = $("transcript");
  t.querySelector(".transcript-empty")?.remove();
  const node = h("div", { class: `msg msg-${kind}` });
  if (who) node.append(h("span", { class: "msg-who", text: who }));
  const body = h("span", { class: "msg-body", text: text || "" });
  node.append(body);
  t.append(node);
  t.scrollTop = t.scrollHeight;
  return { node, body };
}

async function sendMessage(text, { source = "text" } = {}) {
  text = (text || "").trim();
  if (!text || state.streaming) return;
  voice?.queue?.unlock?.();        // we are inside the user's click/keypress
  const speaking = canSpeak() ? voice?.beginReply() : null;
  appendMessage("owner", text, "You");
  $("chat-input").value = "";
  autoGrow();

  state.streaming = true;
  state.abort = new AbortController();
  recomputeBusy();

  const jm = appendMessage("jarvis", "", state.persona.name || "JARVIS");
  jm.body.textContent = "";
  const cursor = h("span", { class: "cursor", "aria-hidden": "true" });
  jm.body.after(cursor);
  let firstToken = true;

  const finish = (note, cls) => {
    cursor.remove();
    if (note) appendMessage("system", note);
    state.streaming = false;
    state.abort = null;
    recomputeBusy();
  };

  const res = await streamChat(api, {
    message: text,
    sessionId: "default",
    source,
    signal: state.abort.signal,
    onFrame: (frame) => {
      const kind = frame.kind || frame.type;
      if (kind === "token" || kind === "delta") {
        if (firstToken) { jm.body.textContent = ""; firstToken = false; }
        const piece = frame.text ?? frame.token ?? frame.content ?? "";
        jm.body.textContent += piece;
        speaking?.push(piece);   // first sentence is spoken while the rest streams
        $("transcript").scrollTop = $("transcript").scrollHeight;
      } else if (kind === "ack") {
        if (frame.text && firstToken) { jm.body.textContent = frame.text; firstToken = false; }
      } else if (kind === "tool_call") {
        appendMessage("tool", `ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ ${frame.tool || frame.name || "tool"}(${briefArgs(frame.args)})`);
      } else if (kind === "tool_result") {
        appendMessage("tool", `ÃƒÂ¢Ã¢â‚¬Â Ã‚Â ${frame.tool || frame.name || "tool"} ${frame.ok === false ? "FAILED" : "ok"}${frame.error ? `: ${frame.error}` : ""}`);
      } else if (kind === "final") {
        const finalText = frame.text ?? frame.content ?? frame.message;
        if (typeof finalText === "string" && finalText) jm.body.textContent = finalText;
      } else if (kind === "error") {
        jm.body.classList.add("msg-error-text");
        if (!jm.body.textContent) jm.body.textContent = `error: ${frame.error || frame.message || "stream error"}`;
        state.errorUntil = Date.now() + 20000;
        updateReactor();
      }
    },
  });

  if (res.aborted) { finish("(cancelled)"); return; }
  if (!res.ok) {
    cursor.remove();
    jm.node.remove();
    if (res.unavailable) {
      appendMessage("system", "Chat endpoint not available (/api/chat returned 404). Nothing was sent.");
    } else {
      appendMessage("system", `Chat failed: ${res.error || res.status}. ${res.network ? "Server unreachable." : ""}`);
    }
    state.streaming = false;
    state.abort = null;
    recomputeBusy();
    return;
  }
  cursor.remove();
  const finalText = jm.body.textContent.trim();
  if (!finalText) { jm.node.remove(); appendMessage("system", "No reply text received."); }
  else speaking?.finish(finalText);
  state.streaming = false;
  state.abort = null;
  recomputeBusy();
}

function canSpeak() {
  return !state.muted && state.ttsEnabled !== false && state.voice?.enabled !== false;
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ push-to-talk + conversation: see voice.js ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
const startPTT = () => voice.startPTT();
const stopPTT = () => voice.stopPTT();

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ diagnostics ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
async function runDiagnostics() {
  const box = $("diag-list");
  box.replaceChildren(h("p", { class: "empty", text: "Running checksÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦" }));
  const rows = [];

  rows.push(diagRow("WebSocket /ws", state.connected ? "available" : (state.wsStatus === "reconnecting" ? "reconnecting" : "unavailable"),
    state.connected ? "link open" : `status: ${state.wsStatus}`));

  const health = await api.get("/api/health");
  rows.push(diagRow("REST /api/health", health.ok ? "available" : health.unavailable ? "unavailable" : "error",
    health.ok ? `core: ${health.data?.core ?? "?"}` : (health.unavailable ? "404 ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â endpoint not implemented" : `status ${health.status}`)));

  const llm = state.llm || health.data?.llm;
  if (llm && typeof llm === "object" && Object.keys(llm).length) {
    rows.push(diagRow("LLM provider", llm.ok ? "available" : "unavailable",
      llm.ok ? `${llm.provider || "?"} ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${(llm.models || []).length} model(s)` : (llm.error || "not reachable")));
  } else {
    rows.push(diagRow("LLM provider", "unknown", "no health data yet"));
  }

  const st = await api.get("/api/state");
  rows.push(diagRow("REST /api/state", st.ok ? "available" : st.unavailable ? "unavailable" : "error",
    st.ok ? `mode ${st.data?.mode ?? "?"} Ãƒâ€šÃ‚Â· defcon ${st.data?.defcon ?? "?"}` : (st.unavailable ? "404 ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â endpoint not implemented" : `status ${st.status}`)));

  // STT capability probe (POST with empty body ÃƒÂ¢Ã¢â‚¬Â Ã¢â‚¬â„¢ 404 means absent, 422 means present)
  const sttProbe = await api.probe("/api/voice/transcribe", "POST");
  if (sttProbe.unavailable) state.capabilities.stt = false;
  else if (sttProbe.status && sttProbe.status < 500) state.capabilities.stt = true;
  rows.push(diagRow("Speech-to-text (/api/voice/transcribe)",
    sttProbe.unavailable ? "unavailable" : state.capabilities.stt ? "available" : "unknown",
    sttProbe.unavailable ? "404 ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â optional voice stack not installed" :
    !navigator.mediaDevices?.getUserMedia ? "server present; browser has no microphone API" :
    state.capabilities.stt ? "endpoint present" : `probe status ${sttProbe.status}`));

  // TTS capability ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â from the server's own engine probes (/api/voice/voices)
  const vi = await voice.load();
  if (!vi) {
    rows.push(diagRow("Text-to-speech (/api/tts)", "unavailable", "voice routes not reachable"));
  } else {
    const active = vi.active_engine;
    const sel = vi.active?.engine;
    const e = vi.engines?.[sel] || {};
    rows.push(diagRow("Text-to-speech (/api/tts)", active === "null" ? "unavailable" : "available",
      active === "null" ? `no speech engine: ${e.reason || "none available"}` :
      `${active}${active !== sel ? ` (fallback; ${sel}: ${e.reason || "unavailable"})` : ""} Ãƒâ€šÃ‚Â· effect ${vi.active?.effect} ${Math.round((vi.active?.effect_strength || 0) * 100)}% Ãƒâ€šÃ‚Â· output ${vi.active?.output}`));
    rows.push(diagRow("Server wake word", vi.wake?.listening ? "available" : "unavailable",
      vi.wake?.listening ? `listening for "${vi.wake.model}"` : (vi.wake?.enabled ? "enabled in config but not listening (see voice status)" : "off (config.voice.wake_enabled=false)")));
  }

  const vcfg = state.voice || {};
  rows.push(diagRow("Voice config",
    vcfg.enabled === false ? "unavailable" : vcfg.enabled ? "available" : "unknown",
    vcfg.enabled === false ? "disabled in settings" : `stt: ${vcfg.stt_enabled ?? "?"} Ãƒâ€šÃ‚Â· tts: ${vcfg.tts_enabled ?? "?"} Ãƒâ€šÃ‚Â· wake: ${vcfg.wake_enabled ?? "?"}`));
  rows.push(diagRow("Browser audio capture", window.AudioContext || window.webkitAudioContext ? "available" : "unavailable",
    window.AudioWorkletNode ? "AudioWorklet present (used for push-to-talk and conversation)" : "AudioWorklet missing ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ScriptProcessor fallback"));

  rows.push(diagRow("Browser MediaRecorder", state.capabilities.mediaRecorder ? "available" : "unavailable",
    state.capabilities.mediaRecorder ? "client support present" : "client cannot record audio"));
  rows.push(diagRow("Microphone API", navigator.mediaDevices?.getUserMedia ? "available" : "unavailable",
    navigator.mediaDevices?.getUserMedia ? "getUserMedia present (permission asked on first use)" : "no mediaDevices in this browser"));

  rows.push(diagRow("Tool registry", state.toolCount === null ? "unknown" : (state.toolCount > 0 ? "available" : "unavailable"),
    state.toolCount === null ? "not loaded" : `${state.toolCount} tool(s)`));

  rows.push(diagRow("Metrics (/api/metrics)",
    Object.keys(state.metricUnavailable || {}).length === 0 || !Object.values(state.metricUnavailable).every(Boolean) ? "available" : "unavailable",
    Object.values(state.metricUnavailable || {}).every(Boolean) && Object.keys(state.metricUnavailable).length
      ? "404 ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â monitoring service not running" : "polling per metric name"));

  box.replaceChildren(...rows);
  renderVoiceSummary();
}

function diagRow(name, status, reason) {
  const cls = status === "available" ? "se-none" : status === "unavailable" ? "se-destructive" : status === "reconnecting" ? "se-local_write" : "";
  return h("div", { class: "card" },
    h("div", { class: "card-title" }, h("span", { text: name }), h("span", { class: `pill ${cls}`, text: status })),
    h("div", { class: "card-desc", text: reason }),
  );
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ command palette ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
const ACTIONS = [
  { kind: "action", name: "Tab: Tools", run: () => selectTab("tools") },
  { kind: "action", name: "Tab: Memory", run: () => selectTab("memory") },
  { kind: "action", name: "Tab: Tasks", run: () => selectTab("tasks") },
  { kind: "action", name: "Tab: Alerts", run: () => selectTab("alerts") },
  { kind: "action", name: "Tab: Nodes", run: () => selectTab("nodes") },
  { kind: "action", name: "Tab: Diagnostics", run: () => selectTab("diagnostics") },
  { kind: "action", name: "Tab: Settings", run: () => selectTab("settings") },
  { kind: "action", name: "Toggle voice mute", run: () => toggleMute() },
  { kind: "action", name: "Conversation mode: on/off (opens microphone)", run: () => voice.toggleConversation() },
  { kind: "action", name: "Stop all microphones", run: () => voice.stopAllMics() },
  { kind: "action", name: "Stop speaking", run: () => voice.stopSpeaking() },
  { kind: "action", name: "Audition voice", run: () => { selectTab("settings"); voice.audition(); } },
  { kind: "action", name: "Set mode: ask", run: () => onModeChange("ask") },
  { kind: "action", name: "Set mode: autonomous", run: () => onModeChange("autonomous") },
  { kind: "action", name: "Set mode: readonly", run: () => onModeChange("readonly") },
  { kind: "action", name: "DEFCON 1 (lockdown)", run: () => onDefconChange(1) },
  { kind: "action", name: "DEFCON 2", run: () => onDefconChange(2) },
  { kind: "action", name: "DEFCON 3", run: () => onDefconChange(3) },
  { kind: "action", name: "DEFCON 4", run: () => onDefconChange(4) },
  { kind: "action", name: "DEFCON 5 (peace)", run: () => onDefconChange(5) },
  { kind: "action", name: "Clear activity feed", run: () => { state.feed = []; renderFeed(); } },
  { kind: "action", name: "Reconnect WebSocket", run: () => socket.connect() },
  { kind: "action", name: "Re-run diagnostics", run: () => { selectTab("diagnostics"); runDiagnostics(); } },
];

let paletteItems = [];
let paletteSel = 0;

function openPalette() {
  $("palette-modal").hidden = false;
  $("palette-input").value = "";
  buildPalette("");
  $("palette-input").focus();
}
function closePalette() { $("palette-modal").hidden = true; }

function buildPalette(q) {
  const toolItems = (state.tools || []).map((t) => ({
    kind: "tool",
    name: t.name || t.tool,
    hint: t.side_effect || "",
    run: () => { selectTab("tools"); $("tools-search").value = t.name || t.tool; renderTools(); addChatText(`${t.name || t.tool} `); },
  }));
  const all = [...ACTIONS, ...toolItems];
  paletteItems = q ? fuzzyRank(all, q).slice(0, 40) : all.slice(0, 40);
  paletteSel = 0;
  renderPalette();
}

function renderPalette() {
  const box = $("palette-results");
  if (!paletteItems.length) { box.replaceChildren(h("li", { class: "empty", text: "No matches." })); return; }
  box.replaceChildren(...paletteItems.map((it, i) => h("li", {
    class: `palette-item${i === paletteSel ? " is-sel" : ""}`,
    role: "option",
    "aria-selected": i === paletteSel ? "true" : "false",
    onclick: () => runPalette(i),
    onmouseenter: () => { paletteSel = i; },
  },
    h("span", { text: it.name }),
    h("span", { class: "palette-kind", text: it.kind === "tool" ? `tool Ãƒâ€šÃ‚Â· ${it.hint || "none"}` : "action" }),
  )));
  box.querySelector(".is-sel")?.scrollIntoView({ block: "nearest" });
}

function runPalette(i) {
  const it = paletteItems[i];
  if (!it) return;
  closePalette();
  it.run();
}

function addChatText(text) {
  const input = $("chat-input");
  input.value = text;
  input.focus();
  autoGrow();
}

function fuzzyRank(items, q) {
  const needle = q.toLowerCase();
  const scored = [];
  for (const it of items) {
    const hay = it.name.toLowerCase();
    const idx = hay.indexOf(needle);
    let score = 0;
    if (idx === 0) score = 100;
    else if (idx > 0) score = 70 - Math.min(idx, 40);
    else if (subsequence(needle, hay)) score = 30;
    else continue;
    scored.push({ it, score });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.map((s) => s.it);
}
function subsequence(needle, hay) {
  let i = 0;
  for (const ch of hay) if (ch === needle[i]) i++;
  return i === needle.length;
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ tabs ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ live screen ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function statRow(label, value, cls) {
  return h("div", { class: "kv-row" },
    h("span", { class: "kv-k", text: label }),
    h("span", { class: `kv-v ${cls || ""}`, text: String(value) }));
}

async function loadScreen() {
  const box = $("screen-status");
  const health = await api.get("/api/screen/health");
  if (health.unavailable) {
    box.replaceChildren(h("p", { class: "empty", text: "Screen endpoint not available - deploy the Core update." }));
    return;
  }
  if (!health.ok) {
    const data = health.data || {};
    box.replaceChildren(h("p", { class: "empty",
      text: `Reader offline: ${data.error || health.error || health.status}. Start it with 'capture on' in PowerShell.` }));
    $("screen-text").textContent = "Not loaded.";
    $("screen-frame").hidden = true;
    return;
  }
  const data = health.data || {};
  const capturing = !!data.capturing;
  box.replaceChildren(
    statRow("Capture", capturing ? "on" : "off", capturing ? "se-none" : "se-destructive"),
    statRow("Samples", data.samples ?? "?"),
    statRow("Errors", data.errors ?? "?"),
    statRow("Interval", `${data.interval_s ?? "?"} s`),
    statRow("Engine", data.engine || "?"),
    statRow("Uptime", `${Math.round(data.uptime_s || 0)} s`));
  if (!capturing) {
    $("screen-text").textContent = "Capture is off - no screen text is served.";
    $("screen-frame").hidden = true;
    $("screen-note").textContent = "Turn capture on to read the screen.";
    return;
  }
  const latest = await api.get("/api/screen/latest");
  if (latest.ok && latest.data) {
    const win = (latest.data.window || {}).title || "(no active window)";
    $("screen-text").textContent = `${win}\n${"-".repeat(48)}\n${latest.data.text || ""}`;
    $("screen-note").textContent = `age ${latest.data.age_s}s Ãƒâ€šÃ‚Â· ${latest.data.line_count} lines Ãƒâ€šÃ‚Â· OCR ${latest.data.latency_ms} ms`;
  } else {
    $("screen-text").textContent = (latest.data && latest.data.error) || "No sample yet.";
  }
  showScreenFrame();
}

function showScreenFrame() {
  const img = $("screen-frame");
  img.hidden = false;
  img.onerror = () => {
    img.hidden = true;
    $("screen-note").textContent = "Frame unavailable (capture off or reader offline).";
  };
  img.src = `/api/screen/frame?ts=${Date.now()}`;
}

async function screenCapture(state) {
  const r = await api.post(`/api/screen/capture/${state}`, {});
  if (!r.ok) {
    toast(`Capture ${state} failed: ${(r.data && r.data.error) || r.error || r.status}`, "error");
  } else {
    toast(`Capture ${state}`, "info");
  }
  loadScreen();
}

async function screenDescribe() {
  $("screen-note").textContent = "Asking the local vision modelÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦";
  const r = await api.get("/api/screen/describe");
  if (!r.ok) {
    $("screen-note").textContent = `Describe failed: ${(r.data && r.data.error) || r.error || r.status}`;
    return;
  }
  const d = r.data || {};
  $("screen-note").textContent = `Vision guess (${d.model || "?"}) - verify against the text above: ${d.description || "(no answer)"}`;
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ evolve ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
async function loadEvolve() {
  const box = $("evolve-status");
  const r = await api.get("/api/evolve/overview");
  if (!r.ok) {
    box.replaceChildren(h("p", { class: "empty", text: `Could not load EVOLVE: ${r.error || r.status}` }));
    return;
  }
  const d = r.data || {};
  const ev = d.evolve || {};
  const arena = d.arena || {};
  const last = (d.last && d.last.last) || null;
  let lastText = "none recorded";
  if (last) {
    const res = last.result || {};
    const best = (res.best && res.best.fitness != null) ? res.best.fitness : null;
    lastText = [last.kind || "run",
      res.generations_run != null ? `${res.generations_run} generations` : null,
      best != null ? `best ${best}` : null].filter(Boolean).join(" Ãƒâ€šÃ‚Â· ");
  }
  box.replaceChildren(
    statRow("EVOLVE", ev.available ? `available${ev.version ? ` (${ev.version})` : ""}` : `unavailable: ${ev.reason || "?"}`, ev.available ? "se-none" : "se-destructive"),
    statRow("Arena", arena.ok ? `online (${arena.status})` : `offline${arena.error ? `: ${arena.error}` : ""}`, arena.ok ? "se-none" : "se-destructive"),
    statRow("Last run", lastText),
    statRow("Arena URL", arena.url || "http://127.0.0.1:8766/"));
  const frame = $("evolve-frame");
  if (arena.ok && !frame.src) frame.src = `${arena.url}?embed=jarvis`;
  $("evolve-open").href = arena.url || "http://127.0.0.1:8766/";
}

function selectTab(name) {
  currentTab = name;
  for (const btn of document.querySelectorAll(".tab")) {
    const on = btn.dataset.tab === name;
    btn.classList.toggle("is-active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  }
  for (const pane of document.querySelectorAll(".panel-pane")) {
    const on = pane.id === `panel-${name}`;
    pane.classList.toggle("is-active", on);
    pane.hidden = !on;
  }
  refreshPanel(name);
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ mute ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function toggleMute(force) {
  state.muted = typeof force === "boolean" ? force : !state.muted;
  const btn = $("mute-btn");
  btn.setAttribute("aria-pressed", state.muted ? "true" : "false");
  btn.textContent = state.muted ? "ÃƒÂ°Ã…Â¸Ã¢â‚¬ÂÃ¢â‚¬Â¡ Voice: muted" : "ÃƒÂ°Ã…Â¸Ã¢â‚¬ÂÃ‹â€  Voice: on";
  if (state.muted) voice?.stopSpeaking();
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ composer ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function autoGrow() {
  const t = $("chat-input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 96) + "px";
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ wiring ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
let _wired = false;
/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ UI scale ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
const UI_SCALE_KEY = "jarvis.ui_scale";

function applyUiScale(value) {
  const scale = String(value || "0.9");
  document.documentElement.style.setProperty("--ui-scale", scale);
  try { localStorage.setItem(UI_SCALE_KEY, scale); } catch { /* private mode */ }
  const sel = $("set-ui-scale");
  if (sel) sel.value = scale;
}

function initialUiScale() {
  try { return localStorage.getItem(UI_SCALE_KEY) || "0.9"; } catch { return "0.9"; }
}

function wire() {
  if (_wired) return;
  _wired = true;

  // topbar posture
  $("mode-select").addEventListener("change", (e) => onModeChange(e.target.value));
  $("defcon-select").addEventListener("change", (e) => onDefconChange(e.target.value));
  $("palette-open").addEventListener("click", openPalette);
  $("conn-retry").addEventListener("click", () => socket.connect());

  // tabs
  for (const btn of document.querySelectorAll(".tab")) btn.addEventListener("click", () => selectTab(btn.dataset.tab));
  $("screen-refresh").addEventListener("click", loadScreen);
  $("screen-on").addEventListener("click", () => screenCapture("on"));
  $("screen-off").addEventListener("click", () => screenCapture("off"));
  $("screen-describe").addEventListener("click", screenDescribe);
  $("evolve-refresh").addEventListener("click", loadEvolve);

  // panels
  $("tools-search").addEventListener("input", renderTools);
  $("tools-refresh").addEventListener("click", loadTools);
  $("memory-refresh").addEventListener("click", loadMemory);
  $("tasks-refresh").addEventListener("click", loadTasks);
  $("alerts-refresh").addEventListener("click", loadAlerts);
  $("nodes-refresh").addEventListener("click", loadNodes);
  $("diag-rerun").addEventListener("click", runDiagnostics);

  // operations panel
  $("runtime-refresh").addEventListener("click", loadRuntime);
  $("ops-check").addEventListener("click", async () => {
    const btn = $("ops-check");
    btn.disabled = true;
    try {
      const r = await api.post("/api/ops/check");
      if (!r.ok) { toast(`Self-check failed: ${r.error || r.status}`, "err"); return; }
      const failing = (r.data?.checks || []).filter((c) => !c.ok).length;
      toast(failing ? `Self-check: ${failing} failing` : "Self-check: all pass",
            failing ? "warn" : "ok");
      await loadRuntime();
      refreshSystems();
    } finally {
      btn.disabled = false;
    }
  });
  $("runtime-tick").addEventListener("click", async () => {
    const btn = $("runtime-tick");
    btn.disabled = true;
    try {
      const r = await api.post("/api/runtime/tick", { jobs: false });
      if (!r.ok) { toast(`Tick failed: ${r.error || r.status}`, "err"); return; }
      const res = r.data?.result || {};
      toast(`Tick complete ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${res.jobs ?? 0} job(s), ${res.events ?? 0} event(s)`, "ok");
      await loadRuntime();
      refreshSystems();
    } finally {
      btn.disabled = false;
    }
  });
  $("runtime-emit").addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = $("runtime-emit-status");
    const topic = ($("runtime-topic").value || "").trim();
    if (!topic) { status.textContent = "A topic is required."; return; }
    let data = {};
    const raw = ($("runtime-data").value || "").trim();
    if (raw) {
      try { data = JSON.parse(raw); } catch { status.textContent = "The data field is not valid JSON."; return; }
      if (!data || typeof data !== "object" || Array.isArray(data)) {
        status.textContent = "The data field must be a JSON object.";
        return;
      }
    }
    status.textContent = "EmittingÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦";
    const r = await api.post("/api/events", { topic, data });
    if (!r.ok) { status.textContent = `Could not emit: ${r.error || r.status}`; return; }
    status.textContent = `Event ${r.data.event_id} accepted ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â ${r.data.matched} rule(s) matched.`;
    $("runtime-topic").value = "";
    $("runtime-data").value = "";
    await loadRuntime();
  });
  // subsystem chips open the panel that explains them
  for (const chip of document.querySelectorAll(".sys-chip")) {
    chip.addEventListener("click", () => selectTab(chip.dataset.target));
  }

  // feed
  $("feed-filter").addEventListener("change", (e) => { state.feedFilter = e.target.value; renderFeed(); });
  $("feed-clear").addEventListener("click", () => { state.feed = []; renderFeed(); });

  // settings
  $("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    // Flat keys are what /api/settings accepts; anything it rejects is shown.
    const body = {
      "policy.mode": $("set-mode").value,
      "voice.enabled": $("set-voice-enabled").checked,
      "voice.tts_enabled": $("set-tts-enabled").checked,
    };
    const defcon = Number($("set-defcon").value);
    const r = await api.post("/api/settings", body);
    const d = await api.post("/api/defcon", { level: defcon });
    const rejected = Object.entries(r.data?.rejected || {}).map(([k, why]) => `${k}: ${why}`);
    if (r.unavailable) $("settings-status").textContent = "not available (404).";
    else if (!r.ok) $("settings-status").textContent = `error: ${r.error || r.status}`;
    else if (rejected.length) $("settings-status").textContent = `partly saved ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â rejected ${rejected.join("; ")}`;
    else { $("settings-status").textContent = "saved (persona name is read-only here)"; toast("Settings saved", "ok"); }
    if (d.ok) { state.defcon = d.data?.defcon ?? defcon; } else toast(`DEFCON not changed: ${d.error || d.status}`, "err");
    state.ttsEnabled = $("set-tts-enabled").checked;
    renderPosture(); updateReactor();
  });
  $("settings-reload").addEventListener("click", loadSettings);
  $("set-ui-scale").addEventListener("change", (e) => applyUiScale(e.target.value));
  $("unlock-btn").addEventListener("click", doUnlock);
  $("lock-btn").addEventListener("click", doLock);

  // chat
  $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendMessage($("chat-input").value); });
  $("send-btn").addEventListener("click", (e) => { e.preventDefault(); sendMessage($("chat-input").value); });
  $("chat-input").addEventListener("input", autoGrow);
  $("chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage($("chat-input").value); }
  });
  $("mute-btn").addEventListener("click", () => toggleMute());
  $("convo-btn").addEventListener("click", () => voice.toggleConversation());
  $("mic-stop").addEventListener("click", () => voice.stopAllMics());
  voice.wireSettings();

  // push to talk ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â button (pointer) and Space (keyboard)
  const ptt = $("ptt-btn");
  ptt.addEventListener("pointerdown", (e) => { e.preventDefault(); startPTT(); });
  ptt.addEventListener("pointerup", () => stopPTT());
  ptt.addEventListener("pointerleave", () => { if (voice.ptt) stopPTT(); });
  ptt.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); startPTT(); } });
  ptt.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") stopPTT(); });

  // approvals modal close
  $("approval-close").addEventListener("click", () => { $("approval-modal").hidden = true; });
  // palette
  $("palette-close").addEventListener("click", closePalette);
  $("palette-input").addEventListener("input", (e) => buildPalette(e.target.value));
  $("palette-input").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); paletteSel = Math.min(paletteSel + 1, paletteItems.length - 1); renderPalette(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); paletteSel = Math.max(paletteSel - 1, 0); renderPalette(); }
    else if (e.key === "Enter") { e.preventDefault(); runPalette(paletteSel); }
  });
  $("palette-modal").addEventListener("click", (e) => { if (e.target === $("palette-modal")) closePalette(); });
  $("approval-modal").addEventListener("click", (e) => { if (e.target === $("approval-modal")) $("approval-modal").hidden = true; });

  // Browsers block audio until a user gesture. Unlock the player on the first one.
  const unlockAudio = () => {
    voice?.queue?.unlock?.();
    window.removeEventListener("pointerdown", unlockAudio);
    window.removeEventListener("keydown", unlockAudio);
  };
  window.addEventListener("pointerdown", unlockAudio);
  window.addEventListener("keydown", unlockAudio);

  // global keyboard
  window.addEventListener("keydown", globalKeydown);
  window.addEventListener("keyup", globalKeyup);
}

function isEditable(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

function globalKeydown(e) {
  if (voice?.keydown(e)) return;              // configured push-to-talk hotkey (works in fields)
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
  if (e.key === "Escape") {
    if (!$("palette-modal").hidden) { closePalette(); return; }
    if (!$("approval-modal").hidden) { $("approval-modal").hidden = true; return; }
    if (voice?.convo) { voice.setConversation(false); toast("Conversation ended", "info"); return; }
    if (voice?.queue.busy) { voice.stopSpeaking(); return; }
    if (state.streaming && state.abort) { state.abort.abort(); toast("Stream cancelled", "warn"); return; }
    return;
  }
  // Space-hold PTT only when not typing in a field
  if (e.code === "Space" && !e.repeat && !isEditable(e.target) && !isModalOpen()) {
    e.preventDefault();
    startPTT();
  }
}
function globalKeyup(e) {
  if (voice?.keyup(e)) return;
  if (e.code === "Space" && !isEditable(e.target) && voice?.ptt) { e.preventDefault(); stopPTT(); }
}
function isModalOpen() { return !$("palette-modal").hidden || !$("approval-modal").hidden; }

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ websocket ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
function startSocket() {
  socket = new BusSocket(wsUrl());
  socket.addEventListener("state", (ev) => {
    const { status, attempt, delayMs } = ev.detail;
    setConnected(status === "open", { status, attempt, delayMs });
    if (status === "open") syncApprovals().catch(() => {});
  });
  socket.addEventListener("frame", (ev) => routeFrame(ev.detail));
  socket.connect();
}

/* ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ boot ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ÃƒÂ¢Ã¢â‚¬ÂÃ¢â€šÂ¬ */
let _inited = false;
function init() {
  if (_inited) return;
  _inited = true;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) document.documentElement.dataset.reducedMotion = "true";
  applyUiScale(initialUiScale());

  reactor = new ArcReactor($("reactor"));
  reactor.setMode("offline");
  reactor.start();

  sparks.cpu = new Sparkline($("spark-cpu"), { max: 100, color: "#3ce0ff" });
  sparks.mem = new Sparkline($("spark-mem"), { max: 100, color: "#57a9ff" });
  sparks.disk = new Sparkline($("spark-disk"), { max: 100, color: "#ffb02e" });

  voice = new VoiceController({
    api,
    send: (text, opts) => sendMessage(text, opts),
    canSpeak,
    toast,
    onState: (s) => { state.voiceState = s; updateReactor(); },
    fillInput: (text) => { $("chat-input").value = text; autoGrow(); $("chat-input").focus(); },
  });

  startClock();
  wire();

  // deep link: /#screen or /#evolve opens that panel directly
  const deepLink = (location.hash || "").replace("#", "");
  if (deepLink && document.querySelector(`.tab[data-tab="${deepLink}"]`)) selectTab(deepLink);
  toggleMute(false);
  renderFeed();

  // baseline honest states
  $("telemetry-stale").hidden = true;
  renderPosture();
  renderHealth();
  renderVoiceSummary();

  startSocket();

  // periodic refresh ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â HTTP is authoritative; the WebSocket is for live events
  setInterval(() => {
    if (document.visibilityState !== "hidden") { refreshHealth(); refreshState(); }
  }, 12000);
  setInterval(pollMetrics, 6000);
  setInterval(() => { if (document.visibilityState !== "hidden") refreshSystems(); }, 12000);
  setInterval(() => {
    // Node telemetry goes over SSH, so refresh it only while the tab is open.
    if (state.connected && currentTab === "nodes") refreshPanel("nodes");
  }, 30000);
  setInterval(() => {
    // decay fault window + refresh clock-adjacent labels
    if (state.errorUntil && Date.now() >= state.errorUntil) { state.errorUntil = 0; updateReactor(); }
    if (state.lastMetricTs) markTelemetryStale(Date.now() - state.lastMetricTs > 25000);
  }, 1000);

  // lazy capability probe for STT so diagnostics can be honest from the start
  api.probe("/api/voice/transcribe", "POST").then((p) => {
    if (p.unavailable) state.capabilities.stt = false;
    else if (p.status && p.status < 500) state.capabilities.stt = true;
    renderVoiceSummary();
  });

  // first load
  loadSettings().catch(() => {});
  loadTools().catch(() => {});
  refreshSystems().catch(() => {});
  syncApprovals().catch(() => {});
  refreshAll().catch(() => {});
}

document.addEventListener("DOMContentLoaded", init);
if (document.readyState !== "loading") init();