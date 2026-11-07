# JARVIS Goals — durable planning and execution

**Status:** Active guide (Observed: 842-test suite, goals tests green, 2026-10-20) | **Owner:** Topaz | **Last updated:** 2026-10-20

A **goal** is a request that needs more than one action: *inspect → decide → act →
verify*. JARVIS plans it once with the model, stores the plan, and the runtime
executes the steps one at a time — across restarts, through the same approval gate
as a spoken request.

## What happens

```
request
  → planner model returns JSON steps (tool | request | answer)
  → validated: only registered tools, bounded count, one answer at the end
  → stored in goals + goal_steps
  → runtime advances one step per tick
      tool    → TaskController.execute_tool (policy, approval, verifier)
      request → one agent sub-turn
      answer  → final text, goal succeeds
```

A step that fails is retried up to `goals.max_attempts`; after that the goal is
`failed` and the error is kept. A crash leaves `running` steps as
`outcome-unknown`; they are **never replayed**.

If the closing answer is a placeholder such as `[time]`, or a generic "Done.",
the executor folds the succeeded step results into a short final reply (one
bounded model call); otherwise the answer text is used as written.

## Guarantees

| Guarantee | How | Test |
|---|---|---|
| A plan can only name real tools | validated against the registry at plan time | `test_plan_drops_unknown_tools_and_ends_with_an_answer` |
| An unparseable plan still works | falls back to one agent sub-task + answer | `test_plan_falls_back_to_one_request_and_closes` |
| Steps run in order, once | `UPDATE … WHERE status='pending'` claim | `test_advance_runs_steps_in_order_and_finishes` |
| A bad step retries then fails the goal | attempts counted per step | `test_a_failing_step_retries_then_marks_the_goal_failed` |
| Cancel stops pending steps | goal + pending steps set `cancelled` | `test_cancel_stops_pending_steps` |
| Two ticks never overlap one goal | SQL excludes goals with a running step | `test_run_pending_skips_a_goal_with_a_running_step` |
| A crash never replays a step | reconcile → `outcome-unknown` | `test_reconcile_marks_interrupted_steps` |

## Configuration

```toml
[goals]
enabled = true
max_steps = 8
max_attempts = 3
auto_run = true       # the runtime advances planned goals
per_tick = 1          # one step per goal per tick
```

## Using it

```powershell
python -m jarvis goals create --request "check disk space, then list the largest folders"
python -m jarvis goals create --request "..." --plan-only   # show the plan, store nothing
python -m jarvis goals list
python -m jarvis goals show <goal_id>
python -m jarvis goals run <goal_id>                        # advance one step now
python -m jarvis goals cancel <goal_id>
```

Tools: `goals.create` (with `dry_run`), `goals.list`, `goals.status`,
`goals.cancel`. HTTP: `POST /api/goals`, `GET /api/goals`,
`GET /api/goals/{id}`, `POST /api/goals/{id}/run`, `POST /api/goals/{id}/cancel`.

## Limits (stated, not hidden)

- **No re-planning yet.** If a step fails permanently the goal fails; it does not
  ask the model for a different plan. That is the next iteration.
- **One step per goal per tick.** Deliberate: it keeps the resource use bounded.
- **Approval-bound steps** wait like any task; in the background they hold a slot
  until `policy.approval_timeout_s` expires (then they are denied).
- The planner is a 7B model: it can produce a poor decomposition. The stored plan
  is visible with `goals show`, and every effect is still gated.

## Related

[RUNTIME.md](RUNTIME.md) · [ARCHITECTURE.md](../ARCHITECTURE.md) · [EXTENDING.md](EXTENDING.md) ·
[CAPABILITY-MAP.md](CAPABILITY-MAP.md) · [Blueprint §04](../../../../Documentation/Blueprints/AI-Blueprint/04_AGENT_RUNTIME.md)
