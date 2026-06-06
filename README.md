# SCREENER — Crypto Data Dashboard

A self-contained web dashboard for browsing the project's Binance futures OHLCV data
and ranking coins by trading cost (spread + fees) and volatility.

It is a packaged copy of the data server and ranking tools. It **reads** the canonical
project data, pairlist, and credentials from `/home/titus/freqvwap/` but keeps its own
generated output (`binance_ranking.json`) and logs inside this folder.

## Contents

| File | Purpose |
|------|---------|
| `data_server.py` | FastAPI web app (the dashboard). Serves the pages and file downloads. |
| `build_binance_ranking.py` | Generates the **full Binance universe** ranking (`binance_ranking.json`). |
| `rank_spreads.py` | Legacy Hyperliquid+Binance spread/arbitrage tool (reference). |
| `sample_csv/` | Example CSV outputs from `rank_spreads.py`. |
| `start_screener.sh` / `stop_screener.sh` / `status_screener.sh` | Service control. |
| `menu.sh` | Interactive control menu (start/stop/refresh/URLs). |
| `binance_ranking.json` | Generated ranking data (read by the dashboard). |
| `screener.log` / `screener.pid` | Runtime log and PID (created on start). |

## Requirements

Uses the parent project's virtualenv at `/home/titus/freqvwap/.venv`
(FastAPI, uvicorn, pandas, python-dotenv). No separate install needed.

## Paths it depends on (canonical project)

- **Market data:** `/home/titus/freqvwap/user_data/data/futures/*.feather`
- **Pairlist:** `/home/titus/freqvwap/user_data/pairs.json`
- **Access token:** `DATA_SERVER_TOKEN` in `/home/titus/freqvwap/.env`

## Quick start

```bash
cd /home/titus/SCREENER
./menu.sh                 # interactive
# or directly:
./start_screener.sh       # starts on port 8000 (override: PORT=8001 ./start_screener.sh)
./status_screener.sh
./stop_screener.sh
```

All endpoints require the token (`?token=...` or `x-access-token` header).
Get the token from `/home/titus/freqvwap/.env`.

## Endpoints

| Path | Description |
|------|-------------|
| `/` | Landing page with navigation + coin dropdown |
| `/summary` | File table (per coin/timeframe), feather + CSV download, TradingView links |
| `/summary.json` | Machine-readable file summary (`?showmeasset=BNB` filter) |
| `/binance-ranking` | **Every** Binance perpetual ranked; filtered by volume, spread, volatility; green = tradeable |
| `/binance-good-pairs.json` | The "good" coins as a freqtrade pairlist (download candidates) |
| `/file/{name}` | Download a raw `.feather` file |
| `/csv/{name}` | Download a `.feather` file converted to CSV on the fly |

## Refreshing rankings

The ranking is a snapshot. Regenerate any time (menu option 4, or):

```bash
/home/titus/freqvwap/.venv/bin/python build_binance_ranking.py   # full universe
```

### "Good" coin criteria (full Binance ranking)

A coin is flagged **GOOD** (green) when all hold:
- 24h quote volume ≥ 1,000,000 USDT
- bid/ask spread ≤ 0.10%
- 24h volatility (range) ≥ 2.0%

Thresholds live at the top of `build_binance_ranking.py`
(`MIN_VOLUME`, `MAX_SPREAD_PCT`, `MIN_VOLATILITY_PCT`).

## Note on ports

The original project dashboard may already run on port **8000**. To run SCREENER alongside it
for testing, use a different port: `PORT=8001 ./start_screener.sh`.
