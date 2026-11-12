# Computer Control — code navigation, git and system monitoring

**Status:** implemented and tested · **Owner:** Topaz

JARVIS can already list/read/write files, launch apps, capture the screen,
OCR, type/click, control windows, manage processes, run shell commands (gated),
and inspect the network. This document covers the additions that close the
"search and read code, inspect repositories, monitor the machine" gap.

All tools below are **read-only** (`SideEffect.NONE`), auto-allowed for low
friction, and constrained to the same allow-listed file roots as the file
tools (`_safe_path`). Credential stores remain refused.

---

## Tools

### `code.read`
Read a text file with **line numbers**, optionally a line range.

| Param | Default | Meaning |
|---|---|---|
| `path` | required | file inside allowed roots |
| `start_line` | 1 | first line (1-based) |
| `end_line` | 0 | last line inclusive; 0 = to EOF |
| `encoding` | utf-8 | text encoding |

Returns `lines: [{n, text}]`, `total_lines`, `truncated`. Binary files are
rejected (use `files.hash`).

### `code.search`
Regex **content** search across a tree (`files.search` only matches
filenames).

| Param | Default | Meaning |
|---|---|---|
| `path` | required | directory or single file |
| `pattern` | required | regular expression |
| `include` | `*` | filename glob filter |
| `case_sensitive` | false | match case |
| `max_results` | 200 | cap (max 500) |
| `max_file_bytes` | 1048576 | skip larger files |

Skips `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `dist`, `build`,
caches, `target`, `.next`, `coverage`. Returns `{path, line, column, text}`
per match plus `files_scanned`.

### `code.symbols`
Definition index across a tree: classes/functions/structs/traits from
language-aware patterns (Python, JS/TS, Go, Rust, Java, shell, C/C++
heuristics).

| Param | Default |
|---|---|
| `path` | required |
| `include` | `*` |
| `max_results` | 300 |

Returns `symbols: [{name, kind, path, line}]`.

### `code.diff`
Unified diff between two files (`difflib`); `context=3`, `max_bytes=200000`.
Returns `changed` and the diff text (capped).

### `git.info`
Read-only git inspection.

| Param | Default |
|---|---|
| `repo` | required |
| `action` | `status` (`status`, `log`, `diff`, `show`, `branch`, `remote`) |
| `ref` | none (diff/show) |
| `path` | none (diff path filter) |
| `count` | 20 (log) |
| `max_bytes` | 200000 |

Runs `git -C <repo> <action>` with **argument-based** subprocess calls (no
shell), 60 s timeout. Returns stdout/stderr/returncode.

### `monitor.probe` (alias `system.snapshot`)
One-shot system health snapshot: CPU (percent + per-core), memory, swap,
disk usage for the home and cwd drives (or supplied `disks`), load average,
uptime, network counters, and the busiest processes by CPU and memory.
`health` is `ok` / `warning` (≥80 % mem or ≥85 % CPU) / `critical` (≥90 % mem
or ≥95 % CPU). `interval_s` samples CPU for up to 2 s; `top` caps the lists.

---

## Examples

```text
code.symbols(path="C:/Users/Topaz/Projects/Evolve")
code.search(path="C:/Users/Topaz/Projects/Evolve", pattern="def diagnose",
            include="*.py")
code.read(path=".../evolve/executive/diagnosis.py", start_line=1, end_line=80)
git.info(repo="C:/Users/Topaz/Projects/Evolve", action="log", count=10)
monitor.probe(top=6)
```

## Security boundary

- Paths resolve through `_safe_path`: allowed roots only (home, JARVIS data,
  configured extras), with credential/runtime-secret paths always refused.
- `git.info` only runs whitelisted read-only actions; no write commands.
- Nothing here executes user or model-supplied code.

## Health and dependencies

- `git.info` reports `unavailable: git is not on PATH` when git is missing.
- `monitor.probe` reports `unavailable: psutil is not installed` without
  psutil (the Core still starts).
