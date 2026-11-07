# JARVIS HUD (`jarvis/ui/`)

A dark, cinematic JARVIS-style heads-up display. Plain HTML, CSS and vanilla
JavaScript — **no build step, no npm, no CDN, no external fonts**. It runs
entirely offline from `localhost`.

## Files

| File | Purpose |
| --- | --- |
| `index.html` | The HUD markup (telemetry, arc reactor, panels, conversation, modals). |
| `styles.css` | All styling, responsive layout, focus rings, `prefers-reduced-motion` path. |
| `app.js` | Controller: WebSocket/SSE clients, event routing, panels, chat, palette, keyboard. |
| `transport.js` | REST / WebSocket / SSE transport helpers with graceful degradation. |
| `reactor.js` | Canvas arc-reactor animation + telemetry sparklines. |
| `favicon.svg` | Arc-reactor mark. |

`app.js` is an ES module and imports the two helper modules:

```html
<script type="module" src="app.js"></script>
```

## How the static files are served

The FastAPI backend mounts this directory as static files, e.g.:

```python
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="jarvis/ui", html=True), name="ui")
```

Then open `http://127.0.0.1:8765/` (the configured server port). The HUD assumes
it is served from the **same origin** as the API — it uses relative URLs
(`/api/...`, `/ws`) so there is no CORS configuration required.

## Endpoint contract

Endpoints listed in the task brief (all relative to the same origin):

| Method | Path | Used for |
| --- | --- | --- |
| `GET` | `/api/health` | LLM reachability, mode, DEFCON, tool count, node role. |
| `GET` | `/api/state` | Persona (mood/busy), voice config, tools, mode, DEFCON. Polled every 12 s. |
| `WS` | `/ws` | Server-pushed event frames `{topic,data,ts,seq}`. |
| `POST` | `/api/chat` | SSE stream of `{kind,...}` frames. |
| `POST` | `/api/approvals/{id}` | `{decision:"allow"\|"deny", remember_s?}`. |
| `POST` | `/api/tts` | `{text}` — best effort, muted-aware. |
| `GET` | `/api/tools` | Tool registry (name, summary, category, side_effect, params). |
| `GET` | `/api/tasks` | Task list. |
| `GET` | `/api/memory` | Memory items. |
| `GET` | `/api/alerts` | Alerts. |
| `GET` | `/api/nodes` | Node registry. |
| `GET` | `/api/metrics?name=` | One metric series (cpu/mem/disk/temp). |
| `GET` | `/api/settings` | Read current settings. |
| `POST` | `/api/settings` | Write settings: `{mode}`, `{defcon}`, `{voice}`, `{persona}`. |

### Assumed endpoints (not in the brief — the UI degrades honestly if absent)

- `POST /api/voice/transcribe` — multipart `file` upload returning `{text}`.
  Adds push-to-talk speech. **Not listed in the brief.** If it 404s the UI says
  so and focuses the text input instead. Detected with a startup probe (an empty
  `POST` returning anything other than 404/405 means the route exists).
- `POST /api/defcon` — `{level}`. **Not listed in the brief.** Used only as a
  fallback when `POST /api/settings {defcon}` does not succeed.
- `GET /api/approvals` — **not used**; pending approvals are driven purely by the
  `approval.requested` / `approval.resolved` WebSocket events.

Anything that returns `404` / `405` is rendered as an explicit
`"… not available (404)"` state. No mock or placeholder data is ever shown as if
real; missing values read `unavailable` / `no data`.

## Event topics consumed (`/ws`)

`status`, `degraded`, `error`, `task.created`, `task.status`, `task.blocked`,
`task.reconcile`, `agent.step`, `agent.tool_call`, `tool.result`,
`approval.requested`, `approval.resolved`, `verify`, `memory.recall`, `alert`,
`defcon`, `voice.tts.start`, `voice.tts.done`, `node.status`, `metric`.

## The arc reactor reflects real state only

| Reactor | Condition |
| --- | --- |
| grey / OFFLINE | WebSocket not connected |
| slow cyan pulse, IDLE | connected, idle |
| fast spin + bright glow, THINKING | `busy` — a chat stream is open or a task is non-terminal |
| amber / ALERT | `defcon <= 3` |
| red / FAULT | an `error`/`degraded` frame arrived in the last 25 s |

There is no idle "fake activity": the animation is derived entirely from
connection state, active tasks, streaming state and DEFCON.

## Interaction

- **Enter** sends; **Shift+Enter** newline.
- **Esc** closes the palette/approvals modal or cancels the in-flight chat stream.
- **Space (hold)** = push-to-talk when focus is not in a text field.
- **Ctrl/⌘+K** = command palette (fuzzy filter over actions and registered tools).
- Mode and DEFCON selectors in the top bar, plus a full Settings panel.
- Approvals modal pops on `approval.requested` and **never auto-approves**.

## Features that need the voice stack

The HUD itself is always functional. Real speech needs the backend's optional
`voice` extra (`requirements-voice.txt`: faster-whisper, piper, sounddevice…):

| Feature | Needs |
| --- | --- |
| Push-to-talk → transcript | `POST /api/voice/transcribe` (STT). Browser needs microphone permission. |
| Spoken replies | `POST /api/tts` (TTS). |
| Voice config summary / wake word | `/api/state` → `voice`. |

When the voice stack is absent, Diagnostics shows the exact reason
(`404 — optional voice stack not installed`, `microphone permission denied`,
`browser has no microphone API`) and push-to-talk falls back to focusing the
text input — stated honestly on screen.
