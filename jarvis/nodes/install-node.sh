#!/usr/bin/env bash
# install-node.sh -- install the JARVIS satellite node on Ubuntu (no root).
#
# Idempotent: safe to re-run. It
#   1. checks for python3 (>= 3.11),
#   2. creates a virtualenv at $JARVIS_DIR/.venv if missing,
#   3. installs the minimal runtime requirements,
#   4. writes + enables a systemd *user* unit "jarvis-node.service",
#   5. prints the health URL.
#
# Overridable via environment:
#   JARVIS_DIR    repo checkout    (default: ~/Projects/AI/Jarvis)
#   JARVIS_VENV   virtualenv path  (default: $JARVIS_DIR/.venv)
#   JARVIS_NODE_PORT  bind port    (default: config node_port, 2226)
#   PYTHON        interpreter      (default: python3)

set -euo pipefail

REPO_DIR="${JARVIS_DIR:-$HOME/Projects/AI/Jarvis}"
VENV_DIR="${JARVIS_VENV:-$REPO_DIR/.venv}"
PYTHON="${PYTHON:-python3}"
NODE_PORT="${JARVIS_NODE_PORT:-2226}"
UNIT_NAME="jarvis-node.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

log() { printf '%s\n' "[install-node] $*" >&2; }
die() { printf '%s\n' "[install-node] ERROR: $*" >&2; exit 1; }

# -- 1. python3 present and new enough --------------------------------------
command -v "$PYTHON" >/dev/null 2>&1 \
  || die "python3 not found. Install it with: sudo apt install python3 python3-venv"

if ! "$PYTHON" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
then
  die "python3 >= 3.11 is required (found $("$PYTHON" --version 2>&1))"
fi

# -- 2. repo + virtualenv ----------------------------------------------------
[ -d "$REPO_DIR" ] || die "repo not found at $REPO_DIR (set JARVIS_DIR)"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  log "creating virtualenv at $VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR" \
    || die "could not create venv. Install it with: sudo apt install python3-venv"
else
  log "reusing virtualenv at $VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"

# -- 3. minimal requirements -------------------------------------------------
log "installing minimal requirements (fastapi, uvicorn, httpx, psutil)"
"$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
"$VENV_PY" -m pip install --upgrade fastapi "uvicorn[standard]" httpx psutil \
  || die "pip install failed (check your network / pip index)"

# -- 4. systemd user unit ----------------------------------------------------
mkdir -p "$UNIT_DIR"

UNIT_CONTENT="[Unit]
Description=JARVIS satellite node agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=JARVIS_NODE_PORT=$NODE_PORT
ExecStart=$VENV_PY -m jarvis.nodes --host 0.0.0.0 --port $NODE_PORT
Restart=on-failure
RestartSec=5
# The shared token comes from config/JARVIS_NODES__TOKEN; never log it.
NoNewPrivileges=true

[Install]
WantedBy=default.target
"

if [ ! -f "$UNIT_PATH" ] || [ "$(cat "$UNIT_PATH")" != "$UNIT_CONTENT" ]; then
  printf '%s' "$UNIT_CONTENT" > "$UNIT_PATH"
  log "wrote $UNIT_PATH"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
else
  log "unit already up to date"
fi

if systemctl --user enable "$UNIT_NAME" >/dev/null 2>&1; then
  log "enabled $UNIT_NAME"
else
  log "could not enable $UNIT_NAME (is a user systemd session available?)"
fi

if systemctl --user restart "$UNIT_NAME" >/dev/null 2>&1 \
   || systemctl --user start "$UNIT_NAME" >/dev/null 2>&1; then
  log "started $UNIT_NAME"
else
  log "could not start it now; run: systemctl --user start $UNIT_NAME"
fi

# Optional: survive logout. Usually needs privileges; never fatal.
if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$USER" >/dev/null 2>&1 \
    || log "note: 'loginctl enable-linger $USER' was refused; run it yourself for boot persistence"
fi

# -- 5. report ---------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${IP:-}" ] || IP="<this-host>"
log "done."
log "health URL:  http://$IP:$NODE_PORT/node/health"
log "local check: curl -H 'X-Jarvis-Node-Token: <token>' http://127.0.0.1:$NODE_PORT/node/health"
log "logs:        journalctl --user -u $UNIT_NAME -f"
