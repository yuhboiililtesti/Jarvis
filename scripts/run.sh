#!/usr/bin/env bash
# Run the JARVIS Core in the foreground (Linux/macOS).
#
# Usage:  ./scripts/run.sh          # serve the API + HUD on 127.0.0.1:8765
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -x ".venv/bin/python" ]; then
  echo "no .venv found — run: bash scripts/install.sh"
  exit 1
fi

exec .venv/bin/python -m jarvis serve
