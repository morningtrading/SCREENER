#!/bin/bash
# Regenerate the momentum (long) and shorts rankings, then fire Telegram alerts.
#   momentum_ranking.json — CMC trending × Binance/MEXC/Hyperliquid
#   shorts_ranking.json   — weakest MEXC/HL perps
# Designed to be run from cron every minute:
#   * * * * * /home/titus/SCREENER/refresh_momentum.sh
# A flock guard means that if a cycle ever runs long, the next minute's run is
# skipped instead of piling up (so concurrent scans never stack).
set -uo pipefail
cd "$(dirname "$0")"

# Single-instance guard: bail immediately if a previous run is still going.
exec 9>".refresh.lock"
if ! flock -n 9; then
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) skip (previous run still active) ===" >> momentum_refresh.log
    exit 0
fi

# Load local .env (token + SCREENER_* overrides: pairs file, optional CMC key, venv).
[ -f .env ] && set -a && . ./.env && set +a

# Python: local ./.venv, else $SCREENER_VENV, else system python3 (same as menu.sh).
if [ -x ".venv/bin/python" ]; then PY="$(pwd)/.venv/bin/python"
elif [ -n "${SCREENER_VENV:-}" ] && [ -x "$SCREENER_VENV/bin/python" ]; then PY="$SCREENER_VENV/bin/python"
else PY="python3"; fi

# Timestamped line + script output, appended to a rotating-ish log.
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) refresh ===" >> momentum_refresh.log

# momentum (longs) and shorts are independent — run them concurrently so the
# cycle is bounded by the slower of the two, not their sum.
"$PY" build_momentum.py  >> momentum_refresh.log 2>&1 &
mpid=$!
"$PY" build_shorts.py    >> momentum_refresh.log 2>&1 &
spid=$!
wait "$mpid"; wait "$spid"

# eval reads both rankings, so it runs after they finish.
"$PY" build_eval.py      >> momentum_refresh.log 2>&1

# Telegram alert for coins newly entering the LONG/SHORT lists (score > 1.9).
# No-ops silently unless TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set in .env.
"$PY" notify_telegram.py >> momentum_refresh.log 2>&1
