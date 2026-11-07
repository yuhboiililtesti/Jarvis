# JARVIS Persistent Runtime

**Status:** Active guide (Observed: 842-test suite, runtime tests green, 2026-10-19) | **Owner:** Topaz | **Last updated:** 2026-10-19

The part that makes JARVIS *operate* rather than *answer*. Before this, the Core
did something only while a request was being answered; everything stopped when
the conversation did. The runtime is a durable loop that keeps working while the
Core is up.

## What runs

```
            ┌──────────────── RuntimeService (one tick) ────────────────┐
            │ 1. claim/refresh the single-owner lease                   │
            │ 2. fire due scheduled jobs (once per occurrence)          │
            │ 3. dispatch pending event reactions (policy-gated)        │
            │ 4. write a heartbeat                                      │
            └───────────────────────────────────────────────────────────┘
```

| Component | File | Responsibility |
|---|---|---|
| Tick loop | `runtime/service.py` | lease, bounded work per tick, heartbeat, backoff. A failing tick never kills the loop. |
| Scheduler | `runtime/scheduler.py` | claims each due occurrence, runs the request, records the outcome |
| Event engine | `runtime/events.py` | persists events, matches declarative rules, dispatches reactions |
| Sessions | `runtime/sessions.py` | durable working memory (the conversation survives a restart) |

## The guarantees

Each of these is a test, not a promise:

| Guarantee | How | Test |
|---|---|---|
| A job fires **once per occurrence** | a run row is claimed by `INSERT OR IGNORE` against `UNIQUE(job_id, due_key)` *before* anything happens | `test_one_shot_fires_once_and_dedupes`, `test_daily_job_dedupes_per_day_then_fires_the_next` |
| An event triggers a rule **once** | `UNIQUE(rule_id, event_id)`; `execute_fire` claims `pending → running` atomically | `test_the_same_event_never_fires_twice` |
| A crash never causes a silent replay | claims left `pending`/`running` are reconciled to `outcome-unknown` and not re-run | `test_reconcile_marks_interrupted_runs_and_never_replays`, `test_reconcile_marks_an_interrupted_reaction` |
| A job hours late is **not** a surprise | later than `catch_up_s` it is recorded `missed` and does not run | `test_late_beyond_catch_up_is_recorded_missed_not_run` |
| An event **cannot bypass the policy gate** | reactions call `TaskController.execute_tool`; in `ask` mode a gated tool still asks | `test_a_mutating_reaction_still_needs_approval` |
| Dispatch and runs are **bounded in flight** | tasks-in-flight caps, not rows-per-tick | `test_tick_bounds_reactions_in_flight`, `test_scheduler_caps_in_flight_and_never_overlaps_a_job` |
| A retried delivery does not act twice | `Idempotency-Key` / `event_id` + unique `dedupe_key` | `test_idempotent_ingest_creates_one_event_and_one_fire` |
| A removed or disabled rule stops queued fires | fires deleted on removal; `enabled` re-checked at claim | `test_removing_a_rule_cancels_queued_fires`, `test_a_fire_for_a_disabled_rule_does_not_run` |
| The conversation survives a restart | sessions table + save in `run_turn`'s `finally` | `test_kernel_session_survives_a_rebuild`, `test_agent_persists_its_session` |
| Only one Core owns the loop | lease row in `kv`, refreshed each tick | `test_lease_has_exactly_one_owner` |
| A bad tick does not stop the runtime | per-tick `try`, audited error, backoff | `test_loop_survives_a_failing_tick` |

## Configuration

```toml
[runtime]
enabled = true
tick_s = 2.0                # how often due work is checked
lease_ttl_s = 30.0          # a dead Core's lease expires and another takes over
max_jobs_per_tick = 5
max_jobs_in_flight = 2      # running turns, not just claims per tick
max_reactions_per_tick = 10
max_reaction_tasks = 4      # reactions in flight (one may block on approval)
catch_up_s = 3600.0         # later than this = 'missed', not fired
session_max_messages = 200
rule_cooldown_s = 5.0       # bounds a reaction that produces the event it reacts to
event_retention_days = 30   # prune old events/fires on start
max_events = 5000
event_topics = ["alert", "security.*", "honeypot.*"]   # bus topics mirrored into the event log
```

`event_topics` is deliberately narrow. Mirroring every bus topic would create a
firehose and, because a reaction can itself publish, a possible feedback loop.

## Scheduling a job

The `scheduler.*` tools record a job; the runtime fires it. A job is a **request
to run**, not a raw tool call, so it goes through the agent and the normal safety
path.

```powershell
# from a chat turn, or directly with the tool
python -m jarvis chat "schedule a daily status check at 07:30"
# the runtime fires it once per day at 07:30, at most once per occurrence
```

## Events and rules

Rules are **console-only** — the same reason plugins are. A remote client may
*ingest* an event, but it cannot define a new action.

```powershell
# react to a disk warning by asking JARVIS to look into it
python -m jarvis runtime rules add disk-watch --topic "disk.*" `
  --condition '{"field": "smart", "op": "==", "value": "warning"}' `
  --request "A disk reported a SMART warning. Investigate, check backups, and tell me what you did."

# a durable alert (written to the alerts table, visible in the HUD; a desktop
# toast is attempted only if policy would not prompt for it)
python -m jarvis runtime rules add notify-plex --topic "plex.stopped" --notify "Plex stopped"

# a tool action (still policy-gated at execution time)
python -m jarvis runtime rules add snapshot --topic "night.finished" --tool files.hash --args '{"path": "C:/backups/latest.zip"}'

# test it now (ingest runs the matching reactions immediately)
python -m jarvis runtime events --emit disk.smart_warning
```

A condition is a JSON predicate — `field`, `op`, `value` — with a fixed operator
set (`== != > >= < <= contains startswith exists`). It is data, never evaluated
as code. A corrupt condition **skips** the rule rather than widening it. The
condition can inspect `source` and `topic` as well as the payload, so a rule can
distinguish a real bus alert from an event posted over HTTP. Event payloads are
treated as **data**; a `request` action's prompt is the fixed text you wrote, so
an attacker who can post an event cannot turn it into instructions.

## Bounds (why a burst cannot eat the Core)

- The scheduler caps **running** turns (`max_jobs_in_flight`) and refuses to
  start a second run of a job while one is still running.
- Reaction dispatch is capped by tasks **in flight**, not by rows returned per
  tick, so pending rows cannot spawn an unbounded number of asyncio tasks.
- A per-rule cooldown (`rule_cooldown_s`) stops a reaction that produces the
  event it reacts to from looping.
- Old events and fires are pruned on start (`event_retention_days`,
  `max_events`).

## Inspecting it

```powershell
python -m jarvis runtime            # status: lease, ticks, jobs, runs, events
python -m jarvis runtime tick       # one step now (useful for testing a rule)
python -m jarvis runtime rules list
python -m jarvis runtime events
```

HTTP (read-only for rules): `GET /api/runtime`, `GET /api/runtime/rules`,
`GET /api/runtime/events`. `POST /api/events` ingests an event (a sensor, a
script, Home Assistant); its reactions still pass policy and approval.

## In the HUD

The **Operations** tab shows the runtime as it really is: state, owner and lease,
tick count and last tick, reactions and runs in flight, session count, then the
scheduled jobs (with next run), recent runs, event rules and recent events. Two
controls are real, not decorative:

- **Run tick** calls `POST /api/runtime/tick` (jobs opt-in) and reports what the
  tick did; it does not claim scheduled work.
- **Emit event** posts to `/api/events` and shows how many rules matched — useful
  for testing a rule without waiting for a sensor.

The **subsystem strip** under the top bar shows CORE / RUNTIME / SANDBOX / AUDIT
from the real endpoints (the audit chip turns red if the chain does not verify).
Clicking a chip opens the panel that explains it. The tab redraws on
`runtime.tick` only while it is visible, so the two-second tick costs nothing
when you are elsewhere.

## What this is **not** (yet)

- **No planner.** A job or a reaction is one request. Durable multi-step goals
  are v0.2.
- **No bypass — but autonomy follows your policy.** Reactions go through the same
  gate as a spoken request. In the default `ask` mode anything mutating asks. In
  **`autonomous` mode**, non-destructive tools run without asking — that is what
  the mode means, and an event rule inherits it. Keep `ask` (or a narrow
  `auto_allow` list) if unattended reactions must stay read-only. Destructive
  actions still ask in every mode.
- **A one-shot `runtime tick` is diagnostics.** It does not claim scheduled jobs
  unless you pass `--jobs`, because a process that exits cannot finish the turn;
  and approvals cannot be answered from it, so a gated reaction times out.
- **No vision.** `screen.capture` saves an image; nothing interprets it.
- **No event retention *policy decisions*.** The log is pruned on start, but you
  choose the window (`event_retention_days`, `max_events`); nothing is archived.
- **Not every event source is authenticated.** `POST /api/events` is a trusted
  local/LAN surface; a caller can forge an internal topic such as `alert`. Rules
  can check `source` (`api` vs `bus`) and should do so for anything sensitive.
- **The lease is not a distributed lock.** Two Cores reading an expired lease at
  the same instant could both write it; the per-occurrence claim is what makes
  double-firing impossible. The lease only decides who ticks.
- **Removing a rule cancels queued reactions, not one already inside a tool call.**
  That one re-checks `enabled` before acting, but a call already awaiting approval
  is in progress.
- **Approval-bound reactions hold a concurrency slot** for up to
  `policy.approval_timeout_s`; `max_reaction_tasks` is the bound.

## Related

[ARCHITECTURE](../ARCHITECTURE.md) · [SANDBOX](SANDBOX.md) · [EXTENDING](EXTENDING.md) ·
[CAPABILITY-MAP](CAPABILITY-MAP.md) · [BUILD-NOTE](BUILD-NOTE.md) ·
[Blueprint §04 Agent runtime](../../../../Documentation/Blueprints/AI-Blueprint/04_AGENT_RUNTIME.md)
