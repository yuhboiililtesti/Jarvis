"""Voice smoke test: report real capability state and synthesise one line.

Run:  .venv\\Scripts\\python.exe scripts\\voice_smoke.py
Nothing is faked: if a capability is unavailable the reason is printed.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.config import load_config
from jarvis.core import build


async def main() -> int:
    j = build(load_config())
    voice = j.services.get("voice")
    if voice is None:
        print("voice service not loaded")
        return 1

    status = await voice.status()
    print("--- capability state ---")
    for key in ("stt", "tts", "wake", "input", "output"):
        cap = status.get(key, {}) or {}
        print(f"{key:8} available={cap.get('available')}  {str(cap.get('reason'))[:110]}")
    print("engine:", status.get("engine"), "| dir:", status.get("tts", {}).get("detail", {}).get("voices_dir"))

    print("\n--- synthesis ---")
    result = await voice.say("Good evening, sir. All systems nominal. I am at your service.")
    keep = {k: result.get(k) for k in ("ok", "engine", "path", "duration_s", "reason", "speech")}
    print(json.dumps(keep, default=str))

    path = result.get("path")
    if path and Path(path).exists():
        print(f"wav exists: {path} ({Path(path).stat().st_size} bytes)")
    j.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
