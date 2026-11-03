#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
echo "Open http://localhost:8770/hub.html"
exec .venv/bin/python hub/server.py
