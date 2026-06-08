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

gen_of() { grep -o '"generated_utc": *"[^"]*"' "$1" 2>/dev/null | head -1 | cut -d'"' -f4; }

status() {
    echo "--------------------------------------------"
    # Server (the dashboard)
    if pgrep -f "uvicorn data_server:app --host 0.0.0.0 --port $PORT" >/dev/null 2>&1; then
        pid=$(pgrep -f "uvicorn data_server:app --host 0.0.0.0 --port $PORT" | head -1)
        echo "  Server      : UP   (pid $pid, port $PORT)"
    else
        echo "  Server      : DOWN"
    fi
    # Bot = the 5-min auto-refresh cron job
    if crontab -l 2>/dev/null | grep -q 'refresh_momentum.sh'; then
        last=$(grep '=== .* refresh ===' momentum_refresh.log 2>/dev/null | tail -1 | tr -d '=' | xargs 2>/dev/null)
        echo "  Bot (cron)  : ON  (*/5 min)  last run: ${last:-never}"
    else
        echo "  Bot (cron)  : OFF (not installed)"
    fi
    # Data freshness + pick history
    echo "  Momentum    : data $(gen_of momentum_ranking.json || echo '—')   picks $(wc -l < momentum_history/momentum_picks.jsonl 2>/dev/null || echo 0)"
    echo "  Shorts      : data $(gen_of shorts_ranking.json   || echo '—')   picks $(wc -l < shorts_history/short_picks.jsonl 2>/dev/null   || echo 0)"
    echo "--------------------------------------------"
}

urls() {
    echo "  Landing  : http://$HOST:$PORT/?token=$TOKEN"
    echo "  Summary  : http://$HOST:$PORT/summary?token=$TOKEN"
    echo "  Combined : http://$HOST:$PORT/combined?token=$TOKEN"
    echo "  Momentum : http://$HOST:$PORT/momentum?token=$TOKEN"
    echo "  Shorts   : http://$HOST:$PORT/shorts?token=$TOKEN"
    echo "  Results  : http://$HOST:$PORT/results?token=$TOKEN"
}

while true; do
    echo ""
    echo "============================================"
    echo "   SCREENER  (data dashboard)   port $PORT"
    echo "============================================"
    status
    echo "  1) Start dashboard"
    echo "  2) Stop dashboard"
    echo "  3) Detailed status / health check"
    echo "  4) Refresh momentum + shorts now"
    echo "  5) Refresh Binance + MEXC rankings"
    echo "  6) Evaluate SHORTS  (were we right?)"
    echo "  7) Evaluate MOMENTUM (were we right?)"
    echo "  8) Show URLs (with token)"
    echo "  9) Tail dashboard log"
    echo "  0) Exit"
    echo "--------------------------------------------"
    read -rp "Choose: " c
    case "$c" in
        1) ./start_screener.sh ;;
        2) ./stop_screener.sh ;;
        3) ./status_screener.sh ;;
        4) "$PY" build_momentum.py && "$PY" build_shorts.py ;;
        5) "$PY" build_binance_ranking.py && "$PY" build_mexc_ranking.py ;;
        6) read -rp "Min age hours [0]: " a; "$PY" eval_shorts.py --min-age-hours "${a:-0}" ;;
        7) read -rp "Min age hours [0]: " a; "$PY" eval_momentum.py --min-age-hours "${a:-0}" ;;
        8) urls ;;
        9) tail -n 30 -f screener.log ;;
        0) exit 0 ;;
        *) echo "Invalid choice" ;;
    esac
done
