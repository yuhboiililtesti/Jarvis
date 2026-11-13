
# Stress testing — absolute-limit harness (scripts/pc001_stress.py)

Pushes RAM, CPU and GPU to their limits, time-bounded and with a memory
reserve. Use it to validate hardware stability after a crash investigation.

## Modes

`powershell
# CPU: one busy worker per logical core
python scripts\pc001_stress.py cpu --seconds 300

# RAM: allocate and TOUCH every page (real physical commit)
python scripts\pc001_stress.py ram --seconds 300 --ram-percent 85
# absolute limit (allocates until MemoryError, keeps 256 MB free)
python scripts\pc001_stress.py ram --seconds 300 --ram-percent 100 --reserve-mb 256

# GPU: WebGL heavy shader in Edge kiosk (no CUDA required)
python scripts\pc001_stress.py gpu --seconds 300

# Everything together (GPU + RAM thread + all CPU cores)
python scripts\pc001_stress.py all --seconds 300 --ram-percent 85
`

## Behaviour

- RAM is allocated in 64 MB chunks and every 4 KB page is written, so the
  pages are physically committed (not just reserved). It stops on
  `MemoryError` and reports the achieved total.
- A hang or BSOD **is** the test result — the machine cannot report afterwards
  by definition. Keep a restore point and a backup before absolute runs.
- `--reserve-mb` (default 512) leaves headroom for the OS to survive a
  failed run; `--ram-percent 100` still honours the reserve.
- The GPU mode opens Edge/Chrome kiosk with a 900-iteration raymarch shader
  and terminates the browser at the end.

## Validated 2026-11-12 (PC-001, 2×8 GB @ 3200, RTX 3080)

| Mode | Run | Result |
|---|---|---|
| cpu | 4 workers, 4 s | finished cleanly |
| ram | 10 %, 3 s (768 MB) | allocated, held, freed |
| gpu | 4 s | WebGL shader ran, browser closed |

Full-power GPU + full test (314 s at ~300 W) passed after the memory change
from 3600 → 3200 MT/s; see
`Documentation/Issues/Systems/20261112-PC001-Hard-Hang.md`.
