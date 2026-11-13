# JARVIS — Desktop action lab

**Status:** Active (first verified workflow, 2026-11-13) | **Owner:** Topaz

The first **real** desktop action with an independent verifier, confined by
construction. It closes the smallest honest version of:

**observe → act → verify → record**

## Tool: `desktop.lab_write`

```
desktop.lab_write(filename="demo.txt", text="...")
```

1. Validates the filename (bare `.txt`, no paths) and confines it to
   `<data>/lab`.
2. Launches a small **lab editor** (tkinter) with a unique title.
3. Finds exactly that window by title and brings it to the foreground
   (`AttachThreadInput` workaround).
4. **Refuses to type** unless `GetForegroundWindow` proves focus — no blind
   keystrokes into other applications.
5. Types via `SendInput` (the existing input primitives) and presses Ctrl+S.
6. Verifies the file on disk **byte-for-byte** (including SHA-256), not the
   app's claim of success.
7. Closes the editor and appends the episode to
   `<data>/lab/outcomes.jsonl` (task, actions, hash, verified, duration).

## Measured demonstration (2026-11-13, PC-001)

```
ok: True
typed 60 chars, ctrl+s
verified: True, exact: True, 460.3 ms
sha256: 276ef693a56c9f7dc099ecac0d1d63f9031c12dba3d8da4f44b71e92ef752165
content: "JARVIS verified desktop write.\nLine two: exact bytes matter."
```

## Safety

- Only the lab editor's own window is addressed (unique title).
- Refuses to type when focus cannot be proven.
- Writes only inside `<data>/lab`; deletes only a same-named lab file.
- No clipboard use, no shell, no network.
- `SideEffect.LOCAL_EXEC`; the action is a separate tool call, not a hidden
  side effect of a read.
- The editor closes itself after 60 s even if the tool crashes.

## What this proves — and what it does not

**Proves:** real input synthesis, real window focus, a real file written by a
real application, and independent byte-level verification, with the evidence
recorded.

**Does not prove:** operating an *unfamiliar third-party* application, reading
a UI tree, choosing actions from perception, or recovering from unexpected
app states. Those remain the next milestones (the review's workflows 2–5).

## Next

1. Add the same verified pattern for a real third-party app using UI
   Automation (inspect controls, then act).
2. Replace the fixed script with perception: the agent chooses the filename,
   presses save, and verifies.
3. Feed `outcomes.jsonl` into EVOLVE's memory and learn action selection
   against held-out real tasks.

## Workflow results (2026-11-13)

| # | Workflow | State | Evidence |
|---|---|---|---|
| 1 | Lab editor: type + save + verify | **verified** | 460.3 ms, exact bytes, SHA-256 |
| 2 | Notepad | **blocked** | Windows 11 Notepad is a Store-app shim here and exposes no window (15 s wait, none) |
| 3 | File Explorer: move a file | **verified** | randomized folders/name/window position; real keyboard+mouse; moved, content SHA-256 matched; 9.2 s |
| 4 | Browser local form | not implemented | — |
| 5 | Calculator state | not implemented | — |
| 6 | Unexpected-dialog recovery | not implemented | — |

Workflow #3 is driven by `scripts/explorer_workflow.py`: it launches Explorer,
moves the window to a random position, proves focus (AttachThreadInput + ALT
unlock + SwitchToThisWindow), clicks the file list, Ctrl+A/Ctrl+X, navigates,
pastes, and verifies the filesystem independently. It does not trust Explorer's
UI feedback.