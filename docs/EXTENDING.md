# Extending JARVIS

**Status:** Active guide (Observed: plugin loader and contract tests pass, 2026-10-18) | **Owner:** Topaz | **Last updated:** 2026-10-18

How to add a capability so it is gated, verified, auditable and visible from day one.

## Pick the right extension point

| You want to… | Add | Where |
|---|---|---|
| Let JARVIS *do* something new | a **tool** | `jarvis/tools/builtin/<area>_tool.py` (core) or a **plugin** (`data/plugins/<id>/`) |
| Run something continuously (watch, poll, listen) | a **service** | a class taking the kernel, registered in `kernel._try_build_optional` |
| Do something later, automatically | a **scheduled job** | `scheduler.*` tools (the runtime fires it) — [RUNTIME.md](RUNTIME.md) |
| React when something happens | an **event rule** | `python -m jarvis runtime rules add …` (console-only) — [RUNTIME.md](RUNTIME.md) |
| Plan a multi-step objective | a **goal** | `goals.*` tools / `python -m jarvis goals` — [GOALS.md](GOALS.md) |
| Let another agent call JARVIS | the **MCP server** | `python -m jarvis mcp` — [MCP.md](MCP.md) |
| Expose a new HTTP surface | a **router** | `build_<x>_router(kernel)` included from `api/app.py` |
| Prove behaviour end to end with the model | an **eval task** | `jarvis/eval/suites/*.json` |
| Change how JARVIS speaks | the **persona** | `jarvis/persona/persona.py` |
| Run a process | the **sandbox** | `ctx.service("sandbox").run_async(argv, …)` — never `subprocess` directly ([SANDBOX.md](SANDBOX.md)) |

Prefer a plugin for personal or experimental tools: it needs no core edit and can be withdrawn by
disabling it. Promote it to a builtin once it is stable and tested.

## The tool contract

Every tool is a `BaseTool` subclass with a `ToolSpec` and an `async run(ctx, **kwargs)`
([base.py](../jarvis/tools/base.py)). The rules below are enforced by
[`tests/test_tool_contract.py`](../tests/test_tool_contract.py) or by policy:

1. **True side effect.** `none / network / local_write / local_exec / remote / destructive`.
   Policy, approvals and DEFCON gate on it. Deletes, overwrites, power-off and kill are
   `destructive`.
2. **Never raise out of `run()`.** Return `ToolResult(ok=False, error=…)` with an actionable reason.
3. **Preview.** State-changing tools add the shared `dry_run` parameter and return
   `_common.preview(...)` — exact target, operation, effect, reversibility, rollback — without
   acting ([_common.py](../jarvis/tools/builtin/_common.py)). Approvals show this preview.
4. **Read-back.** Implement `verify(ctx, result)` with an independent check (re-read the file,
   re-query the state). If no real read-back exists, return `None`: the attempt is recorded as
   *unverified*, which is honest. Never return a fabricated `verified: True`.
5. **Health.** A module-level `health() -> {"available": bool, "reason": str}` reports missing
   dependencies or unsupported platforms. `python -m jarvis tools --health` and
   `GET /api/tools/health` aggregate it.
6. **Lazy optional imports.** Import heavy or optional packages inside the function so the Core
   starts without them.
7. **Secrets only via `ctx.secrets(name)`.** Never read secret files directly, never log values.
8. **Data, not instructions.** Text fetched from the web, documents or devices is returned as
   structured data; never phrase tool output as commands to the model.
9. **No parameter named `ctx`.** Avoid `name` too (see the note in [apps.py](../jarvis/tools/builtin/apps.py)).
10. **Run processes through the sandbox.** A tool that starts a process must call
    `ctx.service("sandbox").run_async(argv)` (or `_win.run_cmd`), never
    `subprocess` directly: the sandbox applies resource limits, kills the whole
    process tree, and keeps `JARVIS_SECRET_*` out of the child's environment
    ([SANDBOX.md](SANDBOX.md)). The one exception is `apps.launch`, which starts an
    allowlisted application **detached on purpose** so it outlives the call.

## Writing a plugin

1. Copy [`plugins/_template/`](../plugins/_template) to `data/plugins/<id>/`.
2. Edit `plugin.toml`: `id` (equals the folder name), `version`, `summary`, `modules`, and
   `max_side_effect` — the highest side-effect class any of its tools may declare.
3. Write tools exactly as above, registering them with `@register()`.
4. Approve the exact files: `python -m jarvis plugins enable <id>`. This shows the modules,
   ceiling and SHA-256 fingerprint, then pins it. Restart the Core to load.
5. Check: `python -m jarvis plugins list` and `python -m jarvis tools --health`.

What the loader refuses (Observed in [`tests/test_plugins.py`](../tests/test_plugins.py)):

- anything not enabled by the owner;
- any plugin whose files changed after approval (until re-enabled);
- a tool above the approved `max_side_effect` (the whole plugin is backed out);
- a tool that would replace an existing tool (the original is restored);
- an invalid manifest.

Enabling is console-only on purpose: `GET /api/plugins` is read-only, so a remote client can never
cause new code to run.

**Limit:** plugin code runs in-process with the Core's rights. The pin and ceiling stop silent
change and over-reach by declared tools; they are **not** a sandbox against hostile code. Enable
only code you have read.

## Checklist before calling a new capability done

- [ ] `python -m pytest -q` passes, including the contract test.
- [ ] A test covers the success case, a boundary and the important failure case, with any
      Windows or network call mocked.
- [ ] `dry_run` preview and `verify()` behave as described (or `verify` honestly returns `None`).
- [ ] `tools --health` reports a useful reason when the dependency is missing.
- [ ] Read-only tools that should run without prompting are added to `policy.auto_allow`;
      nothing mutating is.
- [ ] Any process it starts goes through the sandbox (`ctx.service("sandbox")`), and a test
      proves a child cannot read a `JARVIS_SECRET_*` value ([SANDBOX.md](SANDBOX.md)).
- [ ] If the model should use it, an eval task in `jarvis/eval/suites/` proves it does.
- [ ] [CAPABILITY-MAP.md](CAPABILITY-MAP.md) and the project record are updated.

## Related

[AGENTS.md](../AGENTS.md) · [ARCHITECTURE.md](../ARCHITECTURE.md) ·
[CAPABILITY-MAP.md](CAPABILITY-MAP.md) ·
[Shared coding system](../../../../Documentation/Agents/Coding/Read-Me.md) ·
[Blueprint: tool and plugin system](../../../../Documentation/Blueprints/AI-Blueprint/05_TOOL_PLUGIN_SYSTEM.md)
