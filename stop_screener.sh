#!/bin/bash
# Stop the SCREENER dashboard. Port configurable via PORT env (default 8000).
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
if pkill -f "uvicorn data_server:app .*--port $PORT"; then
    echo "🛑 Stopped screener on :$PORT"
else
    echo "Not running on :$PORT"
fi
rm -f screener.pid
