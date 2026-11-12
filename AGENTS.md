# JARVIS — Agent Instructions

Read the shared coding system first: `C:/Users/<USER>/Documentation/Agents/Coding/Read-Me.md`,
then `rules/coding.md`, plus the one rule module the task triggers.

## Project facts

- Purpose: local-first, free, open-source personal assistant (production Core on APOS; WIN11 is an interactive client, laptop a satellite).
- Production Core: APOS `jarvis-core.service`, using `config/jarvis-core.toml` and `data-core`. Do not launch a duplicate production Core on WIN11.
- Development-only local Core: `.venv\Scripts\python.exe -m jarvis serve` (http://127.0.0.1:8765/); first verify it will not conflict with the client tunnel or production topology.
- Test: `.venv\Scripts\python.exe -m pytest -q`
- Behavioural eval (needs Ollama): `.venv\Scripts\python.exe -m jarvis eval core`
- Tool availability: `.venv\Scripts\python.exe -m jarvis tools --health`
- Sandbox posture: `.venv\Scripts\python.exe -m jarvis sandbox --check`
- Audit chain: `.venv\Scripts\python.exe -m jarvis audit`
- Runtime: `.venv\Scripts\python.exe -m jarvis runtime [status|tick|rules|events]`
- Goals: `.venv\Scripts\python.exe -m jarvis goals [list|show|create|run|cancel]`
- Self-check: `.venv\Scripts\python.exe -m jarvis ops [status|check]`
- MCP (for another agent): `.venv\Scripts\python.exe -m jarvis mcp`
- Typecheck/lint: none configured.

## Project rules (deltas from the shared rules)

- Every effect goes through `TaskController.execute_tool` — never call a tool's `run()` from
  elsewhere to act on the world. This includes the runtime and goals: a scheduled job, an event
  reaction or a goal step must use the controller so policy, approval and the verifier apply.
- The MCP server is read-only by default on purpose: it has no approval channel, so a mutating
  call is refused rather than bypassed or left hanging.
- Scheduled jobs and event rules are **owner-defined at the console**. Do not add an HTTP endpoint
  that creates or edits a rule: a remote client must not be able to define new actions.
- New capability = a tool (`jarvis/tools/builtin/<area>_tool.py`) or a plugin
  (`data/plugins/<id>/`). Follow [docs/EXTENDING.md](docs/EXTENDING.md);
  `tests/test_tool_contract.py` must pass for it.
- Declare the true `SideEffect`; state-changing tools take `dry_run` and implement a real
  `verify()` read-back, or return `None` (recorded as unverified — never fake it).
- Every process a tool starts goes through the sandbox: `ctx.service("sandbox").run_async(argv)`
  or `_win.run_cmd`. Never call `subprocess` directly; the sandbox applies resource limits,
  kills the whole process tree, and keeps secrets out of the child's environment
  ([docs/SANDBOX.md](docs/SANDBOX.md)). The single exception is `apps.launch`, which starts an
  allowlisted app **detached on purpose** so it outlives the tool call.
- Tests run with no model, no voice stack and no network: skip, never fake.
- Optional dependencies are imported lazily; the Core must start without them.
- Secrets live in `data/secrets.json` / `JARVIS_SECRET_<NAME>` and reach tools only via
  `ctx.secrets(name)`; never in config, logs, prompts or commits.
- Free / open-source / local only unless the owner says otherwise.

## Do not

- Bind anything but loopback without `server.allow_lan` and the auth gate.
- Add a model or service that sends owner data off the machine by default.
- Weaken the sandbox, the audit chain, a policy, an approval or a verifier check to make a test pass.
- Imitate a real person's voice or likeness.
- Weaken a policy, approval or verifier check to make a test pass.
