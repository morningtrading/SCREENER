#!/bin/bash
# Regenerate the momentum (long) and shorts rankings.
#   momentum_ranking.json — CMC trending × Binance/MEXC/Hyperliquid
#   shorts_ranking.json   — weakest MEXC/HL perps
# Designed to be run from cron, e.g. every 5 minutes:
#   */5 * * * * /home/titus/SCREENER/refresh_momentum.sh
set -uo pipefail
cd "$(dirname "$0")"

# Load local .env (token + SCREENER_* overrides: pairs file, optional CMC key, venv).
[ -f .env ] && set -a && . ./.env && set +a

# Python: local ./.venv, else $SCREENER_VENV, else system python3 (same as menu.sh).
if [ -x ".venv/bin/python" ]; then PY="$(pwd)/.venv/bin/python"
elif [ -n "${SCREENER_VENV:-}" ] && [ -x "$SCREENER_VENV/bin/python" ]; then PY="$SCREENER_VENV/bin/python"
else PY="python3"; fi

# Timestamped line + script output, appended to a rotating-ish log.
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) refresh ===" >> momentum_refresh.log
"$PY" build_momentum.py  >> momentum_refresh.log 2>&1
"$PY" build_shorts.py    >> momentum_refresh.log 2>&1
"$PY" build_eval.py      >> momentum_refresh.log 2>&1

# Telegram alert for coins newly entering the LONG/SHORT lists (score > 1.9).
# No-ops silently unless TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set in .env.
"$PY" notify_telegram.py >> momentum_refresh.log 2>&1
