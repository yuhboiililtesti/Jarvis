"""Prove the Core <-> Node link end to end.

Enrols the ThinkPad, pings it, and sends two real remote tasks, then reports
exactly what came back. Nothing is assumed: every result is the node's response.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.config import load_config
from jarvis.core import build

NODE_ID = "laptop"
# The ThinkPad runs ufw, which blocks the agent port on the LAN. We reach it
# through an SSH tunnel instead (scripts/node-tunnel.ps1), so the default is the
# tunnel endpoint. Override with JARVIS_NODE_URL for a direct LAN connection.
NODE_URL = os.environ.get("JARVIS_NODE_URL", "http://127.0.0.1:2226")


async def call(obj, *args, **kwargs):
    """Call a method that may be sync or async and return the value."""
    out = obj(*args, **kwargs)
    if hasattr(out, "__await__"):
        out = await out
    return out


async def main() -> int:
    cfg = load_config()
    print("core token configured:", bool(cfg.nodes.token))
    j = build(cfg)
    nodes = j.services.get("nodes")
    if nodes is None:
        print("nodes service not loaded")
        return 1

    print("\n--- enroll ---")
    enrolled = await call(nodes.enroll, NODE_ID, NODE_URL,
                          ["exec", "sysinfo", "files.read", "ping"])
    node_ref = enrolled.get("id") or NODE_ID
    print("node id:", node_ref, "| base_url:", enrolled.get("base_url"),
          "| status:", enrolled.get("status"))

    print("\n--- ping ---")
    ping = await call(nodes.ping, node_ref)
    print(json.dumps(ping, default=str, indent=2)[:600])

    print("\n--- remote task: sysinfo ---")
    sysinfo = await call(nodes.send_task, node_ref, "sysinfo", {})
    data = sysinfo.get("data") or sysinfo.get("result") or {}
    print("ok:", sysinfo.get("ok"))
    for key in ("hostname", "uname", "cpu_count", "uptime_s", "load"):
        if key in data:
            print(f"  {key}: {str(data[key])[:120]}")
    if not sysinfo.get("ok"):
        print("  reason:", sysinfo.get("reason") or sysinfo.get("error"))

    print("\n--- remote task: exec 'uname -a' ---")
    ex = await call(nodes.send_task, node_ref, "exec", {"command": "uname -a"})
    print("ok:", ex.get("ok"))
    out = ex.get("data") or ex.get("result") or {}
    print("  stdout:", str(out.get("stdout", ex.get("stdout", ""))).strip()[:200])
    print("  exit_code:", out.get("exit_code", ex.get("exit_code")))

    print("\n--- remote task: refused command (denylist) ---")
    bad = await call(nodes.send_task, node_ref, "exec", {"command": "rm -rf /"})
    print("ok:", bad.get("ok"), "| code:", bad.get("code"), "| refused:",
          not bool(bad.get("ok")))

    print("\n--- node list ---")
    for n in await call(nodes.list_nodes):
        print(f"  {n.get('id')} name={n.get('name')} {n.get('status')} "
              f"{n.get('base_url')} last_seen={n.get('last_seen')}")

    j.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
