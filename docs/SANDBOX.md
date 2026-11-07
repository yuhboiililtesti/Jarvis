# JARVIS Sandbox

**Status:** Active guide (Observed: Job Object tier contains real runs, 733-test suite passes, 2026-10-18) | **Owner:** Topaz | **Last updated:** 2026-10-18

How JARVIS contains the processes it starts — and, just as importantly, what it
does **not** contain. Read this before trusting `shell.run` or a plugin with
anything.

## Why it exists

JARVIS reaches the operating system through two doors:

- `shell.run` — the generic escape hatch that can execute any command;
- `jarvis.tools.builtin._win.run_cmd` — the helper the desktop tools use to call
  `powershell`, `ffmpeg`, `notify-send` and friends.

Both used to inherit the Core's entire environment and start a child that
outlived the Core if it misbehaved. That is how stray `python.exe` processes
appear (parent gone, no command line visible), and it is how a command as
ordinary as `Get-ChildItem env:` could print a `JARVIS_SECRET_*` value.

Every child process now goes through `jarvis.sandbox.SandboxService`:
`shell.run`, `run_cmd`, the media worker (`media.probe`/`media.convert`) and the
clipboard tools all do. The **one deliberate exception** is `apps.launch`, which
starts an owner-allowlisted application **detached on purpose** so it outlives
the tool call — putting it in a job would kill the app the moment the tool
returned. It is bounded by the allowlist instead, and it is still handed a
scrubbed environment (except via `os.startfile`, which takes no environment — a
documented gap). There is no other path to a process.

## What is enforced (Observed on WIN11, 2026-10-18)

| Guarantee | How | Verified by |
|---|---|---|
| The whole process tree dies | Windows Job Object with `KILL_ON_JOB_CLOSE`; `TerminateJobObject` on timeout | `tests/test_sandbox.py::test_timeout_reaps_a_grandchild` |
| Wall-clock timeout | Killed by the sandbox, reported as `timed_out` | `...::test_timeout_kills_the_tree` |
| Memory ceiling | `JOB_OBJECT_LIMIT_PROCESS_MEMORY` / `RLIMIT_AS` | `...::test_memory_cap_stops_a_large_allocation` |
| Process-count ceiling | `JOB_OBJECT_LIMIT_ACTIVE_PROCESS` (Windows tier) | `jarvis sandbox` tier capabilities |
| CPU-time ceiling | `JOB_OBJECT_LIMIT_PROCESS_TIME` / `RLIMIT_CPU` (off by default) | configured limit |
| No secret in the child environment | `sanitized_env` strips `JARVIS_SECRET_*` and any value matching a stored secret | `...::test_child_does_not_inherit_secret_environment`, `...::test_shell_run_child_cannot_read_a_secret` |

Limits come from `[sandbox]` in `config/jarvis.toml` (see
`config/jarvis.example.toml`). A value of `0` disables that dimension.
`kill_tree = false` is honoured by both tiers (the job stops setting
kill-on-close; POSIX stops creating a new session) — it trades orphan-safety for
letting a command's children survive it.

## Reported per run, not just in the config

`SandboxResult` states what actually happened *for that run*. `contained` is true
only when the OS accepted the limits **and** the process was assigned; if either
fails, `contained` is false and `notes` says why. The sandbox never reports
containment it did not get. Resource caps can be disabled per call — the media
worker (`media.convert`/`media.probe`) does exactly that: a legitimate transcode
may need more RAM than a shell command, so it keeps tree-kill and environment
scrubbing but drops the caps.

A runner that fails *after* the child has started returns an error result and
does **not** fall back to an uncontained run, because that would execute the
command twice.

## What is NOT enforced (stated, not implied)

- **Filesystem isolation.** The child runs as the owner's account and can read
  and write anything the owner can. The sandbox limits *resources*, it does not
  confine *reach*.
- **Network isolation.** A Job Object or an rlimit cannot restrict sockets. There
  is no in-process network off-switch.
- **Signature or trust for plugins.** A plugin still runs in-process with the
  Core's rights. The pin and ceiling (see [EXTENDING.md](EXTENDING.md)) stop
  silent change and over-reach; they are not a sandbox against hostile code.

Filesystem and network isolation are the blueprint's answer for unknown code
(`Blueprints/AI-Blueprint/17_SECURITY_PRIVACY_RECOVERY.md` §Sandboxing) and they
require a **VM or container** tier. Neither is integrated, and JARVIS says so
rather than pretending:

```powershell
python -m jarvis sandbox          # the tier matrix, with a reason for each
python -m jarvis sandbox --check  # run a contained probe and report the result
```

Observed on this machine (2026-10-18): `jobobject` available; `rlimit` N/A
(Windows); `windows_sandbox` not installed/enabled — enabling the optional
feature `Containers-DisposableClientVM` would give a disposable VM with real
filesystem and network isolation; `container` N/A (no Docker/Podman).

## Tiers

| Tier | Platform | Provides | Notes |
|---|---|---|---|
| `jobobject` | Windows | kill-tree, memory, process-count, CPU-time | Assigned right after the process starts (see below). |
| `rlimit` | Linux/macOS | kill-tree, memory, CPU-time, file-size | New session; process-group `SIGKILL` on timeout. No process-count cap (`RLIMIT_NPROC` is per-user and would stop the child from starting). A `setrlimit` the kernel refuses is not reportable from inside the child — a known limit. |
| `none` | any | nothing (environment still scrubbed) | Used only when a tier is unavailable or `enabled = false`. |

`tier = "auto"` picks the best available. Setting `tier = "jobobject"` (or
`rlimit`) pins it; if the pinned tier is unavailable JARVIS falls back to the
best tier and reports the fallback in `status()`.

## The one honest caveat in the Windows tier

The ideal sequence is *create suspended → assign to the job → resume*, which
leaves no window for a child to escape. CPython's `subprocess` closes the
primary-thread handle, so `Popen` cannot resume a suspended process. The
assignment therefore happens microseconds after `CreateProcess` returns; a shell
interpreter has not finished initialising in that window, so nothing escapes it
in practice — but it is a window, and it is written here rather than claimed
away.

The clean fix is to create the process ourselves with a `STARTUPINFOEX` carrying
`PROC_THREAD_ATTRIBUTE_JOB_LIST`, which assigns it to the job *atomically at
creation*. That is future work, not done.

## Using it from code

```python
from jarvis.sandbox import SandboxService

svc = SandboxService(config=cfg)          # or the kernel's service: ctx.service("sandbox")
svc.status()                              # tiers, limits, isolation matrix
result = await svc.run_async(argv, cwd=cwd, timeout=30)
result.contained, result.timed_out, result.limit_terminated   # what actually happened
```

`SandboxService.run`/`run_async` never raise: a tier that cannot be set up falls
back to an uncontained run and says so (`contained=False`, plus a note).

## Verifying it yourself

```powershell
python -m jarvis sandbox --check                 # reports the tier a real probe used
python -m jarvis tools --health                  # every tool, including sandbox.status
.\.venv\Scripts\python.exe -m pytest tests/test_sandbox.py -q
```

## Related

[AGENTS.md](../AGENTS.md) · [ARCHITECTURE.md](../ARCHITECTURE.md) ·
[EXTENDING.md](EXTENDING.md) · [CAPABILITY-MAP.md](CAPABILITY-MAP.md) ·
[Sandbox policy](../../../../Documentation/Sandbox/Read-Me.md) ·
[Blueprint §17 Security](../../../../Documentation/Blueprints/AI-Blueprint/17_SECURITY_PRIVACY_RECOVERY.md)
