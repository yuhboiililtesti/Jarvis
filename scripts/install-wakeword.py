"""Install a custom wake-word model (e.g. a bare "jarvis" model).

The shipped openwakeword package only bundles ``hey_jarvis`` — there is no bare
"jarvis" model in it (measured: "jarvis" alone scores ~0.08 vs ~0.99 for "hey
jarvis"). Community "Jarvis" models exist on https://openwakeword.com/library
(free, requires a free account to download the ONNX). This script installs one
so ``voice.wake_model`` can point at it — no code change needed.

Usage:
  python scripts/install-wakeword.py --status
  python scripts/install-wakeword.py --model path/to/jarvis.onnx [--name jarvis]
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WAKE_DIR = ROOT / "data" / "voices" / "wake"


def _status() -> None:
    print(f"wake model directory: {WAKE_DIR}")
    if WAKE_DIR.is_dir():
        models = sorted(WAKE_DIR.glob("*.onnx"))
        if models:
            print("installed custom models:")
            for m in models:
                print(f"  - {m.name} ({m.stat().st_size} bytes)")
        else:
            print("  (no custom models installed)")
    else:
        print("  (directory does not exist yet)")
    print()
    print("To use a custom model, set in config/jarvis.toml:")
    print('  [voice]')
    print('  wake_model = "<absolute path or data/voices/wake/NAME.onnx>"')


def _install(model_path: str, name: str) -> int:
    src = Path(model_path).expanduser().resolve()
    if not src.is_file() or src.suffix.lower() != ".onnx":
        print(f"error: '{src}' is not an .onnx file")
        return 1
    WAKE_DIR.mkdir(parents=True, exist_ok=True)
    dst = WAKE_DIR / f"{name}.onnx"
    shutil.copy2(src, dst)
    print(f"installed {src.name} -> {dst}")
    print()
    print("Now set in config/jarvis.toml:")
    print(f'  wake_model = "{dst}"')
    print("and restart the voice listener.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Install a custom wake-word model")
    ap.add_argument("--status", action="store_true", help="show wake model status")
    ap.add_argument("--model", help="path to a downloaded .onnx model")
    ap.add_argument("--name", default="jarvis", help="model name (default: jarvis)")
    args = ap.parse_args()

    if args.status or not args.model:
        _status()
        return 0
    return _install(args.model, args.name)


if __name__ == "__main__":
    raise SystemExit(main())
