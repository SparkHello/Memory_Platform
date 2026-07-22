#!/usr/bin/env bash
# One-click launcher for memory-gateway on macOS/Linux.
#
# Usage:
#   scripts/dev.sh          # backend (reload) + Vite dev server, live UI editing
#   scripts/dev.sh prod     # backend only, serving the built ui/dist via /ui
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="${1:-dev}"
BACKEND_PORT="${PORT:-2026}"

if [ ! -d ".venv" ]; then
  echo "No .venv found. Create one first: python3 -m venv .venv && .venv/bin/pip install -e .[dev]" >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "No .env found. Copy .env.example to .env and fill it in first." >&2
  exit 1
fi

PIDS=()
cleanup() {
  echo
  echo "Stopping..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

echo "Starting backend on http://localhost:${BACKEND_PORT}"
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload &
PIDS+=("$!")

if [ "$MODE" = "prod" ]; then
  echo "Waiting on backend (ui served from ui/dist at /ui)..."
  wait
  exit 0
fi

if [ ! -d "ui/node_modules" ]; then
  echo "Installing ui dependencies..."
  (cd ui && npm install)
fi

echo "Starting Vite dev server (proxies API to backend)"
(cd ui && npm run dev) &
PIDS+=("$!")

wait
