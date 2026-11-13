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

**Fixed defect:** the script used `wait -n`, so when the PC leg failed (during
the PC-001 crash recovery the journal showed repeated `No route to host`),
the whole service exited, systemd restarted it, and the healthy laptop tunnel
was torn down with it. Each leg is now supervised independently and retried
every 5 s; one node's failure can no longer break the other. The previous
script is kept as `jarvis-node-tunnels.sh.bak-20261112`.

Verification: both local ports listen after restart; the PC leg takes up to
~30 s to establish while its sshd settles after boot. The PC leg terminated a
general SSH probe (`Connection closed`) because it fronts the node endpoint,
not the general sshd; that is expected. A laptop tunnel that hangs at banner
exchange while `10.0.0.234:2225` answers should be retested when the laptop is
idle — a CPU-saturated laptop can starve the node endpoint.

## Related

[RUNTIME.md](RUNTIME.md) · [GOALS.md](GOALS.md) · [SANDBOX.md](SANDBOX.md) ·
[BUILD-NOTE.md](BUILD-NOTE.md)
