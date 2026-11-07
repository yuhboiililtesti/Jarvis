# Using JARVIS from another agent (MCP)

**Status:** Active guide (Observed: real stdio round-trip, 2026-10-20) | **Owner:** Topaz | **Last updated:** 2026-10-20

JARVIS exposes its tool registry over the **Model Context Protocol** (MCP), so
Claude Code, OpenCode or any MCP client can use JARVIS as a tool provider — the
same policy-gated, verified actions the HUD uses.

## Run it

```powershell
.\.venv\Scripts\python.exe -m jarvis mcp
```

It speaks JSON-RPC 2.0 over stdin/stdout (newline-delimited). Logs go to stderr,
so the protocol stream stays clean.

## Read-only by default

MCP has **no approval UI**, so the server exposes only tools that policy would
run without asking: side-effect `none`, plus whatever is in
`policy.auto_allow`. A mutating call is refused immediately with an actionable
message instead of hanging for five minutes:

```json
{"isError": true, "content": [{"type": "text",
  "text": "'files.write' needs the owner's approval; MCP has no approval channel. Use the JARVIS HUD or the console."}]}
```

`--allow-mutations` lists mutating tools too. In the default **ask** mode they
still refuse: there is nobody to approve them in this transport. If the Core is
in **autonomous** mode, non-destructive mutations will run (that is what that
mode means); the server logs a warning at startup. Do not combine the flag with
autonomous mode unless you accept unattended changes.

## Connect a client

**Claude Code:**

```powershell
claude mcp add jarvis -- C:\Users\<USER>\Projects\AI\Jarvis\.venv\Scripts\python.exe -m jarvis mcp
```

**OpenCode** (`opencode.json`):

```json
{
  "mcp": {
    "jarvis": {
      "type": "local",
      "command": ["C:\\Users\\<USER>\\Projects\\AI\\Jarvis\\.venv\\Scripts\\python.exe", "-m", "jarvis", "mcp"],
      "enabled": true
    }
  }
}
```

**Any client:** spawn the command, send `initialize`, `notifications/initialized`,
then `tools/list` / `tools/call`.

## What is exposed

The read-only surface, currently 41 tools (system info, metrics, time, files read,
memory search, knowledge search, nodes status, web search/fetch, sandbox status,
`system.diagnose`, and so on). `tools/list` is authoritative; it is generated from
the live registry, not a static list.

## Why writes are refused

Every JARVIS effect is meant to pass `TaskController.execute_tool` and, when
needed, the owner's approval. An MCP client is another process; it cannot answer
that prompt. Refusing is the honest option: the alternative is either an
unattended bypass or a request that appears to hang. Use the HUD or the console
for mutations.

## Honest limits

- No MCP resources or prompts yet — tools only.
- No streaming; each call returns one result.
- A mutating tool is invisible by default. That is a safety choice, not a bug.

## Related

[EXTENDING.md](EXTENDING.md) · [ARCHITECTURE.md](../ARCHITECTURE.md) ·
[RUNTIME.md](RUNTIME.md) · [AGENTS.md](../AGENTS.md)
