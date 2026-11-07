#!/usr/bin/env bash
# Install JARVIS on a Linux/Ubuntu Core host.
#
# Creates a virtual environment, installs the Core requirements, optionally the
# voice stack, optionally pulls the local model, writes config and runs a
# diagnosis. Idempotent: safe to re-run. Changes nothing outside the project
# directory and the Ollama model store.
#
# Usage:
#   bash scripts/install.sh
#   bash scripts/install.sh --voice --model
#   bash scripts/install.sh --force --service
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VOICE=0
MODEL=0
FORCE=0
SERVICE=0
MODEL_NAME="${JARVIS_MODEL:-qwen2.5:3b-instruct}"

while [ $# -gt 0 ]; do
  case "$1" in
    --voice) VOICE=1 ;;
    --model) MODEL=1 ;;
    --model-name) MODEL_NAME="$2"; shift ;;
    --force) FORCE=1 ;;
    --service) SERVICE=1 ;;
    -h|--help)
      echo "usage: $0 [--voice] [--model] [--model-name NAME] [--force] [--service]"
      exit 0 ;;
    *) echo "unknown option: $1"; exit 1 ;;
  esac
  shift
done

step() { printf '\n=== %s ===\n' "$1"; }

step "Checking Python"
command -v python3 >/dev/null || { echo "python3 3.11+ required"; exit 1; }
python3 - <<'PY'
import sys
print("python", sys.version.split()[0])
if sys.version_info < (3, 11):
    sys.exit("Python 3.11+ required")
PY

step "Virtual environment"
if [ "$FORCE" = 1 ] && [ -d .venv ]; then rm -rf .venv; fi
if [ ! -d .venv ]; then
  # Minimal Ubuntu installs may lack python3-venv (ensurepip). Fall back to a
  # --without-pip venv plus a get-pip bootstrap, which needs no root.
  if python3 -m venv .venv >/dev/null 2>&1 && [ -x .venv/bin/python ]; then
    if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
      echo "ensurepip unavailable; bootstrapping pip..."
      curl -sS https://bootstrap.pypa.io/get-pip.py -o "${TMPDIR:-/tmp}/get-pip.py"
      .venv/bin/python "${TMPDIR:-/tmp}/get-pip.py"
    fi
  else
    echo "venv creation failed; retrying without pip and bootstrapping..."
    rm -rf .venv
    python3 -m venv --without-pip .venv
    curl -sS https://bootstrap.pypa.io/get-pip.py -o "${TMPDIR:-/tmp}/get-pip.py"
    .venv/bin/python "${TMPDIR:-/tmp}/get-pip.py"
  fi
  echo "created .venv"
else
  echo ".venv already exists"
fi
VENV_PY="$ROOT/.venv/bin/python"

step "Installing Core requirements"
"$VENV_PY" -m pip install --upgrade pip wheel >/dev/null
"$VENV_PY" -m pip install -r requirements.txt

if [ "$VOICE" = 1 ]; then
  step "Installing voice + compute stack (large download)"
  echo "Prerequisite system packages (Ubuntu/Debian):"
  echo "  sudo apt install -y libportaudio2 libsndfile1 ffmpeg espeak-ng"
  "$VENV_PY" -m pip install -r requirements-voice.txt
else
  printf '\n(skipping voice stack; re-run with --voice to install it)\n'
fi

step "Configuration"
if [ ! -f config/jarvis.toml ]; then
  cp config/jarvis.example.toml config/jarvis.toml
  echo "wrote config/jarvis.toml"
else
  echo "config/jarvis.toml already exists (left untouched)"
fi
"$VENV_PY" -m jarvis init >/dev/null || true

if [ "$MODEL" = 1 ]; then
  step "Local model"
  if ! command -v ollama >/dev/null; then
    echo "ollama not found; install it from https://ollama.com then run: ollama pull $MODEL_NAME"
  else
    echo "pulling $MODEL_NAME (several GB; resumable)..."
    ollama pull "$MODEL_NAME"
  fi
fi

step "Diagnosis"
"$VENV_PY" -m jarvis doctor || true

if [ "$SERVICE" = 1 ]; then
  step "systemd user service"
  bash scripts/install-systemd.sh
fi

printf '\nDone.\n'
printf 'Start the Core with:  ./scripts/run.sh      then open http://127.0.0.1:8765/\n'
printf 'Or as a service:      ./scripts/install-systemd.sh   (systemctl --user start jarvis-core)\n'
