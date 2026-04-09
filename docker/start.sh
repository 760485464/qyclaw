#!/bin/sh
set -eu

FRONTEND_PORT="${FRONTEND_PORT:-8080}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"

cleanup() {
  if [ -n "${FRONTEND_PID:-}" ]; then
    kill "${FRONTEND_PID}" 2>/dev/null || true
  fi
}

trap cleanup INT TERM EXIT

echo "Starting Qyclaw frontend on ${FRONTEND_HOST}:${FRONTEND_PORT}"
python -m http.server "${FRONTEND_PORT}" --bind "${FRONTEND_HOST}" --directory /app &
FRONTEND_PID=$!

echo "Starting Qyclaw backend on ${BACKEND_HOST}:${BACKEND_PORT}"
exec python -m uvicorn backend.main:app --host "${BACKEND_HOST}" --port "${BACKEND_PORT}"
