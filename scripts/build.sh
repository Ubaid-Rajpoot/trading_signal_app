#!/usr/bin/env bash
# Build the frontend into backend/static so a single uvicorn process serves
# both the API and the UI (handy for deployment).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/frontend"

npm install
npm run build

rm -rf "$ROOT/backend/static"
cp -r dist "$ROOT/backend/static"

echo "Built frontend -> backend/static"
echo "Start it with:  cd backend && ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
