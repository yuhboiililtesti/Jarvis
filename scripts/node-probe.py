"""Probe the node endpoint and print the raw status + body (diagnostic)."""
from __future__ import annotations

import pathlib
import sys

import httpx

root = pathlib.Path(__file__).resolve().parents[1]
token = (root / "data" / "node-token").read_text(encoding="ascii").strip()

for path in ("/node/health", "/health", "/"):
    for label, headers in (("no-token", {}), ("token", {"X-Jarvis-Node-Token": token})):
        try:
            r = httpx.get(f"http://127.0.0.1:2226{path}", headers=headers, timeout=10.0)
            body = r.text[:300].replace("\n", " ")
            print(f"GET {path:15} [{label:8}] -> {r.status_code} {body}")
        except Exception as exc:
            print(f"GET {path:15} [{label:8}] -> ERROR {type(exc).__name__}: {exc}")
print("token length:", len(token))
