# JARVIS Build Note

**Status:** Active build note (Observed: 864 tests pass, 2026-10-20 night audit) | **Owner:** Topaz | **Last updated:** 2026-10-20

The build note for the sandbox + audit-chain work, kept per Tony-Stark rule 7 (what it is, how
to run it, how to test it, what was measured, known limits, dependencies, versions) and
Protocol-0.0.11 (completion matrix, failure journal, assumptions, rollback).

## Objective

"Fully improve JARVIS and make sure everything is there", including the process sandbox a prior
session had promised but not built. No claim without a run.

## Definition of done

A requirement is complete only when **implemented + integrated + tested + verified** with captured
evidence. Existence of code is not completion.

## Completion matrix

Status: NOT STARTED · IN PROGRESS · IMPLEMENTED · INTEGRATED · TESTED · VERIFIED · BLOCKED · DEFERRED

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Sandbox service: tier detection, limits, scrubbed child env | VERIFIED | `tests/test_sandbox.py` (29 tests) |
| 2 | Windows Job Object containment (kill-tree, mem/process/CPU caps) | VERIFIED | `jarvis sandbox --check` → tier `jobobject`, `contained=True`; grandchild-reap test |
| 3 | POSIX rlimit tier | IMPLEMENTED | code + detection; **not executed on this host** (Windows) — honest gap |
| 4 | Timeout kills the whole process tree | VERIFIED | `test_timeout_reaps_a_grandchild` (psutil-confirmed) |
| 5 | Child never inherits `JARVIS_SECRET_*` / stored-secret values | VERIFIED | `test_child_does_not_inherit_secret_environment`, `test_run_cmd_scrubs_secret_environment`, `test_shell_run_child_cannot_read_a_secret` |
| 6 | Every tool that spawns a process goes through the sandbox | VERIFIED | `shell.run`, `_win.run_cmd`, `media.*` (ffmpeg), `clipboard.*` routed; only `apps.launch` detached on purpose and documented |
| 7 | Audit hash chain + external anchor | VERIFIED | `tests/test_audit_chain.py` (16 tests); `jarvis audit` → chain OK |
| 8 | Tool contract + health for every tool | VERIFIED | `jarvis tools --health` → **85/85 available** |
| 9 | CLI + API surfaces (`sandbox`, `audit`, `/api/sandbox`, `/api/audit/verify`) | VERIFIED | `test_api_sandbox_endpoint`, `test_api_audit_verify_endpoint` |
| 10 | Full suite green and **deterministic** | VERIFIED | 842 passed, 2 skipped; stable under `PYTHONHASHSEED` 1 and default |
| 11 | Documentation updated (repo + canonical record) | VERIFIED | README, docs/SANDBOX.md, docs/EXTENDING.md, docs/CAPABILITY-MAP.md, ARCHITECTURE §11–12, AGENTS.md, project record, CHANGELOG v2.0.9 |
| 12 | Independent security review of the sandbox/audit code | VERIFIED | review run 2026-10-18; 9 findings fixed, 1 accepted-and-documented (unkeyed chain), 1 confirmed-but-out-of-diff |
| 13 | Rollback available | VERIFIED | git history; pre-commit the tree was uncommitted — the work is committed as coherent units |
| 14 | Filesystem / network isolation for untrusted code | **BLOCKED (owner)** | needs Windows Sandbox enabled or Podman; reported unavailable by `jarvis sandbox`, not faked |
| 15 | Real-microphone voice conversation / wake word | **BLOCKED (owner)** | needs the openWakeWord model download + a real mic run; marked PARTIAL |
| 16 | Laptop node re-check | **BLOCKED (environment)** | needs the SL510 reachable (ISSUE-18) |

## Measured evidence (Observed 2026-10-18)

| Command | Result |
|---|---|
| `.venv\Scripts\python.exe -m pytest -q` | **842 passed, 2 skipped** (stable across hash seeds) |
| `python -m jarvis tools --health` | **85/85 available** |
| `python -m jarvis sandbox --check` | exit 0, tier `jobobject`, `contained=True`, probe stdout `sandbox-probe-ok` |
| `python -m jarvis audit` | exit 0, chain OK |
| ruff `--select E9,F` on the new/changed files | clean |

## Failure journal

| # | Problem | Approach / outcome |
|---|---|---|
| 1 | Sandbox child hung forever (killed only at timeout) | Approach A: create suspended → assign → resume. **Failed**: CPython closes the primary-thread handle, so `Popen._thread` is missing. **Strategy reset** → assign immediately after start (documented microsecond window); `int(proc._handle)` needed because the handle is a `subprocess.Handle`. |
| 2 | Memory-cap smoke test reported "ok" | Test bug, not product: allocated 400 MB under a 2048 MB cap. Re-tested at a 96 MB cap → `MemoryError`, as expected. |
| 3 | `tests/test_knowledge.py` failed ~3 in 10 runs | Root cause: the fake embedder bucketed on `hash(w)`, randomised per process. Fixed with an insertion-order map; passes seeds 0–9. The earlier "green suite" was partly luck. |
| 4 | `ffmpeg` and `clipboard` tools spawned processes outside the sandbox (inheriting secrets) | Routed both through the sandbox; made `_win.run_cmd` contained; `apps.launch` documented as the single deliberate detached-launch exception. |

## Independent security review (2026-10-18)

An independent reviewer (read-only) hunted for containment bypasses, handle leaks, limit-enforcement
mistakes, TOCTOU, scrub bypasses, audit weaknesses and false-confidence tests. It confirmed the
Win32 ABI is correct and that limits genuinely reach the kernel, and it found real defects. Findings
and dispositions:

| # | Finding | Sev | Disposition |
|---|---|---|---|
| F1 | A failed `SetInformationJobObject` left the job with **no** limits and **no** kill-on-close, while the result still said `contained=True` (fail-open) | High | **Fixed** — `_configure` returns success; a failure degrades `contained` to False and adds a note; absurd limit values are clamped instead of silently truncated |
| F2 | A malformed anchor **file** made `audit_chain.verify` raise; startup swallowed it, disabling all verification (API returned 500) | Med | **Fixed** — the id is parsed defensively; a malformed anchor reaches `verify_audit`, which fails closed with "anchor is malformed" |
| F3 | A missing/corrupt anchor file was treated as "first run", and the next start re-anchored the truncated chain | Med | **Fixed** — a kv marker records that an anchor was written; a missing file after that is a failure, and a broken chain is never re-anchored; anchor writes are atomic |
| F4 | The chain is unkeyed: an attacker who rewrites the whole log and recomputes every hash verifies | Med | **Accepted, documented** — a key stored beside the database would not stop an attacker who can write the database. Stated as a limit; keyed/signed chain is future work |
| F5 | One tampered (non-ASCII) hash made every later `audit()` raise `UnicodeEncodeError`, stopping audit logging | Med | **Fixed** — the previous hash is encoded with replacement, so `audit()` keeps working and `verify_audit` still reports the break |
| F6 | Kernel-less services (`run_cmd`, ffmpeg) lost value-based scrubbing, so an aliased secret value leaked | Med | **Fixed** — a service built without a kernel reads the secret store from disk for value matching; tested |
| F7 | `apps.launch` started the app with the unscrubbed inherited environment | Low/Med | **Fixed** for `Popen`; `os.startfile` (ShellExecute, no env parameter) is documented as the one unscrubbable path |
| F8 | A runner exception other than `SandboxUnavailable` fell through to an **uncontained re-run** | Low/Med | **Fixed** — only setup failures fall back; a post-start exception returns an error result and never re-runs the command |
| F9 | POSIX tier reported `contained=True` and "wall_timeout enforced" optimistically | Low | **Partly fixed** — status now distinguishes "whole tree killed" from "direct child only". `setrlimit` failures remain unreportable from the child (documented) |
| F10 | `Limits.kill_tree` was a dead config knob | Low | **Fixed** — both tiers honour it; a test asserts the flag reaches the real job |
| F11 | Audit `detail`/`target` can contain a pasted secret and `/api/audit` serves it verbatim | Low | **Recorded, out of this diff** — pre-existing; noted as a follow-up (redact audit detail on read) |

The reviewer's false-confidence critique was acted on too: new tests assert a *downgrade* is reported
(`test_configure_failure_is_reported_not_hidden`), that a runner error is never re-run
(`test_service_never_reruns_a_command_when_the_runner_raises`), value/alias scrubbing without a
kernel, `kill_tree` reaching the job, a malformed anchor file, an anchor deleted after being
recorded, and that one tampered hash no longer stops logging.



## v0.1 Persistent runtime (2026-10-19)

**Objective.** Make JARVIS operate rather than wait: scheduled work that fires,
events that cause reactions, and working memory that survives a restart — with
no new way to bypass policy.

| Requirement | Status | Evidence |
|---|---|---|
| Durable tick loop with single-owner lease + heartbeat | VERIFIED | `test_lease_has_exactly_one_owner`, `test_tick_records_a_heartbeat`, `test_stop_releases_the_lease` |
| A failing tick never kills the loop | VERIFIED | `test_loop_survives_a_failing_tick` |
| Scheduler fires a job once per occurrence | VERIFIED | `test_one_shot_fires_once_and_dedupes`, `test_daily_job_dedupes_per_day_then_fires_the_next` |
| Late jobs are `missed`, not fired hours later | VERIFIED | `test_late_beyond_catch_up_is_recorded_missed_not_run` |
| Interrupted runs never replayed | VERIFIED | `test_reconcile_marks_interrupted_runs_and_never_replays` |
| Event log + declarative rules | VERIFIED | `test_ingest_queues_only_matching_rules` |
| One fire per (rule, event) | VERIFIED | `test_the_same_event_never_fires_twice` |
| Reactions cannot bypass approval | VERIFIED | `test_a_mutating_reaction_still_needs_approval` |
| Durable working memory | VERIFIED | `test_kernel_session_survives_a_rebuild`, `test_agent_persists_its_session` |
| CLI + HTTP surfaces | VERIFIED | `test_api_runtime_surfaces`; manual `runtime rules add` → `events --emit` → reaction `succeeded` |
| Docs | VERIFIED | `docs/RUNTIME.md`, README, ARCHITECTURE §13, CAPABILITY-MAP, EXTENDING, AGENTS |
| HUD Operations surface + live subsystem strip | VERIFIED | `tests/test_ui_runtime.py`; real server on 8765: `/` 200, `/static/app.js` 200, browser opened `/ws`, `/api/runtime` running+owner |
| Planner / vision / cameras | **DEFERRED** | v0.2–v0.5, listed in `docs/RUNTIME.md` and the capability map |

**Failure journal (this slice).** (1) A UTF-8 BOM in a config written by Notepad/PowerShell made
`tomllib` refuse to start the Core — fixed by stripping the BOM, with `tests/test_config.py`.
(2) `SessionStore` silently raised a configured `max_messages` below 10 to 10; the config is now
honoured (floor 2). (3) The scheduler first reported "succeeded" for any finished turn; it now
uses the turn's own final status, so an errored turn is recorded `failed`. (4) The launcher opened
the browser before the server was listening and then ran uvicorn in the foreground, so the user saw
a console and a dead tab. `run-and-open.cmd` now starts the Core hidden, waits for the port, then
opens the HUD; `stop-jarvis.cmd` stops it. Verified by starting the real server, fetching the page,
and seeing the browser open a WebSocket.

## Independent runtime review (2026-10-19)

A second adversarial review targeted the runtime specifically, because it acts without a request.
It confirmed the hard guarantee (per-occurrence and per-fire claims are atomic across connections)
and no approval bypass in the default `ask` path. It also found real defects; all were fixed:

| # | Finding | Sev | Disposition |
|---|---|---|---|
| H1 | The audit chain forked when two processes wrote (serve + CLI): select and insert were separate commits | High | **Fixed** — `BEGIN IMMEDIATE` around read+append serialises writers across processes; regression test with two connections |
| H2 | Reaction dispatch spawned a task per pending row every tick; `max_reaction_tasks` did not bound it | High | **Fixed** — dispatch is capped by tasks *in flight*; test holds tasks open and proves no growth |
| H3 | Scheduled runs were unbounded and could overlap the same job | High | **Fixed** — `max_jobs_in_flight` cap + a running-run overlap guard |
| H4 | `runtime tick` claimed a job, exited, and consumed the occurrence forever | High | **Fixed** — the one-shot tick does not claim jobs unless `--jobs`, and drains/releases when it does |
| H5 | A daily job at 23:59 was silently skipped by a tick at 00:00:30 | High | **Fixed** — `due_occurrence` can look back for yesterday's occurrence within `catch_up_s` |
| M1 | A standby Core reconciled the owner's live claims | Med | **Fixed** — the lease is taken before reconcile; a standby leaves claims alone (test) |
| M2 | A corrupt stored condition failed **open** (became a wildcard) | Med | **Fixed** — the rule is skipped, not widened |
| M3 | Removing/disabling a rule did not stop queued fires | Med | **Fixed** — fires are deleted on removal; `enabled` is re-checked at claim |
| M4 | A standby ingested bus events; pending fires had no age bound | Med | **Fixed** — the tap is owner-gated; retention prunes old rows |
| M5 | Concurrent reactions of one rule shared one writable session | Med | **Fixed** — one session per fire |
| M6 | The documented `notify` action needed approval, so it could not alert unattended | Med | **Fixed** — `notify` writes a durable alert row; a toast is attempted only if policy would not prompt |
| M7 | "No unsupervised mutation" was false under `mode = autonomous` | Med | **Fixed in docs** — autonomy is the mode's definition and now stated for reactions |
| M8 | A retried `POST /api/events` ran the reaction twice | Med | **Fixed** — `Idempotency-Key`/`event_id` + unique `dedupe_key` |
| M10/M11 | Unbounded growth; forged internal topics attributed to "owner" | Med | **Fixed** — retention/pruning; conditions see `source`/`topic`; audit actor is `api:<host>` |
| M12 | Several tests would pass with the feature broken | Med | **Fixed** — new tests for midnight, in-flight bounds, standby reconcile, corrupt conditions, removal, idempotency, two writers |
| L1–L5 | Session trim orphaned tool messages; unbounded session cache; optimistic status; env list became a string; rule tool not validated | Low | **Fixed** — whole-exchange trim, bounded cache, honest owner/health, list coercion, tool check at creation |

The one guarantee that held before the fixes and still holds: **a crash or a second Core cannot
fire the same occurrence or react to the same event twice**, and a reaction cannot bypass the policy
gate. What was not safe was leaving it running unattended; that is what these fixes address.

## Live-use corrections (2026-10-19 evening)

Real use exposed defects that the unit suite could not: the first conversation refused every turn, and
"open steam" tried to open a website. Found and fixed:

- **The "fault / failed" was Ollama not running.** `GET /api/health` reported it honestly
  (`llm.ok=false`, "All connection attempts failed") but the HUD's reactor only said FAULT. Started
  Ollama; both models are present. Follow-up: make the HUD's reactor subtext name the failing
  subsystem (e.g. "model offline") instead of a generic fault.
- **"open steam" opened `https://steam.com`.** Two causes: the app allowlist was empty, and a small
  model routes "open" to `web.open`. Fixed with (a) a reviewable default allowlist, (b) an
  installed-aware `apps.list`, (c) `apps.launch` preflight that refuses a missing app *before* an
  approval prompt, (d) explicit tool descriptions and a persona TOOL ROUTING block, and (e) a
  deterministic route in the agent for an unambiguous `open <installed app>` — still through the
  controller, so the approval gate is intact. Live: "open steam" asked for
  `apps.launch {"app":"steam"}` and replied "Opening steam, sir."
- **Critical integrity alerts on every code edit.** The watcher fingerprinted `jarvis/config.py`
  and the example TOML. It now tracks runtime state only (active config, secrets, db); the baseline
  went from 5 files to 3 and a restart after code changes is quiet.
- **Telemetry was never linked.** The HUD asked for metric `cpu` (stored as `cpu.percent`), could not
  read the `series` response, and listened for `metric` while the bus publishes `metrics`. All three
  fixed; HTTP polling no longer depends on the WebSocket, so a reconnecting socket cannot blank the
  gauges.
- **Voice used Piper, not the JARVIS voice.** The live config now selects Kokoro with `bm_fable`;
  audio is unlocked on the first gesture (autoplay), alerts are spoken, barge-in was made less
  twitchy, and `num_predict` was raised so replies do not stop mid-sentence. Measured: 34 words →
  12.9 s Kokoro (no truncation), live `/api/tts` → `bm_fable`, 4.37 s, speech true.
- **HUD recovery.** Pending approvals are re-fetched on load and reconnect (the missed approval was
  why "open steam" stayed `awaiting-authorization`); replayed frames are deduped by `seq`; the
  runtime heartbeat/metrics no longer flood the Activity feed; the four-row grid is restored after
  the subsystem strip; a persisted UI scale (default 90%) is in Settings.

## Completion pass: planner, self-diagnostics, MCP (2026-10-20)

**Objective.** Close the structural gaps between "a capable assistant" and an
operating system: multi-step planning, self-monitoring, and usability by other
agents — without weakening the gate.

| Requirement | Status | Evidence |
|---|---|---|
| Durable plan: request → validated steps → stored | VERIFIED | `test_goals.py` (9 tests) |
| Step execution through the controller, in order, once | VERIFIED | `test_advance_runs_steps_in_order_and_finishes` |
| Retry then fail; crash never replays | VERIFIED | retry/reconcile tests |
| A plan can only name registered tools | VERIFIED | `test_plan_drops_unknown_tools_and_ends_with_an_answer` |
| Self-diagnostics across every subsystem | VERIFIED | `test_ops.py` (5 tests) |
| One bounded, audited, cooldown-limited repair | VERIFIED | `test_tunnel_repair_runs_once_per_cooldown` |
| MCP stdio server, read-only by default | VERIFIED | `test_mcp.py` (7 tests) + a real subprocess round-trip |
| Local OCR for images and the live screen | VERIFIED | `test_vision.py` (5 tests); a real 1920×1080 capture OCR'd in 1.9 s |
| Tool choice at 104 tools | VERIFIED | relevance shortlist (≤24 offered) + exact-repeat refusal; `test_agent_selection.py` (5 tests) |
| Anton questions grounded in a real read | VERIFIED | "what is Anton doing?" → exactly `anton.status` + `anton.jobs`, factual answer; no `nodes.status` loop |
| Wake-word models installed (opt-in) | VERIFIED | `hey_jarvis_v0.1.onnx`, `melspectrogram.onnx`, `embedding_model.onnx` present |
| Docs + config for all three | VERIFIED | `docs/GOALS.md`, `docs/OPS.md`, `docs/MCP.md`, README, CAPABILITY-MAP |
| Planner re-planning after a failed step | **NOT DONE** | deliberately deferred; the goal fails with its error |
| Screen OCR / vision | **DONE** | `vision.ocr`, `vision.screen` (RapidOCR, local) |
| Homelab skills (Docker/Plex/NAS) | **BLOCKED** | no Docker/Podman; PC-002 down; no service to verify against |
| Cameras / physical events | **BLOCKED** | no camera/device integration installed |

**Measured.** 842 passed, 2 skipped. Live MCP round-trip: initialize, 41 read-only
tools, `time.now` succeeded, `files.write` refused with the approval reason.
Wake models downloaded from the openWakeWord release.

## Night audit (2026-10-20)

Bounded Night Protocol run: baseline → read-only audit → targeted fixes → tests → re-audit.
`jarvis/tools/builtin/anton.py` was untracked user work and was **not** modified, staged or
committed. No commit was made; the rollback is the working tree (`git checkout -- <path>`).

**Baseline.** 842 passed, 2 skipped (96.33 s); 104/104 tools; sandbox `jobobject`,
`contained=True`; audit chain OK (124 rows, 34 legacy); runtime enabled but not running, with a
stale lease from 15:49 still reported at 22:30. `jarvis doctor`: all dependencies present,
Ollama installed but **not reachable** (honest failure).

| # | Finding | Sev | Disposition | Regression |
|---|---|---|---|---|
| N1 | `files.*` exposed `allowed_roots` to the model, so a prompt-injected call could widen its own file boundary; they also skipped the protected-root checks that `file_ops` had, so `data/api-token`, `node-token`, `~/.ssh` and `secrets.json` were readable with `files.read` (auto-allowed). | High | **Fixed** — parameter removed; trusted controller roots only (`services["allowed_roots"]`); protected roots moved to `files.py` and shared by `file_ops`/`vision.ocr`; token files, audit anchor and DB sidecars added. | `tests/test_files_boundary.py` (9) |
| N2 | `apps.launch` passed model-supplied `args` to an allowlisted binary. With the default `explorer.exe` entry, `explorer.exe <file>` executes an arbitrary file, so the allowlist was an escape hatch. A dead duplicate `preflight` also hid the real one. | Med/High | **Fixed** — args are refused unless the owner entry opts in (`{target=..., allow_args=true}`); duplicate removed; args checked before approval. | `test_extra_args_are_refused_before_an_approval`, `test_owner_can_opt_into_arguments_per_app` |
| N3 | `web.fetch` (auto-allowed) accepted loopback/private/link-local/metadata URLs, including JARVIS's own API and a local SearXNG/router. A page could use it as an SSRF/local-read proxy. | Med/High | **Fixed** — private-target guard (literal + DNS); `[security] allow_private_fetch=false` by default; owner SearXNG remains exempt. | `tests/test_web_ssrf.py` (6) |
| N4 | Audit `detail`/`target` stored and served verbatim, so a pasted secret was captured (F11). | Med | **Fixed** — key-based redaction plus known `SecretStore` values and runtime `data/api-token`, `data/node-token`, `data/anton-token` files are scrubbed before chaining; token files are re-read per scrub for rotation. Arbitrary unregistered secrets remain a stated residual. | `test_audit_redacts_secret_values_at_the_write_boundary`, `test_runtime_token_files_are_redacted_and_rotation_is_picked_up` |
| N5 | A target-scoped grant matched a call with **no** target (`not target` was treated as in-scope), widening “may write `C:\safe\*`” to any write. | Med | **Fixed** — a target-scoped grant requires a target that matches. | `test_target_scoped_grant_does_not_widen_to_a_targetless_call` |
| N6 | Runtime `status` printed a lease that expired hours earlier as if it were live; health called it a live standby. | Low | **Fixed** — `lease_valid` / `lease_expired` and an honest health reason. | `test_a_stale_lease_is_reported_as_expired_not_as_a_live_standby` |
| N7 | `scripts/write-core-config.py` printed the node token line to stdout. | Low | **Fixed** — prints present/empty only. | manual source check |
| N8 | The system prompt did not tell the model that tool output is untrusted; a fetched page could ask it to read the screen/memory and send data out. | Med | **Mitigated** — explicit `UNTRUSTED DATA` prompt block. This is defence in depth, not a hard boundary; the file/SSRF/approval fixes are the boundaries. | `test_persona_treats_tool_output_as_untrusted_data` |
| N9 | `files.read` hashed the whole file before returning its capped prefix, so a read-only auto-allowed call on a multi-GB file caused a full-disk read (cheap I/O DoS). | Low/Med | **Fixed** — full hash only up to 32 MiB; larger files return `sha256: null`, `sha256_skipped: true`, and the bounded prefix. | `test_read_does_not_hash_a_huge_file` |

**Measured after fixes.** 864 passed, 2 skipped (91.34 s); 104/104 tools; sandbox unchanged;
`jarvis audit` chain OK; runtime status exits 0 and now reports the stale lease as expired.
Core idle RSS measured **59.2 MB** with voice disabled and runtime/monitoring off (startup
4.06 s); `python -m jarvis tools --health` peak **96.8 MB** (1.16 s). The 7B model was not
loaded during this run (Ollama stopped); measured host: 15.89 GiB RAM, RTX 3080 10 240 MiB
VRAM (8 953 MiB free), 7B weights 4.7 GB. **APOS (16 GiB / GTX 1660S 6 GiB) needs the 3B/4B
profile** — no server profile exists yet; this is a proposal, not a claimed feature.

**Rollback.** Every change is uncommitted working-tree state. Revert all audit changes with
`git checkout -- README.md config/jarvis.example.toml docs/BUILD-NOTE.md docs/CAPABILITY-MAP.md `
`jarvis/ scripts/write-core-config.py tests/` and delete the two new test files; `anton.py` is
untracked and must not be touched.

## Assumptions

- The owner wants the sandbox **on** and secrets scrubbed from children — recorded, not asked (safe default, easily changed in `[sandbox]`).
- Media transcodes should be contained (tree-kill, scrubbed env) but **not** resource-capped, since a legitimate transcode can exceed shell defaults.
- `apps.launch` must not be sandboxed, because a launched app is meant to survive the call.

## Known limits (not hidden)

- Filesystem and network are **not** isolated; a Job Object/rlimit limits resources, not reach.
- The audit chain is **unkeyed**: it detects edits/deletions against the anchor, but an actor who can
  write the database *and* recompute every hash is not detected. A key stored beside the database
  would not change that, so it is deferred rather than faked (F4).
- The Windows job is assigned microseconds after `CreateProcess` (a `PROC_THREAD_ATTRIBUTE_JOB_LIST` STARTUPEX is the future fix).
- On POSIX, a `setrlimit` that the kernel refuses is not reported from inside the child (partial F9).
- Audit `detail`/`target` are stored and served verbatim, so a secret pasted into a request would be
  captured in the log (F11) — a follow-up, pre-existing.
- The POSIX rlimit tier is implemented and detected but not exercised on this Windows host.
- `runtime_events` has **no retention policy** yet: it grows without bound (follow-up).
- The runtime lease is **not a distributed lock**; the per-occurrence scheduler claim is what makes
  double-firing impossible. Two Cores reading an expired lease at the same instant could both tick.
- Rule `condition` is a small fixed-operator predicate; it deliberately cannot express arbitrary
  expressions. Event data is treated as data, and a `request` action's prompt is fixed text.
- 13 pre-existing `E9/F` lint items (unused imports) remain in files outside this change; the project configures no linter, so this is hygiene noise, not a defect. One (an unused local in `compute/engine.py`) is a harmless dead assignment.

## How to run / test

```powershell
.\.venv\Scripts\python.exe -m jarvis serve            # Core + HUD at http://127.0.0.1:8765/
.\.venv\Scripts\python.exe -m jarvis sandbox --check  # containment posture + probe
.\.venv\Scripts\python.exe -m jarvis audit            # audit-chain verification
.\.venv\Scripts\python.exe -m pytest -q               # full suite
```

## Rollback

Each change is committed in a coherent unit; `git revert <sha>` (or `git checkout <sha>^ -- <path>`)
returns the affected files to the previous state. The two commits for this work are:

- `5c84e11` — docs(jarvis): build note, sandbox and extending guides, capability map, agent instructions
- `c6abda7` — feat(jarvis): integrate prior-session assistant work; add process sandbox and tamper-evident audit

Previous baseline: `fc5c0ce`. The sandbox can be disabled without a revert by
setting `[sandbox] enabled = false`; the audit chain cannot destroy evidence — `verify_audit` fails
closed and the anchor is only advanced when the chain is intact.

## Related

[README](../README.md) · [SANDBOX](SANDBOX.md) · [EXTENDING](EXTENDING.md) ·
[CAPABILITY-MAP](CAPABILITY-MAP.md) · [Project record](../../../../Documentation/Projects/AI/Jarvis/Read-Me.md)

## 2026-10-21 post-night hardening (v2.0.23 work)

- `jarvis/logging_setup.py` now scrubs common credential shapes in free text (bearer, JWT, OpenAI,
  GitHub, Slack, AWS access key, password/token assignment, private-key blocks) in addition to
  key-based redaction; JSONL `msg` and `exc` pass through the same boundary.
- `jarvis/policy/permissions.py`: in-memory session grants (`add_session_grant`, `revoke_grant`,
  `active_grants`); cleared on Core restart. `TaskController.resolve_approval` accepts
  `remember_session`; `/api/approvals/{id}` accepts `remember_session`; HUD adds
  **Allow for 1 hour** and **Allow for this session**.
- `jarvis/core/agent.py`: voice turns use `llm.fast_model` when configured; unambiguous time/date
  requests use a deterministic `time.now` fast path. `config/jarvis.toml` currently keeps
  `fast_model=""` after benchmarking showed PC 3B was not faster than warm 7B and pushed VRAM to
  9.4/10.2 GB.
- `scripts/jarvis-core-supervisor.py` and `scripts/jarvis-tunnels.py`: console-free supervisors,
  `CREATE_NO_WINDOW`, kill-on-close job objects. Task Scheduler uses `pythonw`, not PowerShell.
- APOS/laptop contract: loopback-only node/Ollama, SSH local forwards (APOS `AllowTcpForwarding
  local`); APOS 3B `keep_alive=-1` + boot warmup; stale `jarvis-v2` service disabled.
- Tests: `pytest -q` -> **869 passed, 2 skipped**. `node --check jarvis/ui/app.js` passes.
- Residual unchanged or newly explicit: PC-001 ICS no NAT/APOS offline; no TLS/WireGuard for
  `--lan`; no filesystem/network sandbox; DNS-rebinding TOCTOU; full Linux core port; Anton TLS;
  AMSD server.properties/checksums; Plex unclaimed; 32 GiB RAM/UPS/off-site.
