#!/usr/bin/env bash
# Diagnostic only — confirms what the node can use without root.
echo "--- python ---"
python3 --version
python3 -c "import venv, sys; print('venv module: ok')" 2>&1
echo "--- pip ---"
command -v pip3 || echo "pip3: MISSING"
python3 -m pip --version 2>&1 | head -1
echo "--- network ---"
timeout 12 curl -sS -o /dev/null -w "pypi: %{http_code}\n" https://pypi.org/simple/ 2>&1 || echo "pypi: unreachable"
timeout 12 curl -sS -o /dev/null -w "bootstrap: %{http_code}\n" https://bootstrap.pypa.io/get-pip.py 2>&1 || echo "bootstrap: unreachable"
echo "--- existing site-packages ---"
python3 -c "import fastapi" 2>&1 | tail -1
python3 -c "import httpx" 2>&1 | tail -1
python3 -c "import uvicorn" 2>&1 | tail -1
python3 -c "import psutil" 2>&1 | tail -1
echo "--- sudo (NOT used, just reporting) ---"
sudo -n true 2>&1 && echo "passwordless sudo: available" || echo "passwordless sudo: not available"
echo "--- tools ---"
for t in curl tar python3; do printf '%-8s ' "$t"; command -v "$t" || echo MISSING; done
