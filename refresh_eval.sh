#!/bin/bash
# Rebuild the /eval backtest dashboard (eval_results.json) on its own schedule.
# Split out from refresh_momentum.sh because build_eval is slow and variable and
# must NOT block the 1-minute alert loop. Designed to run from cron every 5 min:
#   */5 * * * * /home/titus/SCREENER/refresh_eval.sh
set -uo pipefail
cd "$(dirname "$0")"

# Own single-instance guard (separate from the alert loop's .refresh.lock).
exec 9>".eval.lock"
if ! flock -n 9; then
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) eval skip (previous run still active) ===" >> momentum_refresh.log
    exit 0
fi

# Load local .env (SCREENER_* overrides: pairs file, venv).
[ -f .env ] && set -a && . ./.env && set +a

# Python: local ./.venv, else $SCREENER_VENV, else system python3 (same as menu.sh).
if [ -x ".venv/bin/python" ]; then PY="$(pwd)/.venv/bin/python"
elif [ -n "${SCREENER_VENV:-}" ] && [ -x "$SCREENER_VENV/bin/python" ]; then PY="$SCREENER_VENV/bin/python"
else PY="python3"; fi

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) eval refresh ===" >> momentum_refresh.log
# Run at low CPU/IO priority: this box has only 2 cores, and the backtest is
# CPU-heavy (pandas). Niced, it yields to the 1-minute alert loop's builds when
# they overlap, so eval never slows down alert latency.
nice -n 19 ionice -c3 "$PY" build_eval.py >> momentum_refresh.log 2>&1 \
    || "$PY" build_eval.py >> momentum_refresh.log 2>&1
