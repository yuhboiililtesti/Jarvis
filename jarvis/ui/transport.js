/* =========================================================================
   transport.js — REST, WebSocket and SSE clients.
   Every network helper degrades gracefully: a missing endpoint (404) or a
   transport failure is reported as an explicit result, never thrown as a
   crash and never replaced with fake data.
   ========================================================================= */

/** Result of an HTTP call. `unavailable` is true for 404/405 (endpoint absent). */
export class HttpError extends Error {
  constructor(status, path, body) {
    super(`${status} ${path}`);
    this.status = status;
    this.path = path;
    this.body = body;
  }
}

export class Api {
  constructor(base = "") {
    this.base = base;
  }

  async request(method, path, { body, headers, signal } = {}) {
    const opts = { method, headers: { ...(headers || {}) }, signal };
    if (body !== undefined) {
      if (body instanceof FormData) {
        opts.body = body;              // let the browser set the boundary
      } else {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
      }
    }
    let res;
    try {
      res = await fetch(this.base + path, opts);
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      return { status: 0, unavailable: true, network: true, ok: false, data: null, error: String(err) };
    }
    let data = null;
    const ct = res.headers.get("content-type") || "";
    try {
      if (ct.includes("application/json")) data = await res.json();
      else { const t = await res.text(); data = t ? safeJson(t) : null; }
    } catch { data = null; }
    const unavailable = res.status === 404 || res.status === 405 || res.status === 501;
    return {
      status: res.status,
      ok: res.ok,
      unavailable,
      data,
      error: res.ok ? null : (data && (data.detail || data.error)) || `${res.status} ${res.statusText}`,
    };
  }

  get(path, opts) { return this.request("GET", path, opts); }
  post(path, body, opts) { return this.request("POST", path, { body, ...(opts || {}) }); }

  /** Lightweight capability probe used by the Diagnostics panel. */
  async probe(path, method = "POST", body) {
    const r = await this.request(method, path, body === undefined ? {} : { body });
    return { path, method, status: r.status, available: r.status !== 0 && !r.unavailable && r.status < 500, unavailable: r.unavailable, network: !!r.network, error: r.error || null };
  }
}

/** Streaming SSE chat client for POST /api/chat. */
export async function streamChat(api, { message, sessionId = "default", source = "text", onFrame, signal }) {
  // source "voice" asks the persona for speech-friendly replies (no markdown, short).
  const res = await fetch(api.base + "/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify({ message, session_id: sessionId, source }),
    signal,
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    const unavailable = res.status === 404 || res.status === 405;
    return { ok: false, status: res.status, unavailable, error: safeJson(txt)?.detail || `${res.status} ${res.statusText}` };
  }
  if (!res.body) return { ok: false, status: res.status, error: "streaming unsupported by this browser" };

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawAny = false;

  const handleBlock = (block) => {
    const lines = block.split("\n");
    const dataLines = [];
    for (const line of lines) {
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      // ignore "event:"/"id:"/"retry:" — frame JSON carries `kind`
    }
    if (!dataLines.length) return;
    const payload = dataLines.join("\n").trim();
    if (payload === "" || payload === "[DONE]") return;
    let frame = safeJson(payload);
    if (frame === undefined) frame = { kind: "token", text: payload };
    sawAny = true;
    onFrame?.(frame);
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        handleBlock(block);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) handleBlock(buffer);
  } catch (err) {
    if (err && err.name === "AbortError") return { ok: true, aborted: true, sawAny };
    return { ok: false, status: 0, network: true, error: String(err), sawAny };
  }
  return { ok: true, sawAny };
}

/**
 * WebSocket client with exponential backoff + jitter.
 * Emits "frame" CustomEvents carrying parsed server frames
 * ({topic,data,ts,seq}) and "state" events carrying the connection status.
 */
export class BusSocket extends EventTarget {
  constructor(url) {
    super();
    this.url = url;
    this.ws = null;
    this.attempt = 0;
    this.closedByUser = false;
    this.timer = 0;
    this.status = "idle"; // idle | connecting | open | reconnecting | closed
  }

  _emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }

  _setStatus(status, extra = {}) {
    this.status = status;
    this._emit("state", { status, attempt: this.attempt, ...extra });
  }

  connect() {
    this.closedByUser = false;
    clearTimeout(this.timer);
    this._setStatus(this.attempt ? "reconnecting" : "connecting");
    let ws;
    try {
      ws = new WebSocket(this.url);
    } catch (err) {
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.addEventListener("open", () => {
      this.attempt = 0;
      this._setStatus("open");
    });
    ws.addEventListener("message", (ev) => {
      let frame;
      try { frame = JSON.parse(ev.data); } catch { return; }
      if (frame && typeof frame === "object" && frame.topic) this._emit("frame", frame);
    });
    ws.addEventListener("close", (ev) => {
      this.ws = null;
      if (this.closedByUser) { this._setStatus("closed", { code: ev.code }); return; }
      this._scheduleReconnect(ev.code, ev.reason);
    });
    ws.addEventListener("error", () => { /* close follows */ });
  }

  _scheduleReconnect(code, reason) {
    this.attempt += 1;
    const base = Math.min(30000, 500 * Math.pow(2, this.attempt - 1));
    const delay = Math.round(base * (0.7 + Math.random() * 0.6));
    this._setStatus("reconnecting", { code, reason, delayMs: delay, nextInMs: delay });
    this.timer = setTimeout(() => this.connect(), delay);
  }

  close() {
    this.closedByUser = true;
    clearTimeout(this.timer);
    try { this.ws?.close(1000, "client"); } catch { /* ignore */ }
    this.ws = null;
    this._setStatus("closed");
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(typeof obj === "string" ? obj : JSON.stringify(obj));
      return true;
    }
    return false;
  }
}

function safeJson(text) {
  try { return JSON.parse(text); } catch { return undefined; }
}

export function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}
