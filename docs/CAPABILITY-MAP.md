# JARVIS Capability Map

**Status:** Observed baseline (104 tools, 842 tests passing, 2026-10-19) | **Owner:** Topaz | **Last updated:** 2026-10-19

Maps the owner's "everything a computer can do" list (141 domains) onto what JARVIS can do now, what
is verified, and the free / open-source / local path for everything else. It is
an inventory, not a claim that every domain works.

## How to read this

JARVIS reaches a capability only through a **tool** that passes the policy gate, the approval gate
and the independent verifier ([ARCHITECTURE.md](../ARCHITECTURE.md) §2). The blueprint principle
applies: the model decides what should be done; trusted adapters decide what can be done.

| Label | Meaning | Evidence class |
|---|---|---|
| **NOW** | A dedicated tool or service exists in the repo and its tests pass | Observed (104 tools, 842 tests, 2026-10-18) |
| **PARTIAL** | The code exists and is tested, but a real-world path is unverified | Observed code / unverified runtime |
| **NEXT** | Free local path identified and feasible on this hardware; not started | Planned |
| **HARDWARE** | Needs a device or service the owner does not have yet | Planned / Unknown |
| **ADVISORY** | Regulated or safety-critical domain: JARVIS may explain, analyse and draft, never operate | Policy decision |

`shell.run` (approval-gated) technically reaches any command-line program on the PC. That is a
**generic escape hatch, not a capability claim**: it has no domain-specific verification, so each
domain below still counts as NOW only when a dedicated, verified tool exists.

The 2026-10-18 pass promoted the former BUILDING rows after the whole suite went green: the tool
exists, `tools --health` reports it available, and a test covers it. Where a capability is only
proven at the unit level (not exercised against the real world), it is marked PARTIAL and that gap
is named.

## Hardware and software baseline (Observed 2026-10-18, read-only probe on WIN11)

- **Present:** RTX 3080 10 GB (≈8 GB in use by other apps during the probe), 3 healthy SSDs
  (1 TB + 2 × 2 TB), USB audio, USB camera "WG01", Microsoft Print to PDF.
- **Toolchains on PATH:** ffmpeg/ffprobe, git, Ollama, Python 3.12, Node, Java 21, .NET, CMake,
  OpenSSH client, curl, openssl, certutil, robocopy, powercfg, schtasks, winget, isoburn, nvidia-smi.
- **Absent / disabled:** no Docker or Podman; Hyper-V, WSL, Virtual Machine Platform and Windows
  Sandbox are installed but **disabled**; no optical drive, no physical printer, no UPS/battery, no
  Pandoc/LibreOffice, no Tailscale/WireGuard, no 3D/CAD/creative suites on PATH. OCR is present via
  RapidOCR (installed 2026-10-20).
- **Laptop node:** ThinkPad SL510, Ubuntu 24.04, cannot host an LLM (Reported; see README).

## Map

### Core, files, documents (owner domains 1–3, 43, 65, 101–103, 106)

| Capability | Status | Path (free / local) | Gate |
|---|---|---|---|
| Arithmetic, logic, symbolic maths | NOW | `calc.eval`, `compute.solve` (sympy) | auto |
| Hashing, compression, encoding | NOW | `files.hash`, `files.zip/unzip` (stdlib, zip-slip safe) | write = approval |
| Encrypt / decrypt | NEXT · P3 | `age` or `openssl` via a dedicated tool; keys stay in secret store | approval |
| File read/list/search/write/delete | NOW | `files.*` inside allowed roots | write/delete = approval |
| Copy, move, mkdir, duplicates, disk usage | NOW | `files.copy/move/mkdir/duplicates/disk_usage`; atomic copy + SHA-256 read-back | approval |
| Indexed semantic search over documents | NOW | `knowledge.*` — SQLite FTS5 + local embeddings, path/heading/line provenance | auto (read) |
| Notes, reminders, scheduled jobs | NOW | `notes.*`, `scheduler.*` | write = approval |
| PDF / DOCX / XLSX read and create | NOW | `docs.read_pdf/read_docx/create_docx/read_xlsx/create_xlsx` | write = approval |
| OCR of scans and screenshots | NOW | `vision.ocr`, `vision.screen` — RapidOCR (ONNX, CPU, offline models); local, no upload | auto (read) |
| Format conversion (docs) | NEXT · P3 | Pandoc (winget) behind a `docs.convert` tool | write = approval |
| Calendar / contacts | NEXT · P2 | Radicale (self-hosted CalDAV/CardDAV, plain files) + `caldav` client | write = approval |
| Translation, summarisation, grammar | NOW (model) | local LLM; dedicated offline translation model NEXT · P3 | auto |

### Persistent runtime — operate, not answer (the v0.1 slice)

| Capability | Status | Path | Gate |
|---|---|---|---|
| Durable conversation / working memory | NOW | `runtime/sessions.py` + `sessions` table; survives restart | — |
| Scheduled jobs that actually fire | NOW | `scheduler.*` tools + `runtime/scheduler.py` (at-most-once per occurrence) | per action |
| Event log + declarative rules | NOW | `runtime/events.py`; `jarvis runtime events --emit`, `POST /api/events` | per action |
| Proactive reactions | NOW (policy-limited) | reactions call `TaskController.execute_tool`; mutating actions still need approval | approval |
| Operations console (HUD) | NOW | Operations tab: lease/ticks, schedules, runs, rules, events; live subsystem strip | — |
| Durable multi-step planner/executor | NOW | `goals.*` tools + one step per runtime tick; policy-gated | approval |
| Self-diagnostics + tunnel repair | NOW | `jarvis ops`, `system.diagnose`, `/api/ops`; one bounded repair | auto (read) |
| MCP server for other agents | NOW | `python -m jarvis mcp` (read-only tool surface) | per tool |
| Vision / screen understanding | NEXT · v0.3 | local VLM or OCR over `screen.capture` output | auto (read) |
| Cameras → local CV events | NEXT · v0.5 | detector emits events; only interesting ones reach reasoning | auto (read) |

### Desktop and OS control (domains 22, 60, 76–77, 127, 139–140)

| Capability | Status | Path | Gate |
|---|---|---|---|
| System info, metrics, processes | NOW | `system.*`, monitoring service | auto |
| Launch allowlisted apps | NOW | `apps.launch` (args opt-in per app) | approval |
| Screenshot | NOW | `screen.capture` | auto |
| Window list / focus / min / max / close | NOW | `window.*` (ctypes `EnumWindows`) | close = approval |
| Keyboard / mouse input | NOW | `input.*` (`SendInput` via ctypes) | approval, capped |
| Volume, mute, media keys | NOW | `media.volume_get/set`, `media.mute`, `media.play_pause/next/previous` | approval |
| Lock / sleep / shutdown / restart | NOW | `power.*` (cancellable delay, never auto-retried) | destructive |
| Kill process | NOW | `process.kill` (refuses system/JARVIS processes) | destructive |
| Desktop notifications | NOW | `notify.toast` (WinRT toast via PowerShell) | approval |
| Process containment / sandbox | NOW | `sandbox.status`; Windows Job Object tier + secret scrubbing ([SANDBOX.md](SANDBOX.md)) | always on |
| Wake-on-LAN for other machines | NEXT · P3 | magic packet (stdlib UDP) | approval |
| Event triggers (file appears, device connects) | NEXT · P2 | watchdog-style poller feeding the scheduler; same policy path | per action |

### Voice, speech, accessibility (domains 11, 64, 109)

| Capability | Status | Path | Gate |
|---|---|---|---|
| Speech-to-text | NOW (code) / PARTIAL (runtime) | faster-whisper; `/api/voice/transcribe` | owner-initiated |
| Text-to-speech, JARVIS voice | NOW (code) / PARTIAL (runtime) | Kokoro (British male) + JARVIS effect chain; Piper fallback | auto |
| Hands-free conversation, barge-in | PARTIAL | browser VAD; off by default, visible mic indicator; not yet exercised with a real microphone | explicit toggle |
| Wake word "Hey Jarvis" | PARTIAL (models installed, off) | openWakeWord `hey_jarvis`; models downloaded 2026-10-20, still opt-in | explicit toggle |
| Captions / live transcript | NOW (code) | HUD shows heard + spoken text | — |
| Speaker identification | NEXT · P4 | local speaker-embedding model | — |
| Voice cloning of a real person | **Not provided** | Declined: imitating a real person's voice without consent | — |

### Web, communications, networking (domains 4–6, 10, 105, 114–115)

| Capability | Status | Path | Gate |
|---|---|---|---|
| Web search / fetch | NOW | `web.search`, `web.fetch` (content treated as data; private/loopback targets refused) | auto |
| Open URL in browser | NOW | `web.open` (http/https only) | approval |
| Full browser automation | NEXT · P2 | Playwright for Python + Chromium (one-time install) | form submit = approval |
| Weather | NOW | `weather.current/forecast` (Open-Meteo, no key) | auto |
| Email draft / send | NOW | `email.draft` always; `email.send` needs config + secret | send = approval |
| Chat (Matrix / Discord webhooks) | NEXT · P3 | self-hosted Matrix (Synapse/Conduit) or webhook adapter | approval |
| Network info, ping, ports, LAN discovery | NOW | `network.*`, discovery service | auto |
| DNS / DHCP / firewall / VPN management | NEXT · P3 | read first; changes only with staged rollback (lock-out risk) | destructive |
| Packet capture | NEXT · P4 | Wireshark/tshark (not installed), authorised networks only | approval |
| Private remote access | NOW (LAN) / NEXT (off-LAN) | `serve --lan` + API token + pairing (loopback always trusted); WireGuard/Headscale for off-LAN | owner setup |

### Servers, homelab, private cloud (domains 7–8, 24–25, 118–123, 128–130)

| Capability | Status | Path | Gate |
|---|---|---|---|
| JARVIS as its own private cloud | NOW (LAN gate) | `serve --lan` + API token + pairing | owner setup |
| Remote node control | NOW | `nodes.*` (laptop over SSH tunnel) | approval |
| Containers | NEXT · P2 | Docker/Podman not installed; enable WSL2 + Podman, then `containers.*` tools | approval |
| Virtual machines | NEXT · P3 | Hyper-V installed but disabled; enable, then `vm.*` via PowerShell Hyper-V module | approval |
| Self-hosted services (Jellyfin, Immich, Nextcloud, Home Assistant, Pi-hole, Radicale) | NEXT · P3 | deploy via compose on a spare box; JARVIS health-checks and manages over their APIs | approval |
| Clusters, HA, render farms | HARDWARE | needs more machines; worker protocol already exists in `nodes` | — |

### Media, creative, AI generation (domains 9, 11–17, 26–27, 67–69, 99, 116)

| Capability | Status | Path | Gate |
|---|---|---|---|
| Probe / transcode audio and video | NOW | `media.probe`, `media.convert` (ffmpeg; output verified by re-probe) | write = approval |
| Image resize / convert / metadata | NEXT · P3 | Pillow + exiftool | write = approval |
| Local image generation | NEXT · P3 | ComfyUI server (`POST /prompt`, `/history`, websocket); GPU contention with games | approval |
| Media library (Jellyfin) management | NEXT · P3 | Jellyfin API on a spare box | approval |
| Screen / stream recording | NEXT · P3 | ffmpeg gdigrab or OBS websocket | approval |
| 3D, CAD, slicing | NEXT · P4 | Blender / FreeCAD / PrusaSlicer CLIs once installed | approval |
| Optical disc read / rip / burn | HARDWARE | no optical drive present; `isoburn` exists for ISO burning only | destructive for burns |

### Development (domains 19–21, 28, 30, 123–126)

| Capability | Status | Path | Gate |
|---|---|---|---|
| Run code / scripts | NOW | `shell.run` behind approval, inside the sandbox | approval |
| Git status / diff / commit | NEXT · P2 | dedicated `git.*` tools (read auto, write approval) | approval |
| Build / test a repo | NEXT · P2 | project-declared commands only, captured output as evidence | approval |
| ML training / fine-tune | NEXT · P4 | local PyTorch on the 3080 when not gaming | approval |
| Hardware diagnostics / benchmarks | NEXT · P3 | `smartctl` (not installed), `nvidia-smi`, `winsat`, Ollama tokens/s | auto (read) |
| Microcontrollers (Arduino, ESP32) | HARDWARE | `arduino-cli` / PlatformIO once boards exist | approval |

### Home, sensors, physical (domains 51–53, 63, 78–80, 110–111)

| Capability | Status | Path | Gate |
|---|---|---|---|
| Smart home | NOW (needs a Home Assistant instance) | `home.states`, `home.call_service`, `home.call_service_critical` | locks/alarms/garage/heat = destructive |
| Cameras / NVR | NEXT · P3 | Frigate (open source, local detection) API + MQTT events | auto (read) |
| USB camera snapshot | NEXT · P3 | ffmpeg dshow capture; disclosed, owner-initiated only | approval |
| Weather station, energy, UPS | HARDWARE | none present | — |
| Robots, motors, relays | HARDWARE | via a device gateway with safety envelope + watchdog (Blueprint §15) | destructive |

### Security, backup, monitoring (domains 54–59, 88, 117, 125, 136–138)

| Capability | Status | Path | Gate |
|---|---|---|---|
| Monitoring + alerts, integrity watcher, honeypot, DEFCON | NOW | monitoring / security services | auto |
| Audit log of every action | NOW | `audit` table, now a **SHA-256 hash chain** with an external anchor (`jarvis audit`, `/api/audit/verify`) | — |
| Sandboxed process execution | NOW | Windows Job Object / POSIX rlimits + secret-free child env (`jarvis sandbox`) | always on |
| Filesystem / network isolation for untrusted code | NEXT · P2 | Windows Sandbox VM (feature installed but disabled) or Podman; both need enabling | owner setup |
| Backups with restore proof | NEXT · P1 | restic (open source) to a second disk + restore test; EDITH owns recovery | approval |
| Log collection / analysis | NEXT · P3 | Windows Event Log read tool | auto (read) |
| Vulnerability scanning of own machines | NEXT · P4 | authorised assets only | approval |
| Disk imaging / forensics | NEXT · P4 | read-only imaging of owned media | approval |

### Advisory only (domains 41, 45–49, 72, 81–84, 89–92, 112–113)

Banking transactions and trading, vehicle/aviation/space control, emergency dispatch, medical
decisions, legal advice, government systems, industrial control and critical infrastructure.
JARVIS may research, explain, calculate and draft; it does not operate these systems. Personal
finance *tracking* from owner-supplied files is allowed (NEXT · P4). This follows Blueprint
§AM / §15 and the persona rule "confirms before anything that spends".

## Model upgrade path

The current model is `qwen2.5:7b-instruct` (Observed). Ollama's tool-capable catalogue (fetched
2026-10-18) lists 9B-class options that fit 10 GB at 4-bit quantisation (for example `nimble`,
`ornith:9b`, `bonsai-2:9b`). None is verified here. Adopt one only after it beats the current model
on `python -m jarvis eval core` with no safety or honesty regression. Do not pull models while the
GPU is busy with games.

## Priorities

P1 backups with restore proof · P2 OCR, browser automation, calendar, git/build tools, event
triggers, containers, filesystem/network isolation · P3 media/creative, homelab services, VMs,
network management · P4 everything else.

## What this pass changed (2026-10-18)

- Promoted 25 rows from BUILDING to NOW after the suite went green (85 tools, 842 tests).
- Added **process containment / sandbox** and **audit hash chain** as new NOW rows.
- Kept PARTIAL for voice features whose code and unit tests exist but whose real-microphone path
  has not been exercised, so the map does not round unit tests up to a working microphone.

## Sources

Ollama tool models: <https://ollama.com/search?c=tools> · OCR comparison:
<https://codesota.com/ocr/best-for-python>, <https://bestllmfor.com/guides/paddleocr-guide/>,
<https://www.linuxlinks.com/rapidocr-ocr-toolkit/> · Home Assistant REST:
<https://developers.home-assistant.io/docs/api/rest/>,
<https://developers.home-assistant.io/docs/auth_api.html> · ComfyUI API:
<https://docs.comfy.org/development/comfyui-server/api-examples> · Radicale:
<https://www.ssdnodes.com/learn/self-hosted-calendar-server-caldav> · Frigate API mirror:
<https://git.laurivan.com/Mirrors/blakeblackshear.frigate/src/commit/bd1d13d78c4ee7bcc5c45f431842a85548e47341/docs/docs/integrations/api.md>
· Playwright: <https://playwright.dev/python/docs/library> · Headscale:
<https://www.bitdoze.com/headscale-self-hosted-tailscale-setup/> · Job Objects:
<https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects>

## Related

[README](../README.md) · [ARCHITECTURE](../ARCHITECTURE.md) · [SANDBOX](SANDBOX.md) ·
[AI Blueprint capability catalog](../../../../Documentation/Blueprints/AI-Blueprint/02_CAPABILITY_CATALOG.md) ·
[Project record](../../../../Documentation/Projects/AI/Jarvis/Read-Me.md)
