"""Write config/jarvis.toml from the example, injecting the shared node token.

Kept as a script (not an inline command) because shell quoting mangles the
inline form, and because a BOM once broke tomllib parsing.
"""
from __future__ import annotations

import pathlib
import re

root = pathlib.Path(__file__).resolve().parents[1]
token_file = root / "data" / "node-token"
token = token_file.read_text(encoding="ascii").strip() if token_file.is_file() else ""

text = (root / "config" / "jarvis.example.toml").read_text(encoding="utf-8")
if re.search(r"(?m)^token\s*=", text):
    text = re.sub(r"(?m)^token\s*=.*$", f'token = "{token}"', text)
else:
    text = re.sub(r"(?m)^(\[nodes\]\s*)$", lambda m: f'{m.group(1)}\ntoken = "{token}"',
                  text, count=1)
if token:
    text = re.sub(r'(?m)^host_role\s*=.*$', 'host_role = "core"', text)

target = root / "config" / "jarvis.toml"
target.write_text(text, encoding="utf-8", newline="\n")
raw = target.read_bytes()
print(f"wrote {target} ({len(raw)} bytes, starts {raw[:3]!r})")
# Never print the token line: stdout ends up in terminal scrollback and logs.
print("node token:", "written (value not printed)" if token else "empty")
