#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1
if [ -x ".venv/bin/python" ]; then
  exec ".venv/bin/python" -B run_app.py "$@"
elif command -v python3 >/dev/null 2>&1; then
  echo "Setting up local Python environment (.venv) for first-time launch..."
  python3 -m venv .venv 2>/dev/null || true
  if [ -x ".venv/bin/python" ]; then
    exec ".venv/bin/python" -B run_app.py "$@"
  else
    exec python3 -B run_app.py "$@"
  fi
else
  echo "Error: Python 3.11+ is required. Please install Python from https://www.python.org/"
  exit 1
fi
