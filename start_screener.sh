#!/bin/bash
# Start the SCREENER dashboard (FastAPI) from this directory.
# Uses the parent project's virtualenv. Port configurable via PORT env (default 8000).
set -euo pipefail
cd "$(dirname "$0")"

# Portable: prefer a local .venv, else $SCREENER_VENV, else the freqvwap project venv.
VENV="${SCREENER_VENV:-/home/titus/freqvwap/.venv}"
[ -x ".venv/bin/uvicorn" ] && VENV="$(pwd)/.venv"
PORT="${PORT:-8000}"

if [ ! -x "$VENV/bin/uvicorn" ]; then
    echo "❌ Missing $VENV/bin/uvicorn (parent project venv)."; exit 1
fi
if pgrep -f "uvicorn data_server:app --host 0.0.0.0 --port $PORT" >/dev/null 2>&1; then
    echo "✓ Screener already running on port $PORT"; exit 0
fi

nohup "$VENV/bin/uvicorn" data_server:app --host 0.0.0.0 --port "$PORT" > screener.log 2>&1 &
echo $! > screener.pid
sleep 1
if ss -tln | grep -q ":$PORT"; then
    echo "✅ Screener started on :$PORT (PID $(cat screener.pid))"
else
    echo "❌ Failed to start. See $(pwd)/screener.log"; exit 1
fi
