#!/usr/bin/env bash
# Install the JARVIS Node agent on the ThinkPad SL510 (Ubuntu 24.04).
#
# The SL510 is a satellite, not the brain: it relays voice/sensors and runs the
# thin node agent. It talks to the Core's node port (default 2226). SSH stays on
# its own port (2225). No root required.
#
# Usage:  bash scripts/install-laptop-node.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== JARVIS node install on $(hostname) ($(uname -m)) ==="
command -v python3 >/dev/null || { echo "python3 required"; exit 1; }
python3 - <<'PY'
import sys
print("python", sys.version.split()[0])
if sys.version_info < (3, 11):
    sys.exit("Python 3.11+ required")
PY

if [ ! -d .venv ]; then
  echo "creating venv..."
  # Many minimal Debian/Ubuntu installs lack python3-venv, so `python3 -m venv`
  # fails at the ensurepip step. Fall back to a --without-pip venv plus a
  # get-pip bootstrap, which needs no root.
  if python3 -m venv .venv >/dev/null 2>&1 && [ -x .venv/bin/python ]; then
    if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
      echo "ensurepip unavailable; bootstrapping pip into the venv..."
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
fi
# shellcheck disable=SC1091
. .venv/bin/activate

pip install --upgrade pip wheel >/dev/null
# Minimal node requirements only — this machine is a Core 2 Duo with ~3.7 GB RAM.
pip install fastapi uvicorn httpx psutil

mkdir -p data/logs data/artifacts

# Generate/keep a node token if the config does not have one.
TOKEN_FILE="$ROOT/data/node-token"
if [ ! -f "$TOKEN_FILE" ]; then
  python3 -c "import secrets;print(secrets.token_urlsafe(32))" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "generated node token at $TOKEN_FILE"
fi

BIND_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
NODE_PORT="2226"

echo
echo "=== systemd user unit ==="
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/jarvis-node.service" <<EOF
[Unit]
Description=JARVIS Node agent (satellite)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=JARVIS_HOST_ROLE=node
Environment=JARVIS_NODES__TOKEN=$(cat "$TOKEN_FILE")
ExecStart=$ROOT/.venv/bin/python -m jarvis.nodes
Restart=on-failure
RestartSec=5
# The SL510 is memory-constrained; keep the agent modest.
MemoryMax=512M

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload || true
systemctl --user enable --now jarvis-node.service || {
  echo "NOTE: could not enable the user service automatically."
  echo "      run: systemctl --user enable --now jarvis-node.service"
}
# allow the user service to survive logout
loginctl enable-linger "$USER" 2>/dev/null || true

echo
echo "=== done ==="
echo "health:  http://${BIND_IP:-<this-host>}:${NODE_PORT}/node/health"
echo "logs:    journalctl --user -u jarvis-node -f"
echo
echo "On the Core, enrol this node:"
echo "  .venv\\Scripts\\python.exe -m jarvis repl"
echo "  > note the node token in config, then use the Nodes panel to add"
echo "    http://${BIND_IP:-<NODE_IP>}:${NODE_PORT}"
