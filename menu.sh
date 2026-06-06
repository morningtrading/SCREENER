#!/bin/bash
# SCREENER control menu
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
ENV_FILE=/home/titus/freqvwap/.env
VENV=/home/titus/freqvwap/.venv
TOKEN=$(grep '^DATA_SERVER_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
HOST="permanent"

urls() {
    echo "  Landing : http://$HOST:$PORT/?token=$TOKEN"
    echo "  Summary : http://$HOST:$PORT/summary?token=$TOKEN"
    echo "  Full Binance rank: http://$HOST:$PORT/binance-ranking?token=$TOKEN"
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
    echo "  5) Show URLs (with token)"
    echo "  6) Tail dashboard log"
    echo "  0) Exit"
    echo "--------------------------------------------"
    read -rp "Choose: " c
    case "$c" in
        1) ./start_screener.sh ;;
        2) ./stop_screener.sh ;;
        3) ./status_screener.sh ;;
        4) "$VENV/bin/python" build_binance_ranking.py ;;
        5) urls ;;
        6) tail -n 30 -f screener.log ;;
        0) exit 0 ;;
        *) echo "Invalid choice" ;;
    esac
done
