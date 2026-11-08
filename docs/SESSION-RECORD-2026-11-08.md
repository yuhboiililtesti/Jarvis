# Session Record — 2026-11-07 / 2026-11-08 — JARVIS expansion & EVOLVE

**Status:** Observed | **Owner:** Topaz | **Last updated:** 2026-11-08

A complete record of this working session: the analytics engine, outcome-based
learning, the multi-engine frontier, external-audit security hardening, GitHub
publication, Linux/Ubuntu support, and the **EVOLVE** evolutionary intelligence
engine — including its live test run with the JARVIS agent.

---

## 1. Analytics engine (compute / statistics / simulation)

**Added** `jarvis/tools/builtin/analytics_tool.py` — dependency-free:

| Tool | Purpose |
|---|---|
| `compute.stats` | count/sum/mean/median/mode/variance/stddev/quartiles/skew/kurtosis/percentiles |
| `compute.forecast` | linear trend / moving average / exponential smoothing |
| `compute.simulate` | Monte Carlo (uniform/normal, seeded, reproducible) |
| `compute.regress` | linear regression + correlation (slope, intercept, r, r², MAE, RMSE) |

**Evidence (live, APOS):** forecast `[10,20,30,40]` → `[50,60]`; normal simulation
mean=100 → `99.996`.

---

## 2. Learning subsystem (outcome-based self-adaptation)

**Added** `jarvis/learning/` (`LearningService`):

- Failed tool calls become durable, **deduplicated** `lesson` memories.
- Repeated failures **reinforce** confidence (0.5 → 0.7 → 0.85 → 0.95) instead of
  duplicating.
- Relevant lessons are recalled and injected into future turns (`LEARNED` block).
- Wired into the agent loop (recall before a turn, record after a turn).
- Tools: `learning.learn` / `learning.list` / `learning.forget`.

**Evidence (live, APOS):** repeated `apps.launch` failure recorded once, confidence
0.5 → 0.7; `relevant("dark mode")` returns the owner-taught lesson.

---

## 3. Multi-engine frontier (LLM providers + tiered router)

**Added** `jarvis/llm/frontier.py`:

| Provider | API | Notes |
|---|---|---|
| `GroqProvider` | OpenAI-compatible | thin subclass of `OpenAICompatProvider` |
| `AnthropicProvider` | native Messages API | `tool_use` ↔ `tool_calls`, tool-result grouping |
| `GeminiProvider` | native generateContent | `functionCall`/`functionResponse`, schema casing |

**Router** (`jarvis/llm/router.py`): tiered `route()` — `fast` / `primary` /
`frontier` — with a **privacy-preserving fallback chain** (a local failure never
silently reaches a remote endpoint; a frontier failure may fall back local).

**Config** (`LLMConfig`): `frontier_provider`, `frontier_model`,
`frontier_key_secret` (SecretStore / `JARVIS_SECRET_<NAME>`), `frontier_trigger`
(keyword escalation; empty = never escalate).

**Agent**: `_route_tier()` escalates a request to the frontier tier only when a
configured trigger matches.

**Evidence:** 13 mocked-HTTP tests; live tier verification on APOS (key → route,
no key → tier disabled).

---

## 4. Security hardening (external-audit fixes)

An external review of the public repo found four code-grounded issues; all fixed:

| Finding | Severity | Fix |
|---|---|---|
| Loopback requests get owner-level trust | **P0** | `/api/approvals`, `/api/defcon`, `/api/settings`, `/api/grants` now require a credential even on loopback; added `/api/auth/unlock` + HUD unlock prompt + a Settings "Security unlock" field |
| Authorization not rechecked after approval | **P1** | policy re-evaluated immediately before every effect (DEFCON/mode changes during the approval wait block the action) |
| Standing grants over-broad (`target=None`) | **P1** | remembered approvals scoped to their target (path/url/node/app) |
| Preflight runs before the policy gate | **P1** | policy evaluated before plugin preflight; denied tools never run plugin code |

**Also fixed this session:**
- V3 teardown `CancelledError` failures (10 → 0): `guardian.stop()` + `kernel.stop()`
  now catch `asyncio.CancelledError` (a `BaseException`, not `Exception`).
- Security watcher false positive: `missing_credentials` ("not yet unlocked") no
  longer triggers "Suspicious audit pattern: auth failures"; only
  `invalid_credentials` / lockouts do.

**Evidence:** `tests/test_security_hardening.py` (6 tests); live on APOS — protected
routes `401` without token, `200` with token; suite 1021 → 1027 passing.

---

## 5. GitHub publication (Prompt-0.0.17 discipline)

- Full repo audit (Prompt-0.0.17 + 0.0.21): no secrets/tokens/keys/real IPs/emails
  in the committed tree (verified with `git grep` on HEAD).
- **Sanitized:** hardcoded private IPs → env vars/placeholders; `C:\Users\Topaz\…`
  → `C:\Users\<USER>\…`; SSH user + key name → env-configurable; owner-specific
  scripts (`bridge-attempt.ps1`, `t8.py`) excluded via `.gitignore`.
- Added MIT `LICENSE`, `.env.example`, hardened `.gitignore`, rewritten public-safe
  README.
- Published to **https://github.com/yuhboiililtesti/Jarvis** (public). Old local
  history (pre-sanitization IPs) was never pushed — a fresh `main` branch carries
  a single clean commit.

---

## 6. Linux / Ubuntu support

| File | Purpose |
|---|---|
| `scripts/install.sh` | full Core installer (mirrors `install.ps1`, ensurepip fallback) |
| `scripts/run.sh` | foreground run helper |
| `scripts/install-systemd.sh` | user-level `jarvis-core` systemd service |
| `scripts/fetch-voices.sh` | Kokoro + Piper voice models with SHA-256 verification |

All bash-validated on Ubuntu (bash 5.2.21); executable bits set (`100755`).
README carries the Ubuntu quick-start. The core code was already cross-platform
(POSIX sandbox, Linux tool fallbacks).

---

## 7. EVOLVE — evolutionary intelligence engine

**New subsystem** `jarvis/evolve/` — self-contained (stdlib only):

| Module | Purpose |
|---|---|
| `engine.py` | core loop: population, selection (tournament/rank/roulette), elitism, crossover, mutation, diversity tracking, genealogy; bounded by generation / wall-clock / stagnation budgets |
| `genome.py` | `numeric_spec` (bounded float vectors) + `expression_spec` (symbolic expression trees) |
| `objectives.py` | symbolic regression (with **parsimony** pressure), minimize/maximize, sphere/rastrigin |
| `meta.py` | `self_tune()` — evolves the engine's **own** `RunConfig` (bounded self-improvement) |
| `service.py` | `EvolveService` — JARVIS integration (audited, persisted, bus-published) |
| `cli.py` | `python -m jarvis.evolve {symbolic,minimize,selftune,demo,gui}` |
| `gui.py` | tkinter GUI — three tabs (symbolic / minimize / self-evolve), threaded runs |

**JARVIS tools:** `evolve.symbolic`, `evolve.minimize`, `evolve.selftune`,
`evolve.status` — `SideEffect.NONE`, in `policy.auto_allow`.

**Live demos:**
- Symbolic regression recovered `x² + 2x + 1` (structurally `x² + 2x + 0.95`).
- Minimized Rastrigin(5D) to cost `0.0616` (global min 0).
- Self-tune found a config converging to cost `0.0`.

**Live JARVIS-agent test run (on APOS, isolated temp data dir, Night-Protocol
bounded):**

```
REQUEST: use the evolve.minimize tool to minimize the sphere function in 3 dimensions
[tool_call] evolve.minimize args={'app': 'sphere', 'dimensions': 3, 'generations': 100}
[tool_result] ok=True best_vector=[-1.5e-4, -8.3e-4, -9.3e-7]
FINAL: "…best cost was approximately 7.137 × 10⁻⁷… achieved after 74 generations."
turn wall time: 12.5s
```

The agent called the tool, the engine evolved a near-perfect solution, and the
agent reported it accurately.

---

## Verification summary

| Item | Value |
|---|---|
| Test suite | **1052 passed, 2 skipped, 0 failed** (up from 1021 at session start) |
| New tests | analytics (12), learning (11), frontier (17), security hardening (6), evolve (14) |
| Tools | 123 → **127** |
| Live checks | APOS core healthy; EVOLVE tools registered; agent test passed |

## Key commits (this session)

`850a894` initial public release · `69ebd24` Linux scripts · `71183a0` security
hardening · `80c1e6d` auth-failure alert fix · `8bd7373` HUD unlock field ·
`26ba1ce` EVOLVE engine · `fe2fee5` EVOLVE CLI+GUI · `b898135` evolve auto-allow.

## Rollback / unknowns

- **APOS SSH path changed** mid-session: `10.88.77.2:2223` (tunnel key) → **`10.0.0.200:2224`** (`id_ed25519`).
- Frontier providers verified via mocked HTTP only; live remote calls need API keys.
- Symbolic regression with tiny budgets can converge to a constant (parsimony vs
  exploration tradeoff — flagged for tuning).
- The full EVOLVE master plan (game/computer-use/hardware labs, world models,
  distributed workers) remains future work; this session built the engine
  foundation + meta-evolution the plan names as its first build.
