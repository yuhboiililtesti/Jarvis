#!/usr/bin/env bash
# Install JARVIS Core as a systemd *user* service (Linux, no root).
#
# Creates and enables ~/.config/systemd/user/jarvis-core.service, which runs
# `.venv/bin/python -m jarvis serve` and restarts on failure. It survives
# logout via linger.
#
# Usage:  bash scripts/install-systemd.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -x ".venv/bin/python" ]; then
  echo "no .venv found — run: bash scripts/install.sh"
  exit 1
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/jarvis-core.service" <<EOF
[Unit]
Description=JARVIS Core (local-first assistant)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=PYTHONUNBUFFERED=1
ExecStart=$ROOT/.venv/bin/python -m jarvis serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now jarvis-core.service || {
  echo "NOTE: could not enable the service automatically."
  echo "      run: systemctl --user enable --now jarvis-core.service"
}

# Allow the user service to keep running after logout.
loginctl enable-linger "$USER" 2>/dev/null || true

echo
echo "=== done ==="
echo "status:  systemctl --user status jarvis-core"
echo "logs:    journalctl --user -u jarvis-core -f"
echo "HUD:     http://127.0.0.1:8765"
