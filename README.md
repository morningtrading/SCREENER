# SCREENER — Crypto Data Dashboard

A self-contained web dashboard for browsing Binance futures OHLCV data and
ranking coins by trading cost (spread + fees) and volatility.

It reads its market data, pairlist, and access token from configurable locations
(see **Configuration**) and keeps its own generated output (`binance_ranking.json`)
and logs inside this folder.

## Contents

| File | Purpose |
|------|---------|
| `data_server.py` | FastAPI web app (the dashboard). Serves the pages and file downloads. |
| `build_binance_ranking.py` | Generates the **full Binance universe** ranking (`binance_ranking.json`). |
| `config.json` | **Tunable fee & filter thresholds** read by the ranking generator. |
| `rank_spreads.py` | Hyperliquid + Binance spread/arbitrage tool (reference). |
| `sample_csv/` | Example CSV outputs from `rank_spreads.py`. |
| `start_screener.sh` / `stop_screener.sh` / `status_screener.sh` | Service control. |
| `menu.sh` | Interactive control menu (start/stop/refresh/URLs). |
| `binance_ranking.json` | Generated ranking data (read by the dashboard). |
| `screener.log` / `screener.pid` | Runtime log and PID (created on start). |

## Requirements

Python 3.10+. Create a local virtualenv:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The shell scripts auto-prefer a local `./.venv` if present, otherwise the venv
named in `$SCREENER_VENV`.

## Configuration (portable)

All external locations are environment variables. Set them in a local `.env`
(see `.env.example`) or your shell so the dashboard can run against data anywhere:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATA_SERVER_TOKEN` | *(required)* | Access token for all endpoints |
| `SCREENER_PROJECT_ROOT` | *(set per host)* | Base used to derive the defaults below |
| `SCREENER_DATA_DIR` | `$SCREENER_PROJECT_ROOT/data/futures` | Folder of `.feather` data files to serve |
| `SCREENER_PAIRS_FILE` | `$SCREENER_PROJECT_ROOT/pairs.json` | Pair list (`{"pairs": [...]}`) for the ranking generator |
| `SCREENER_ENV_FILE` | `$SCREENER_PROJECT_ROOT/.env` | File to read the token from |
| `SCREENER_VENV` | `$SCREENER_PROJECT_ROOT/.venv` | Python venv for the shell scripts |
| `SCREENER_HOST` | `permanent` | Hostname shown in the menu URLs |

A local `.env` placed in this folder is loaded first, so it can hold both the token
and any `SCREENER_*` overrides. Generated output (`binance_ranking.json`) is always
written inside this folder.

> The market data folder can be large (hundreds of MB to GB) and is **not** part of
> this repository — point `SCREENER_DATA_DIR` at wherever the `.feather` files live.

## Quick start

```bash
cd /home/titus/SCREENER
./menu.sh                 # interactive
# or directly:
./start_screener.sh       # starts on port 8000 (override: PORT=8001 ./start_screener.sh)
./status_screener.sh
./stop_screener.sh
```

All endpoints require the token (`?token=...` or `x-access-token` header),
read from `DATA_SERVER_TOKEN`.

## Endpoints

| Path | Description |
|------|-------------|
| `/` | Landing page with navigation + coin dropdown |
| `/summary` | File table (per coin/timeframe), feather + CSV download, TradingView links |
| `/summary.json` | Machine-readable file summary (`?showmeasset=BNB` filter) |
| `/binance-ranking` | **Every** Binance perpetual ranked; filtered by volume, spread, volatility; green = tradeable |
| `/binance-good-pairs.json` | The "good" coins as a JSON pair list (download candidates) |
| `/file/{name}` | Download a raw `.feather` file |
| `/csv/{name}` | Download a `.feather` file converted to CSV on the fly |

## Refreshing rankings

The ranking is a snapshot. Regenerate any time (menu option 4, or):

```bash
.venv/bin/python build_binance_ranking.py   # full Binance universe
```

### Filter parameters — `config.json`

The fee and "good"-coin thresholds are **not in code** — they live in `config.json`:

```json
{
  "fees":    { "taker_pct": 0.04 },
  "filters": {
    "min_volume_usdt":    1000000,
    "max_spread_pct":     0.10,
    "min_volatility_pct": 2.0
  }
}
```

A coin is flagged **GOOD** (green) when **all** hold:
- 24h quote volume ≥ `min_volume_usdt`
- bid/ask spread ≤ `max_spread_pct`
- 24h volatility (range) ≥ `min_volatility_pct`

Edit `config.json` and re-run the generator — no code change needed. Point at a
different file with `SCREENER_CONFIG=/path/to/config.json`. Any missing key falls
back to a built-in default, so the tool still runs if the file is absent.

## Note on ports

The dashboard defaults to port **8000**. To run a second instance alongside it,
use a different port: `PORT=8001 ./start_screener.sh`.
