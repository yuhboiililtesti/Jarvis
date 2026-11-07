# JARVIS

A real, **local-first personal assistant** — a working system, not a mock-up. It runs
entirely on your own hardware, with **no paid API and no token cap** by default.

> Inspired by Tony Stark's AI. The inspiration defines the *experience*; this
> repository is the actual implementation.

**Author:** Topaz · **License:** [MIT](LICENSE) · **Status:** implemented and tested
(1,021 tests passing, 123 tools).

---

## What is it

JARVIS is a self-hosted assistant that can *do things*, not just answer questions:

- **Talks and listens** — wake word, local speech-to-text and a British butler voice.
- **Controls your machine** — files, apps, windows, screen, power, processes, media,
  clipboard, input, browser, shell (all gated by policy and approval).
- **Runs continuously** — a persistent runtime with a durable scheduler, event rules and
  resumable multi-step goals, so it works while you're away.
- **Learns and adapts** — records outcomes, reinforces lessons, and recalls them on later
  turns; durable memory with hybrid keyword + vector recall.
- **Uses multiple engines** — local models first (Ollama / any OpenAI-compatible server),
  with an optional *frontier* tier (Anthropic / Gemini / Groq) for hard requests.
- **Reaches other machines** — enrolled nodes over SSH, with a shared-token protocol.
- **Is verifiable** — tamper-evident audit chain, evidence-based tool results, and a
  held-out evaluation suite with deterministic oracles.

Everything runs locally unless you explicitly opt into a remote model or node.

---

## Quick start

### Prerequisites

- Python 3.11+
- [Ollama](https://ollama.com) with a model pulled (default `qwen2.5:3b-instruct`), or any
  OpenAI-compatible server.

### Install (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Voice -Model
```

This creates `.venv`, installs dependencies, pulls the voice stack and local model, writes
`config/jarvis.toml`, and runs `jarvis doctor`.

### Run it

```powershell
scripts\run-and-open.cmd        # starts hidden, waits for the port, opens the HUD
scripts\stop-jarvis.cmd         # stops it
```

Or from any platform:

```bash
python -m jarvis serve          # Core API + HUD
python -m jarvis repl           # interactive text loop (shows tool calls live)
python -m jarvis chat "status"  # one-shot turn
```

Open `http://127.0.0.1:8765` for the HUD, which includes an **Operations** console for the
persistent runtime.

---

## Architecture

Three roles, one brain:

```
Core        =  JARVIS Core  (jarvis-core :8765)
               brain · tools · memory · policy · orchestration
               local LLM (Ollama / OpenAI-compatible), optional frontier tier
Client/Agent=  HUD · voice · screen/vision · local tools
               connects to the Core over SSH tunnels
Node        =  enrolled satellite · relay · thin agent (no LLM hosting)
```

The Core holds the model and all state. Clients and nodes connect to it; nodes run a
lightweight agent (`python -m jarvis nodes`) that exposes `/node/health` and `/node/task`
behind a shared token. SSH tunnels are the default transport so nothing but SSH is exposed
on the LAN. See [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) and [`docs/CAPABILITY-MAP.md`](docs/CAPABILITY-MAP.md).

---

## What is implemented

| Area | Status |
|---|---|
| Durable task/attempt journal with restart reconciliation | ✅ |
| **Persistent runtime**: tick loop, single-owner lease, durable scheduler, event engine, durable working memory | ✅ |
| **Durable goals**: planner → stored steps → resumable execution through the controller | ✅ |
| **Learning**: outcome logging, lesson reinforcement, recall-augmented turns | ✅ |
| **Self-diagnostics**: subsystem checks and one bounded, audited repair | ✅ |
| **MCP server**: other agents can call the read-only tool surface | ✅ |
| **Vision**: local OCR over an image or the live screen (`vision.ocr`, `vision.screen`) | ✅ |
| **Tool choice at 120+ tools**: relevance shortlist + exact-repeat guard | ✅ |
| Policy broker + approval gate + standing grants + DEFCON posture | ✅ |
| Independent verifier (a tool's own "success" never settles a task) | ✅ |
| **LLM router**: Ollama local-first, OpenAI-compatible, frontier tier (Anthropic/Gemini/Groq), typed stream errors, eligible-only fallback | ✅ |
| Persona engine (JARVIS tone: measured, dry, addresses you as "sir") | ✅ |
| Tool registry + tool contract + **123 built-in tools** across 20+ categories | ✅ |
| Plugin system: discover → inspect → pin hash → load, with per-plugin side-effect ceiling | ✅ |
| Sandbox: process containment (Windows Job Object / POSIX rlimits), kill-tree, resource caps, secret-free child env | ✅ |
| Memory: SQLite + FTS5 keyword + vector recall, provenance, per-user isolation | ✅ |
| Analytics: statistics, forecasting, Monte Carlo simulation, regression | ✅ |
| FastAPI + WebSocket API and a JARVIS-style HUD with an Operations console | ✅ |
| Knowledge index over your own documents (FTS5 + local embeddings, cited) | ✅ |
| Voice: STT (faster-whisper), TTS (Kokoro "JARVIS" voice / Piper), VAD, optional wake word | ✅ |
| Monitoring, alerts, discovery, security watcher | ✅ |
| Night Protocol runner + morning report | ✅ |
| Core↔Node protocol and node agent | ✅ |

---

## Safety model (read this)

| Mode | Behaviour |
|---|---|
| `readonly` | everything mutating is refused |
| `ask` *(default)* | read-only tools run freely; anything that writes, executes, or touches another machine needs **your approval in the UI** |
| `autonomous` | non-destructive actions run without prompting |

- **Side-effect classes.** Every tool declares `none / network / local_write /
  local_exec / remote / destructive`. Policy gates on that class.
- **DEFCON 1–5** narrows autonomy and can suspend outbound effects entirely. It never
  loosens the configured mode.
- **Evidence, not claims.** Side-effecting tools must declare a read-back. A result that
  cannot be verified is recorded as *unverified*, never as success.
- **Approvals are real.** Shows the tool, its arguments, side-effect class and reason.
  Nothing auto-approves.
- **Secrets never enter model context.** They live in `data/secrets.json` or
  `JARVIS_SECRET_<NAME>` and are handed to a tool only at execution time.
- **The file boundary is not a model argument.** `files.*` accepts no `allowed_roots`
  parameter; widening is an owner/config decision. Credential stores (`~/.ssh`, browser
  profiles, OS credential vaults) and JARVIS runtime secrets are refused even inside an
  allowed root.
- **`web.fetch` is not an SSRF proxy.** It refuses loopback, private, link-local and
  metadata targets, and names that resolve to them. Deliberate LAN fetches need
  `[security] allow_private_fetch = true`.
- **App arguments are opt-in.** `apps.launch` launches only allowlisted apps and refuses
  extra arguments unless that app's allowlist entry says `allow_args = true`.
- **No process inherits a secret.** Every child process gets a scrubbed environment:
  `JARVIS_SECRET_*` and any value matching a stored secret are removed.
- **Processes are contained.** Commands run inside a Windows Job Object (or POSIX rlimits):
  the whole process tree dies together, memory/process/CPU ceilings apply, and a timeout
  kills the tree. Filesystem and network isolation need a VM/container and are **not**
  claimed — `python -m jarvis sandbox` states exactly what is and is not enforced.
- **The audit log is tamper-evident.** Every row is SHA-256 chained to the previous one and
  the head is anchored outside the database (`python -m jarvis audit`).
- **Audit writes are redacted.** Secret-key fields and known `SecretStore` values are
  replaced before the row is chained.
- **Persistent, but not unsupervised.** The runtime fires scheduled jobs and event
  reactions; everything goes through the same policy gate as a spoken request. Rules are
  created at the console, never over HTTP.
- **Goals are plans, not bypasses.** Each step of a multi-step goal is an ordinary
  controller call, so approval and verification still apply.
- **MCP is read-only.** A mutating call is refused rather than bypassing the gate.
- **Local-only.** Binds `127.0.0.1` by default; no cloud fallback unless you add one.

---

## Multi-engine (frontier tier)

By default JARVIS uses a local model. You can add a remote, high-capability tier for hard
requests while normal chat stays local and private.

```toml
# config/jarvis.toml  [llm]
frontier_provider   = "anthropic"                 # anthropic | gemini | groq
frontier_model      = "claude-3-5-sonnet-20241022"
frontier_key_secret = "anthropic_key"             # key name in data/secrets.json
frontier_trigger    = ["analyze", "research"]     # requests matching these escalate
```

Store the key in `data/secrets.json` (or `JARVIS_SECRET_ANTHROPIC_KEY`). With no trigger
configured, nothing ever escalates. A local failure never silently falls back to a remote
endpoint.

---

## Command line

```
python -m jarvis serve            # Core API + HUD
python -m jarvis repl             # interactive text loop (shows tool calls live)
python -m jarvis chat "status"    # one-shot turn
python -m jarvis tools            # the whole capability surface
python -m jarvis health           # subsystem health
python -m jarvis sandbox          # process-containment posture (--check to probe)
python -m jarvis audit            # verify the tamper-evident audit chain
python -m jarvis runtime          # persistent runtime: status | tick | rules | events
python -m jarvis goals            # durable multi-step goals
python -m jarvis ops              # self-diagnostics: status | check
python -m jarvis mcp              # stdio MCP server for other agents (read-only)
python -m jarvis plugins list     # owner-approved plugins
python -m jarvis doctor           # dependency + model diagnosis
python -m jarvis init             # write config and data dirs
python -m jarvis night "…"        # run the Night Protocol now
python -m jarvis nodes            # run the node agent (satellite side)
python -m jarvis eval core        # run the held-out evaluation suite
```

---

## Evaluation

The system is scored by a held-out suite with **deterministic oracles** — no model judges
another model, so a regression cannot hide behind a second opinion. Suites live in
`jarvis/eval/suites/*.json`; reports are written to `data/eval-reports/` as JSON and
Markdown.

```powershell
python -m jarvis eval --list          # available suites
python -m jarvis eval core            # run the held-out suite
```

---

## Testing

```bash
python -m pytest -q
```

**1,021 tests pass, 2 skipped** (no local model, voice stack or network required for the
test suite).

---

## Layout

```
jarvis/
  core/       kernel, agent turn loop
  llm/        providers (Ollama, OpenAI-compatible, Anthropic, Gemini, Groq), router
  persona/    JARVIS tone and system prompt
  tools/      base contract, registry, builtin/* tools (123)
  plugins/    verified plugin loader (discover → pin hash → load)
  runtime/    persistent loop: scheduler, event engine, durable sessions
  goals/      durable planner/executor
  learning/   outcome-based self-adaptation
  planner/    task planning engine
  tasks/      durable controller, verifier
  policy/     permission broker, secrets
  memory/     store, hybrid retrieval
  knowledge/  document index over your own files
  voice/      STT, TTS, VAD, wake word, pipeline
  monitoring/ metrics, alerts, night runner
  discovery/  LAN + hardware discovery
  security/   DEFCON, integrity watcher, honeypot, audit-chain anchor
  nodes/      core-side service + node agent
  compute/    algorithm-toolkit bridge
  sandbox/    Job Object / rlimit containment, env scrubbing, tier reporting
  api/        FastAPI app, WebSocket, auth gate, OpenAI-compatible /v1
  mcp/        stdio MCP server for other agents
  eval/       held-out evaluation suites and oracles
  ui/         the HUD (plain HTML/CSS/JS, offline)
config/       jarvis.example.toml
docs/         capability map, extending guide, sandbox, runtime, goals, ops, MCP
scripts/      install, run, night, node deploy/tunnel/verify, voice checks
tests/        pytest suite (1,021 tests)
plugins/      plugin template
```

---

## Configuration

Copy `config/jarvis.example.toml` to `config/jarvis.toml` and edit. Secrets and runtime
state live in `data/` (gitignored). See [`docs/EXTENDING.md`](docs/EXTENDING.md) to add
your own plugins.

---

## License

[MIT](LICENSE) © 2026 Topaz
