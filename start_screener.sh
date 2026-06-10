#!/bin/bash
# Start the SCREENER dashboard (FastAPI) from this directory.
# Port configurable via PORT env (default 8000).
set -euo pipefail
cd "$(dirname "$0")"

# Load local .env (token + optional SCREENER_* location overrides).
[ -f .env ] && set -a && . ./.env && set +a
PORT="${PORT:-8000}"

# Resolve uvicorn: local ./.venv, else $SCREENER_VENV, else PATH.
if [ -x ".venv/bin/uvicorn" ]; then
    UVICORN="$(pwd)/.venv/bin/uvicorn"
elif [ -n "${SCREENER_VENV:-}" ] && [ -x "$SCREENER_VENV/bin/uvicorn" ]; then
    UVICORN="$SCREENER_VENV/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
    UVICORN="uvicorn"
else
    echo "❌ uvicorn not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1
fi
if pgrep -f "uvicorn data_server:app --host 0.0.0.0 --port $PORT" >/dev/null 2>&1; then
    echo "✓ Screener already running on port $PORT"; exit 0
fi

nohup "$UVICORN" data_server:app --host 0.0.0.0 --port "$PORT" > screener.log 2>&1 &
echo $! > screener.pid
sleep 1
if ss -tln | grep -q ":$PORT"; then
    echo "✅ Screener started on :$PORT (PID $(cat screener.pid))"
else
    echo "❌ Failed to start. See $(pwd)/screener.log"; exit 1
fi
