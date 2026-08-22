#!/usr/bin/env bash
# Bring up the full GeoNexus Web reference stack.
#   bash scripts/dev.sh            # starts everything on 8790/8787/8900
#   open http://127.0.0.1:8900     # (or visit the static frontend)
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=${PYTHON:-.venv/bin/python}
if [[ ! -x "$PYTHON" ]]; then
  echo "==> Creating venv and installing dependencies"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.txt
  # Use the sibling SDK checkout when present (dev mode), else PyPI.
  if [[ -d ../mvp ]]; then
    .venv/bin/pip install -e ../mvp >/dev/null 2>&1 || true
  fi
fi

echo "==> Starting demo stack (registry :8790, node :8787, web :8900)"
exec "$PYTHON" backend/demo_stack.py
