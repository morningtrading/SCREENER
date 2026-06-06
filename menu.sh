#!/bin/bash
# SCREENER control menu
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
# Load local .env (token + optional SCREENER_* overrides).
[ -f .env ] && set -a && . ./.env && set +a
ENV_FILE="${SCREENER_ENV_FILE:-.env}"
TOKEN=$(grep '^DATA_SERVER_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
[ -z "$TOKEN" ] && TOKEN="${DATA_SERVER_TOKEN:-}"
# Python: local ./.venv, else $SCREENER_VENV, else system python3.
if [ -x ".venv/bin/python" ]; then PY="$(pwd)/.venv/bin/python"
elif [ -n "${SCREENER_VENV:-}" ] && [ -x "$SCREENER_VENV/bin/python" ]; then PY="$SCREENER_VENV/bin/python"
else PY="python3"; fi
HOST="${SCREENER_HOST:-permanent}"

urls() {
    echo "  Landing : http://$HOST:$PORT/?token=$TOKEN"
    echo "  Summary : http://$HOST:$PORT/summary?token=$TOKEN"
    echo "  Binance ranking : http://$HOST:$PORT/binance-ranking?token=$TOKEN"
    echo "  MEXC ranking    : http://$HOST:$PORT/mexc-ranking?token=$TOKEN"
    echo "  Combined        : http://$HOST:$PORT/combined?token=$TOKEN"
}

while true; do
    echo ""
    echo "============================================"
    echo "   SCREENER  (data dashboard)   port $PORT"
    echo "============================================"
    echo "  1) Start dashboard"
    echo "  2) Stop dashboard"
    echo "  3) Status / health check"
    echo "  4) Refresh FULL Binance ranking"
    echo "  5) Refresh FULL MEXC ranking"
    echo "  6) Show URLs (with token)"
    echo "  7) Tail dashboard log"
    echo "  0) Exit"
    echo "--------------------------------------------"
    read -rp "Choose: " c
    case "$c" in
        1) ./start_screener.sh ;;
        2) ./stop_screener.sh ;;
        3) ./status_screener.sh ;;
        4) "$PY" build_binance_ranking.py ;;
        5) "$PY" build_mexc_ranking.py ;;
        6) urls ;;
        7) tail -n 30 -f screener.log ;;
        0) exit 0 ;;
        *) echo "Invalid choice" ;;
    esac
done
