# EVOLVE — Code Sandbox (remote runner)

**Status:** implemented and tested · **Owner:** Topaz

JARVIS and EVOLVE can test code on a **dedicated Linux runner** (the laptop,
through the operator's SSH config) with kernel-enforced limits. This is the
execution environment behind `sandbox.run_code` and the foundation for safely
testing approved programs.

---

## What it is

`jarvis.sandbox.remote.RemoteSandbox` sends a self-contained program to the
runner, executes it inside a fresh `mktemp -d` directory, and removes the
directory in a trap. Limits are enforced by the kernel on the runner:

| Control | Mechanism | Default |
|---|---|---|
| Wall clock | `timeout -s KILL` + client-side SSH timeout | 10 s |
| Memory | `RLIMIT_AS` (`ulimit -v`) | 256 MiB |
| CPU time | `RLIMIT_CPU` (`ulimit -t`) | 5 s |
| File size | `RLIMIT_FSIZE` (`ulimit -f`) | 16 MiB |
| Processes | `RLIMIT_NPROC` (`ulimit -u`, best effort) | 512 |
| Environment | SSH session environment (no Core secrets) | — |

The exit code is echoed by the script (`__JARVIS_SANDBOX_RC__=`) so a
SIGKILL from the wall timeout is visible instead of being masked by SSH.

## What it is **not**

- **Not network-isolated.** Unprivileged network namespaces are unavailable
  on the runner (`unshare -n` → "Operation not permitted"). Do not run
  hostile code here.
- **Not a security boundary against deliberate escape.** It is machine
  isolation plus resource containment for *approved* programs and tests.
- EVOLVE therefore keeps **untrusted/generated code disabled**: the code
  adapter only compiles approved embedded sources with a compiler whitelist
  and a correctness gate.

## Tools

| Tool | Side effect | Notes |
|---|---|---|
| `sandbox.run_code` | `LOCAL_EXEC` (approval required) | program, interpreter (`python3`/`bash`/`sh`/`node`), `timeout_s`, `memory_mb`, `cpu_seconds`, `stdin`, `host` |
| `sandbox.remote_status` | `NONE` | reachability, host, limits, interpreter whitelist, `network_isolated: false` |
| `sandbox.status` | `NONE` | local containment tiers (existing) |

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `config.sandbox.remote_host` | `laptop` | SSH host alias (credentials live in `~/.ssh/config`) |
| `config.sandbox.remote_connect_timeout_s` | 8.0 | SSH connect timeout |

Per-call limits are parameters of `sandbox.run_code`; the module defaults are
`RemoteLimits(wall_seconds=10, memory_mb=256, cpu_seconds=5, processes=512,
file_mb=16)`.

## Examples

```text
sandbox.run_code(program="print(6*7)")                      -> stdout "42"
sandbox.run_code(program="import time; time.sleep(30)",
                 timeout_s=2)                                -> timed_out true
sandbox.run_code(program="x = bytearray(400*1024*1024)",
                 memory_mb=128)                              -> MemoryError
sandbox.remote_status()                                      -> available true
```

## Tests

`tests/test_remote_sandbox.py` — 10 tests: script construction and quoting,
limit validation, interpreter whitelist, real execution, wall-timeout kill,
memory cap, stdin/args, and truthful status reporting. The integration tests
skip cleanly when the runner is unreachable.
