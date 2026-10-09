#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi

echo "Installing Python packages..."
.venv/bin/python -m pip install -q -r requirements.txt
echo "Installing Chromium for Playwright..."
.venv/bin/python -m playwright install chromium
echo "Starting Amber at http://127.0.0.1:8080"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
