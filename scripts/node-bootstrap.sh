#!/usr/bin/env bash
# Bootstrap the JARVIS node on the ThinkPad SL510.
# Run from the Core:  Get-Content scripts/node-bootstrap.sh | ssh laptop "bash -s"
# It installs only into ~/Projects/AI/Jarvis and ~/.config/systemd/user.
set -euo pipefail

ROOT="$HOME/Projects/AI/Jarvis"
echo "=== host: $(hostname) ($(uname -m)) ==="

mkdir -p "$ROOT"
tar -xzf "$HOME/node-deploy.tgz" -C "$ROOT"
mkdir -p "$ROOT/data"
install -m 600 "$HOME/node-token" "$ROOT/data/node-token"
rm -f "$HOME/node-deploy.tgz" "$HOME/node-token"

cd "$ROOT"
echo "=== extracted $(find . -type f | wc -l) files to $ROOT ==="

# The shared installer creates the venv, installs deps, writes the systemd user
# unit (with the token in the environment) and starts it.
bash scripts/install-laptop-node.sh
