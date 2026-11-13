# JARVIS — Live screen reader

**Status:** Active (verified on PC-001, 2026-11-12) | **Owner:** Topaz

A live, **in-memory** view of the screen: local OCR text plus an optional
vision-model caption. Nothing is recorded: no frames are written, no history
is kept, and the service only holds the **latest** sample in RAM.

It exists so JARVIS — and the coding assistant — can see what the owner is
doing (text, applications, gameplay HUD/objectives) without a screen recording
and without uploading anything.

## Components

| Piece | Purpose |
|---|---|
| `jarvis/screen_live.py` | sampler + loopback HTTP service (`127.0.0.1:8799`) |
| `vision.live` tool (in `jarvis/tools/builtin/vision_tool.py`) | reads `/latest` for the model |
| `llava:7b` on APOS (Ollama) | scene captioning for `/describe` |
| APOS tunnel legs | `8799` forward (APOS→PC reader), `11435` reverse (PC→APOS Ollama) |

Existing tools that remain useful: `vision.screen` (one-off OCR),
`screen.capture` (PNG artefact, writes a file), `host.screen.capture`
(remote host). The live reader is the continuous, no-write variant.

## Start / stop

```powershell
cd C:\Users\Topaz\Projects\AI\Jarvis
.\.venv\Scripts\python.exe -m jarvis.screen_live --interval 3.0
# Ctrl+C to stop; nothing persists
```

One-shot check (prints JSON and exits):

```powershell
.\.venv\Scripts\python.exe -m jarvis.screen_live --once
```

## Endpoints (loopback only)

| Endpoint | Returns |
|---|---|
| `GET /health` | sampler state, `capturing`, interval, samples, errors, engine |
| `GET /latest` | latest OCR text, lines, active window, age, OCR latency |
| `GET /frame` | the **current** JPEG, captured on request (never stored) |
| `GET /describe[?q=...]` | `{description, kind:"vision-guess", model, window, text}` |
| `POST /capture/off` | stop sampling, forget the last sample, refuse reads (409) |
| `POST /capture/on` | resume sampling |

## Capture on / off

The installed command (also `scripts\capture.cmd` in the repository):

```powershell
capture off       # stop sampling; /latest, /frame and /describe return 409
capture on        # resume (starts the reader hidden if it is not running)
capture status    # on / off / not running
capture off -Stop # pause and kill the reader process
```

While capture is off the service is idle (no OCR, no screen access) and it
forgets the last sample, so nothing from the screen can be read until
`capture on`. The switch is also exposed as `POST /capture/off` and
`POST /capture/on` for JARVIS.

The installed `capture.cmd` lives in `%USERPROFILE%\bin`, which is added to the
user `PATH` by `scripts/install-screen-capture.ps1`.

`/describe` always returns the grounded window title and OCR text alongside the
caption, because small vision models invent details.

Examples:

```powershell
curl.exe -s http://127.0.0.1:8799/latest
curl.exe -s -o "$env:TEMP\frame.jpg" http://127.0.0.1:8799/frame
curl.exe -s http://127.0.0.1:8799/describe
```

For a visual check, fetch `/frame` to a temporary file, look at it, and delete
it. The service itself never writes a frame or keeps a history.

From APOS (JARVIS Core host) the same URLs work through the tunnel:
`http://127.0.0.1:8799/...`.

## Settings (environment, no secrets)

| Variable | Default | Meaning |
|---|---|---|
| `JARVIS_SCREEN_LIVE_URL` | `http://127.0.0.1:8799` | where `vision.live` looks |
| `JARVIS_SCREEN_TOKEN` | unset | if set, required as `X-Jarvis-Screen-Token` |
| `JARVIS_SCREEN_VLM_URL` | `http://127.0.0.1:11435` | Ollama reachable from the PC |
| `JARVIS_SCREEN_VLM_MODEL` | `llava:7b` | captioner |
| `JARVIS_SCREEN_CAPTURE` | unset | `pil` forces the GDI fallback |

## Capture path

1. **DXGI Desktop Duplication** (`dxcam`) for the primary monitor — also works
   for borderless games where GDI `BitBlt` can return black.
2. **Pillow `ImageGrab`** as fallback, and for `--all-screens`.

## Measured (2026-11-12, PC-001, 1920×1080)

- Cold `--once`: **73 lines**, 6.3 s (includes RapidOCR model load); warm
  samples ~**2.1 s**.
- `vision.live` against the running service: **43 lines**, age 4.9 s.
- APOS health through the tunnel: `ok: true`, **59 samples, 0 errors**.
- Captioner comparison: `llava:7b` produced a grounded caption
  ("...a social media post with a photo of a person and a caption");
  `moondream` returned fragments (`urn`) and an invented game scene;
  `qwen2.5vl:3b` aborted with `token repeat limit reached` even on text-only
  prompts, so it is not used.
- Tests: 8 in `tests/test_screen_live.py` (sampler, HTTP contract, token,
  frame, tool, unreachable reader, missing VLM) — no real capture in tests.

## Honest limits

- **OCR reads visible text only**; it cannot describe a scene.
- **Captions are guesses.** The VLM may be wrong; `kind: "vision-guess"`
  and the returned window/OCR text are there to check it.
- Exclusive-fullscreen games may still defeat capture; DXGI is the better
  path but it was verified on the desktop, not inside a game.
- Multi-monitor `--all-screens` uses Pillow (DXGI is primary-monitor only).
- The service binds loopback; exposing it requires an explicit tunnel and,
  ideally, `JARVIS_SCREEN_TOKEN`.
- OCR cost grows with resolution: a 1080p frame costs ~2 s on this CPU, so the
  sampling interval should stay ≥3 s while other work runs.
- No Wayland backend.
