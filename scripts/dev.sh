#!/usr/bin/env bash
# Run the API and the Vite dev server together, stopping both on Ctrl-C.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -d .venv ]; then
  echo "Creating virtualenv…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements-dev.txt
fi

if [ ! -d frontend/node_modules ]; then
  echo "Installing frontend dependencies…"
  (cd frontend && npm install)
fi

cleanup() {
  echo
  echo "Shutting down…"
  kill 0 2>/dev/null || true
}
trap cleanup EXIT INT TERM

(cd backend && ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT:-8000}" --reload) &
(cd frontend && npm run dev) &

wait
