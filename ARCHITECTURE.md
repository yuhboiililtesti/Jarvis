# JARVIS — Architecture

This document describes the system as built. The specification pack in the
documentation tree remains authoritative for *requirements*; this file describes
*implementation*.

## 1. Process model

One Python process (the Core) hosts everything: HTTP API, WebSocket, agent loop,
tools, memory, monitoring. Optional satellites are separate processes on other
machines. There is no message broker and no database server — state is one SQLite
file.

```
                    ┌──────────────────────── JARVIS Core (python -m jarvis serve) ──────────┐
                    │                                                                        │
   HUD (browser) ──HTTP/WS──► api/app.py ──► kernel.Jarvis ──► agent.Agent ──► llm.router ──► Ollama
                    │                              │                              │            │
                    │                              ├── registry (tools) ◄─────────┘ tool calls │
                    │                              ├── controller (tasks/attempts/approvals)   │
                    │                              ├── policy (broker, grants, DEFCON)         │
                    │                              ├── verifier (independent read-back)        │
                    │                              ├── memory (SQLite + FTS5 + vectors)        │
                    │                              ├── monitoring / discovery / security       │
                    │                              └── nodes (satellite service) ──LAN──► Node  │
                    └────────────────────────────────────────────────────────────────────────┘
```

## 2. The single effect path

Every action that touches the world goes through **one** function:
`TaskController.execute_tool`. That is what makes the safety properties provable
rather than aspirational.

```
step(request)
  1. tool = registry.get(name)                 -> unknown tool fails loudly
  2. decision = policy.decide(tool, side_effect, args)
       deny-list        -> refuse
       DEFCON clamp     -> refuse outbound
       readonly + mutate-> refuse
       standing grant   -> allow
       else             -> needs_approval
  3. if needs_approval:
       write approvals row, publish approval.requested, AWAIT the owner
       timeout -> treated as deny (never as allow)
  4. OPEN ATTEMPT (committed to SQLite BEFORE the effect)
  5. result = tool.run(ctx)                    -> exceptions captured, never crash
  6. evidence = verifier.verify(tool, ctx, result)   -> independent read-back
  7. settle attempt: succeeded (verified) | completed-unverified | failed
  8. audit row + bus events
```

Consequences:

- A crash between step 4 and 7 leaves a durable `running` attempt. On next start
  `reconcile()` marks it `outcome-unknown` — it is never silently replayed.
- A tool bug cannot crash the Core.
- "Succeeded" means *verified*. Anything else is labelled honestly.

## 3. Task lifecycle

```
received -> validated -> planning -> (awaiting-authorization) -> running
         -> verifying -> succeeded | failed | blocked | cancelled | outcome-unknown
```

Persisted in `tasks`; each effect in `attempts`; each gate in `approvals`; every
event in `audit`.

## 4. Tool contract

A tool is a class with a `ToolSpec` and an async `run(ctx, **kwargs)`.

```python
ToolSpec(
    name="files.write",
    summary="Write text to a file inside an allowed root",
    version="1.0",
    category="files",
    side_effect=SideEffect.LOCAL_WRITE,   # what policy gates on
    params=(ToolParam("path", required=True), ToolParam("content", required=True)),
)
```

Side-effecting tools implement `verify(ctx, result)` returning a read-back
dictionary (e.g. re-read the file, compare a hash). The registry also filters the
tool list exposed to the model by side-effect ceiling, so a readonly posture does
not even *offer* mutating tools to the model.

## 5. Memory

`memory` table + `memory_fts` (FTS5). Recall is hybrid: BM25 keyword ranking
fused 50/50 with cosine over embeddings when an embedding model is available.
When it is not, retrieval silently degrades to keyword-only and reports
`method: "keyword"` — the caller always knows which it got. Every item carries
`source`, `confidence`, `private` and `tags`; deletion is a tombstone by default.

## 6. LLM layer

`OllamaProvider` streams typed events: `token`, `tool_calls`, `meta`, `done`,
`error`. It is impossible for a provider failure to look like an empty answer.
`ModelRouter` tries exactly one eligible fallback, only on connection failure,
and only providers you configured.

## 7. Voice

`VoiceService` probes each capability and reports `{available, reason}`. With no
voice packages installed the Core runs perfectly and the HUD shows "unavailable:
<reason>". STT runs in a thread executor. TTS defaults to Piper with a British
male voice (`en_GB-alan-medium`); a `null` engine exists so a headless node can
still receive audio files. Wake word is opt-in.

## 8. Nodes

Core side: `NodeService` enrols nodes, heartbeats them, and sends tasks over
HTTP with a shared token (`X-Jarvis-Node-Token`). Node side: `python -m jarvis.nodes`
runs a small agent on the SL510. A discovered host is **not** a trusted node —
discovery writes `status='discovered'`; only enrolment writes `status='enrolled'`.

## 9. Degradation

Every optional subsystem is imported in `try/except` in the kernel. A missing
module logs a warning; the Core starts anyway; the affected endpoint returns
`{available: false, reason: …}`. Nothing claims to work when it does not.

## 10. Night Protocol

`NightRunner` implements `Documentation/Agents/NIGHT-PROTOCOL.md`: Stage 0
preflight → recon → audit → bounded work loop → 3→2→1 failure ladder → health
gates → emergency stop → reconciliation → morning report. Bounded by
`night.max_minutes` and by policy: it cannot do anything an interactive turn
could not.

## 11. Sandbox

Every process the Core starts goes through `jarvis.sandbox.SandboxService`; there
is no other path. On Windows it wraps the child in a Job Object
(`KILL_ON_JOB_CLOSE`, memory/process/CPU limits); on POSIX it lowers rlimits and
runs in a new session so the process group can be killed. In both cases the child
gets a scrubbed environment: `JARVIS_SECRET_*` and any value matching a stored
secret are removed before the process starts. Timeouts kill the whole tree.

What it does not do is isolate the filesystem or the network — that needs a VM or
container, and `sandbox.status()` / `jarvis sandbox` say so rather than implying
otherwise. A run reports what it actually got: `contained` is true only if the OS
accepted the limits and the process was assigned; otherwise the result says so in
`notes`. See [docs/SANDBOX.md](docs/SANDBOX.md).

## 12. Audit integrity

`Database.audit` is the single choke point for the audit log, and it chains each
row to its predecessor with SHA-256 (length-prefixed fields, so field-boundary
shifts cannot collide). `verify_audit` walks the chain and reports the first
broken link; because trimming rows from the *end* leaves a valid chain, the head
is also written to `data/audit-anchor.json` on start and re-checked next time.
Once an anchor has been recorded (a marker is kept in the database), a **missing
or unreadable** anchor file is a failure, not a first run; the anchor is only
advanced when the chain verifies, so a detected break is not re-anchored away.
`jarvis audit` and `GET /api/audit/verify` expose the check. The chain is
self-consistent, not signed: an actor who can write the database and recompute
every hash is not detected — that would need a key held elsewhere or an external
witness, and is recorded as future work rather than faked.

## 13. Persistent runtime

`RuntimeService` owns a durable tick loop (`jarvis/runtime/`). While the Core is up it claims the
single-owner lease, fires due scheduled jobs once per occurrence, drains event reactions, and writes
a heartbeat. A failing tick is audited and backed off; it never stops the loop.

The scheduler claims an occurrence with `INSERT OR IGNORE` against `UNIQUE(job_id, due_key)`
**before** it runs, so no crash, restart or second Core can fire it twice; interrupted claims are
reconciled to `outcome-unknown` and never replayed. The event engine persists events and matches
declarative rules (data, not code) with one fire per `(rule, event)`; its reactions call
`TaskController.execute_tool`, so policy, approval, DEFCON and the verifier apply exactly as for a
spoken request. Sessions are stored in SQLite, so working memory survives a restart.

Not in v0.1: a planner (durable multi-step goals), vision, and any bypass of policy. In the default
`ask` mode a mutating reaction needs approval; in `autonomous` mode non-destructive tools run without
asking by design, and a reaction inherits that. See [docs/RUNTIME.md](docs/RUNTIME.md).
