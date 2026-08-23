#!/usr/bin/env bash
# Bring up the GeoNexus Web application (Amazon vegetation change analysis).
#   bash scripts/dev.sh
#   open http://127.0.0.1:8900
set -euo pipefail
cd "$(dirname "$0")/.."

# --- Pick a Python >= 3.10 (geonexus-sdk requires it) --------------------- #
PY_CMD=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
    PY_CMD="$c"
    break
  fi
done
if [[ -z "$PY_CMD" ]]; then
  echo "ERROR: need Python >= 3.10 (found only: $(python3 --version 2>&1))" >&2
  exit 1
fi
echo "==> Using $($PY_CMD --version) ($PY_CMD)"

PYTHON=${PYTHON:-.venv/bin/python}
# Rebuild the venv when missing OR built with an unsupported Python version.
NEED_VENV=0
if [[ ! -x "$PYTHON" ]]; then
  NEED_VENV=1
elif ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
  echo "==> Existing venv uses an unsupported Python; rebuilding"
  rm -rf .venv
  NEED_VENV=1
fi
if [[ "$NEED_VENV" == "1" ]]; then
  "$PY_CMD" -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
fi

# --- Install the SDK: prefer the sibling mvp checkout (dev), else PyPI ---- #
if [[ -d ../mvp && -f ../mvp/pyproject.toml ]]; then
  echo "==> Installing SDK from sibling checkout (../mvp, dev mode)"
  .venv/bin/pip install -e "../mvp[mcp]" >/dev/null
  .venv/bin/pip install -r requirements.txt >/dev/null
else
  echo "==> Installing SDK from PyPI"
  .venv/bin/pip install -r requirements.txt >/dev/null
fi

echo "==> Starting GeoNexus Web application"
echo "    registry :8790 | node :8787 | web :8900 | MCP :9001"
echo "    open http://127.0.0.1:8900  (demo / demo1234)"
exec "$PYTHON" backend/demo_stack.py
