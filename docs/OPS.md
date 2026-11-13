# JARVIS Self-Diagnostics

**Status:** Active guide (Observed: 842-test suite, ops tests green, 2026-10-20) | **Owner:** Topaz | **Last updated:** 2026-10-20

JARVIS checks its own dependencies on an interval and reports them honestly. It
performs exactly one automatic repair, and only one that is safe and bounded.

## Checks

| Check | Fails when | Repair hint |
|---|---|---|
| `llm` | Ollama is unreachable or has no model | start Ollama / pull the model |
| `runtime` | the tick loop is not running or this Core is standby | restart the Core / stop the other Core |
| `goals` | the goals service is not loaded | restart the Core |
| `audit` | the hash chain does not verify | `jarvis audit` |
| `sandbox` | no containment tier, while enabled | `jarvis sandbox` |
| `database` | `PRAGMA integrity_check` is not `ok` | restore a checkpoint |
| `disk` | the data volume has less than 10% free | free space |
| `node:<name>` | an enrolled node does not answer | restart its tunnel / check the host |

## The one automatic repair

If an enrolled node whose `base_url` is the local tunnel
(`http://127.0.0.1:2226`) is unreachable, OpsService runs
`scripts/node-tunnel.ps1 -Start` through the sandbox, at most once per
`ops.repair_cooldown_s` (default 120 s). The result is audited
(`ops.repair_tunnel`) and the node is re-observed before the check is reported.

It does **not** start Ollama, change policy, install anything, or restart the
Core. Those are reported as `repair` strings for the owner.

## Configuration

```toml
[ops]
enabled = true
interval_s = 60
auto_repair_tunnel = true
repair_cooldown_s = 120
```

## Using it

```powershell
python -m jarvis ops            # last snapshot
python -m jarvis ops check      # run the checks now
```

HTTP: `GET /api/ops`, `POST /api/ops/check`. Tool: `system.diagnose` (read-only,
returns the last snapshot). The HUD's Diagnostics panel includes it.

## Honest limits

- The checks are **observations with a timeout**, not a guarantee of health.
- The tunnel repair only applies to a **local** tunnel endpoint; a direct LAN node
  is reported, never silently re-addressed.
- Starting the model server is deliberately left to the owner: a GUI app started
  by a background service is surprising and hard to supervise.

## APOS tunnel supervision (2026-11-12)

The production control plane runs both node tunnels from
`jarvis-node-tunnels.service` (`/home/topaz/bin/jarvis-node-tunnels.sh`):

| Local (APOS) | Via | Destination |
|---|---|---|
| `127.0.0.1:2228` | `Topaz@10.0.0.200:2223` (PC) | PC `127.0.0.1:2228` |
| `127.0.0.1:2230` | `topaz@10.0.0.234:2225` (laptop) | laptop `127.0.0.1:2226` |
| `127.0.0.1:8799` | same PC leg (forward) | PC `127.0.0.1:8799` (live screen reader) |
| `127.0.0.1:8770` | same PC leg (forward) | APOS `127.0.0.1:8770` (EVOLVE Arena; 8766 is AMSD) |
| PC `127.0.0.1:11435` | same PC leg (**reverse**) | APOS `127.0.0.1:11434` (Ollama for captions) |

The last two legs connect the live screen reader (`docs/SCREEN-READER.md`):
APOS reads `/latest` from the PC, and the PC calls llava through the reverse
Ollama endpoint without exposing either port on the LAN.

**Fixed defect:** the script used `wait -n`, so when the PC leg failed (during
the PC-001 crash recovery the journal showed repeated `No route to host`),
the whole service exited, systemd restarted it, and the healthy laptop tunnel
was torn down with it. Each leg is now supervised independently and retried
every 5 s; one node's failure can no longer break the other. The previous
script is kept as `jarvis-node-tunnels.sh.bak-20261112`.

**Probe the node as HTTP, never as SSH.** The satellite endpoint is a
JSON-over-HTTP server (uvicorn), so an SSH probe of `127.0.0.1:2228` or
`127.0.0.1:2230` hangs at "banner exchange" or is reset â€” that is expected and
does not indicate a broken tunnel. The correct probe is:

```bash
curl -s -i -m 8 http://127.0.0.1:2230/node/health | head -1
# HTTP/1.1 401 Unauthorized   -> tunnel + node are alive (token required)
```

**Laptop node unit (2026-11-12):** `jarvis-node.service` is a **user** unit
(`~/.config/systemd/user/jarvis-node.service`), enabled, with
`Linger=yes`; it runs uvicorn on `127.0.0.1:2226`. A five-day-old node
process was still bound to 2226 and was stopped manually; because the unit's
`Restart=on-failure` does not restart after a clean signal, the unit stayed
enabled but inactive and the tunnel had nothing to forward to. Restored with
`systemctl --user start jarvis-node`; the Core then reported `ok:true` with
laptop **53.84 ms** and PC **43.88 ms**.

Verification: both local ports listen; `POST /api/ops/check` returns
`ok:true` with both nodes online. The PC leg can take up to ~30 s to
establish while its sshd settles after boot.

## PC-side tunnel supervisor (`scripts/jarvis-tunnels.py`)

The PC runs its own supervisor (Task Scheduler task **JARVIS Agent**). It has
**no default endpoints**: without the environment it logs
`starting ... via <APOS_HOST>:2223` and retries forever, so the PC loses
`127.0.0.1:8765` (Core), `11435` (APOS Ollama) and `2227` (APOS node) even
though APOS is healthy.

Required user environment variables:

| Variable | Value used here |
|---|---|
| `JARVIS_APOS_HOST` | `10.0.0.63` |
| `JARVIS_APOS_PORT` | `2223` |
| `JARVIS_LAPTOP_HOST` | `10.0.0.234` |
| `JARVIS_LAPTOP_PORT` | `2225` |
| `JARVIS_SSH_USER` | `topaz` (the Linux user; Windows `USER=Topaz` is denied) |

Observed 2026-11-12: with the placeholders and `Topaz@`, every leg exited
`rc=255`; `tunnels.log` reached 25 MB. After setting the variables and
restarting the task, `8765`, `11435` and `2227` listen and
`GET /api/health` from the PC returns `core=ready`. The supervisor now rotates
the log at 5 MB and backs off to 60 s on repeated failures.

The laptop leg remains unreachable directly from the PC (port 2225 times out;
APOS reaches it). That leg is redundant â€” the Core reaches the laptop through
APOS `127.0.0.1:2230` â€” so the backoff keeps it quiet.

## Related

[RUNTIME.md](RUNTIME.md) Â· [GOALS.md](GOALS.md) Â· [SANDBOX.md](SANDBOX.md) Â·
[BUILD-NOTE.md](BUILD-NOTE.md)