#!/bin/bash
# Show SCREENER dashboard status + health check. Port via PORT env (default 8000).
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
TOKEN=$(grep '^DATA_SERVER_TOKEN=' /home/titus/freqvwap/.env | cut -d= -f2)

echo "=== SCREENER status (port $PORT) ==="
if pgrep -f "uvicorn data_server:app --host 0.0.0.0 --port $PORT" >/dev/null; then
    echo "Running: YES"
    pgrep -af "uvicorn data_server:app --host 0.0.0.0 --port $PORT"
else
    echo "Running: NO"
fi
ss -tln | grep ":$PORT" || echo "(no listener on $PORT)"
code=$(curl -s -m8 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/?token=$TOKEN" || true)
echo "Landing page health: HTTP $code"
