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
| `build_mexc_ranking.py` | Generates the **full MEXC universe** ranking (`mexc_ranking.json`) — public API, no key. |
| `config.json` | **Tunable fee & filter thresholds** read by the ranking generators. |
| `data/futures/` | **Bundled sample data** — 15m/1h/1d for the top-20 pairlist coins (see below). |
| `pairs.json` | The 20 bundled coins as a pair list (default for the ranking generator). |
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
| `DATA_SERVER_TOKEN` | *(required)* | Access token / API key for all endpoints |
| `SCREENER_USER` | `admin` | Username for the login page & HTTP Basic auth |
| `SCREENER_PASSWORD` | falls back to `DATA_SERVER_TOKEN` | Password for the login page & HTTP Basic auth |
| `SCREENER_SECRET_KEY` | derived from the token | Secret used to sign the session cookie |
| `SCREENER_SESSION_MAX_AGE` | `604800` (7 days) | Session cookie lifetime, in seconds |
| `SCREENER_PROJECT_ROOT` | the SCREENER folder | Base used to derive the defaults below |
| `SCREENER_DATA_DIR` | `<SCREENER>/data/futures` | Folder of `.feather` data files to serve |
| `SCREENER_PAIRS_FILE` | `$SCREENER_PROJECT_ROOT/pairs.json` | Pair list (`{"pairs": [...]}`) for the ranking generator |
| `SCREENER_ENV_FILE` | `$SCREENER_PROJECT_ROOT/.env` | File to read the token from |
| `SCREENER_VENV` | `$SCREENER_PROJECT_ROOT/.venv` | Python venv for the shell scripts |
| `SCREENER_HOST` | `permanent` | Hostname shown in the menu URLs |

A local `.env` placed in this folder is loaded first, so it can hold both the token
and any `SCREENER_*` overrides. Generated output (`binance_ranking.json`) is always
written inside this folder.

## Bundled data — operational out of the box

The repo ships with `data/futures/` pre-loaded so a fresh clone works immediately for
**20 assets** (the highest-volume coins in the pairlist), at **15m, 1h and 1d** (~50 MB total):

| | | | | |
|---|---|---|---|---|
| BTC | ETH | ZEC | SOL | HYPE |
| XRP | WLD | DOGE | BNB | 1000PEPE |
| NEAR | ADA | SUI | ENA | XLM |
| AVAX | BCH | LINK | FIL | BABY |

*(ranked by 24h USDT volume at bundle time)*

After cloning, the dashboard is live for these 20 once you (1) `pip install -r requirements.txt`,
and (2) set `DATA_SERVER_TOKEN` (e.g. in a local `.env`). The default `SCREENER_DATA_DIR`
is this folder's `data/futures`, so no path setup is needed.

**More assets / timeframes** are downloaded on the destination machine into `data/futures/`
(those extra files are gitignored, so they won't bloat the repo). The full pipeline that
produced this data lives outside this repo.

## Quick start (fresh clone — all-inclusive)

From nothing to a live dashboard for the 20 bundled assets:

```bash
# 1. Clone
git clone git@github.com:morningtrading/SCREENER.git
cd SCREENER

# 2. Install dependencies into a local venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Set an access token (required; no insecure default)
echo "DATA_SERVER_TOKEN=$(openssl rand -hex 24)" > .env

# 4. Start — serves the 20 bundled coins on port 8000
./start_screener.sh

# 5. Open the dashboard (token is in your .env)
echo "http://localhost:8000/?token=$(grep '^DATA_SERVER_TOKEN=' .env | cut -d= -f2)"
```

Then `./status_screener.sh` to health-check, `./stop_screener.sh` to stop, or
`./menu.sh` for an interactive control panel. Use a different port with
`PORT=8001 ./start_screener.sh`.

No `data/` path setup is needed — it defaults to the bundled `data/futures/`.
To serve a larger dataset, drop more `.feather` files into `data/futures/`
(or point `SCREENER_DATA_DIR` elsewhere) — see **Configuration**.

## Authentication

Every endpoint is protected. Any **one** of these grants access:

1. **Session login (browser)** — visit any page and you're redirected to a `/login`
   screen. Sign in with `SCREENER_USER` / `SCREENER_PASSWORD` (defaults: `admin` and
   your `DATA_SERVER_TOKEN`). A signed cookie then keeps you logged in for
   `SCREENER_SESSION_MAX_AGE` (7 days by default). Log out via `/logout`.
2. **HTTP Basic auth** — send `Authorization: Basic <base64(user:password)>`. Handy for
   scripts and for browsers that prefer the native prompt:
   `curl -u admin:$TOKEN http://host:8000/summary.json`
3. **Access token** — `?token=...` query param or `x-access-token` header, as before:
   `curl -H "x-access-token: $TOKEN" http://host:8000/summary.json`

Browsers should use the login page (the token no longer needs to ride along in URLs
once you have a session). Scripts can keep using the token or Basic auth.

> Put the server behind HTTPS (or a TLS-terminating reverse proxy) for real use, and
> set `https_only=True` on the session cookie in `data_server.py`.

## Endpoints

| Path | Description |
|------|-------------|
| `/` | Landing page with navigation + coin dropdown |
| `/summary` | File table (per coin/timeframe), feather + CSV download, TradingView links |
| `/summary.json` | Machine-readable file summary (`?showmeasset=BNB` filter) |
| `/binance-ranking` | **Every** Binance perpetual ranked; filtered by volume, spread, volatility; green = tradeable |
| `/mexc-ranking` | Same ranking for **MEXC** futures (public API, no key); coin names link to Binance data |
| `/combined` | **Trade-on-MEXC** selection view + Binance backtest-data (CSV) links side by side |
| `/momentum` | **CoinMarketCap trending** coins scored on Binance 1h/2h/4h; green = real uptrend, not post-pump |
| `/momentum.json` | Machine-readable momentum scores (the same data as `/momentum`) |
| `/shorts` | Weakest **MEXC/HL** perps to short — weak + liquid + low reversal risk; ⚠️ flags squeeze/bounce risk |
| `/shorts.json` | Machine-readable short scores (the same data as `/shorts`) |
| `/results` | **Track record** — both scorecards (longs + shorts): each pick's entry price vs the live price + hit-rate |
| `/results.json` | Machine-readable results (the same data as `/results`) |
| `/binance-good-pairs.json` / `/mexc-good-pairs.json` | The "good" coins as a JSON pair list (download candidates) |
| `/file/{name}` | Download a raw `.feather` file |
| `/csv/{name}` | Download a `.feather` file converted to CSV on the fly |

## Refreshing rankings

The ranking is a snapshot. Regenerate any time (menu option 4, or):

```bash
.venv/bin/python build_binance_ranking.py   # full Binance universe
.venv/bin/python build_mexc_ranking.py       # full MEXC universe (public API, no key)
```

### Filter parameters — `config.json`

The fee and "good"-coin thresholds are **not in code** — they live in `config.json`:

```json
{
  "fees":    { "taker_pct": 0.04, "mexc_taker_pct": 0.02 },
  "filters": {
    "min_volume_usdt":    1000000,
    "max_spread_pct":     0.10,
    "min_volatility_pct": 2.0
  }
}
```

> `taker_pct` is the Binance futures taker fee; `mexc_taker_pct` is MEXC's (a placeholder
> default of 0.02% — set it to your actual MEXC tier). The same `filters` thresholds
> apply to both exchanges.

A coin is flagged **FILTER PASS** when **all** hold (otherwise **FILTER FAIL**):
- 24h quote volume ≥ `min_volume_usdt`
- bid/ask spread ≤ `max_spread_pct`
- 24h volatility (range) ≥ `min_volatility_pct`

Edit `config.json` and re-run the generator — no code change needed. Point at a
different file with `SCREENER_CONFIG=/path/to/config.json`. Any missing key falls
back to a built-in default, so the tool still runs if the file is absent.

## Momentum screener (CMC trending × Binance 1h/2h/4h)

`build_momentum.py` finds coins that are *trending and actually climbing* — not ones that
already pumped and are rolling over:

```bash
.venv/bin/python build_momentum.py   # writes momentum_ranking.json (+ snapshots under momentum/)
```

It (1) scrapes **CoinMarketCap's trending list** (the candidate set the market is watching),
(2) pulls **1h, 2h and 4h** candles from Binance for each coin (USDⓈ-M futures, falling back
to spot), and (3) scores momentum with a **strong weight on 1h**. The score also blends in
very-recent action: a small **recent bucket** (length-weighted 5/15/30/45m drift, `weights.recent`)
and a **5–15m acceleration** term (`accel_weight`) that rewards a freshening move and penalises a
fading one inside a confirmed uptrend. A coin is flagged **UPTREND** (green on `/momentum`) only
when it is a genuine advance, *not a post-pump top*:

- composite score ≥ `min_score`, and 1h is still rising;
- **not overextended** — 1h price ≤ `max_extension_pct` above its EMA (a blown-off top is rejected);
- **no single-bar spike** — the last 1h candle moved ≤ `max_single_bar_pct` (rejects one vertical candle);
- the **4h trend confirms** the move (`require_uptrend_alignment`) so a 1h blip alone doesn't qualify;
- **no recent dump** — rejected if the last 15m fell more than `max_recent_drop_pct` (catching a coin rolling over).

#### Early-detection (leading signals)

To catch a move *before* it's obvious, each coin is also checked for leading signals — shown on
`/momentum` as the **Buy% / RVOL / Early** columns, and contributing a small `early_weight` bonus
per signal that fires. **Early** flags confluence (`early_min_signals`+ firing):

- **BUY** — taker-buy share ≥ `buy_ratio_min` (aggressive demand, leads price);
- **VOL** — relative volume ≥ `rvol_min` (a surge often precedes the breakout);
- **ACC** — the 1h move is accelerating (2nd derivative ≥ `min_accel_pct`);
- **BRK** — price made a new `breakout_lookback`-bar high (Donchian breakout);
- **OI▲** — open interest rose ≥ `oi_min_pct` (new money entering — futures only);
- **F** — funding ≤ `funding_max` (not yet crowded-long, so room to run — futures only).

A coin can be **EARLY** without being **UPTREND** yet — that's the point: the leading signals fire
before the 1h/2h/4h composite fully confirms. Pulled from the same candles (taker-buy/volume are in
the kline payload) plus one bulk funding call and one open-interest call per coin.

A **market-regime banner** sits above the table — one dot line per reference coin
(`regime_coins`, default **BTC / ETH / HYPE / ZEC**) across the same windows (5/15/30/45m +
1h/2h/4h) with a Risk-on / Risk-off / Mixed label. It's **context only** (not a filter), so you can
read each coin's signal against the broader market.

### Momentum parameters — `config.json` (`"momentum"`)

All weights and thresholds are config, not code:

```json
"momentum": {
  "timeframes": ["1h", "2h", "4h"],
  "weights": { "1h": 0.5, "2h": 0.3, "4h": 0.2, "recent": 0.12 },
  "accel_weight": 0.2,
  "max_recent_drop_pct": 1.5,
  "roc_lookback_bars": 6,
  "ema_fast": 9, "ema_slow": 21, "slope_bars": 3,
  "klines_limit": 60,
  "trend_down_factor": 0.3, "damp_floor": 0.2,
  "max_extension_pct": 12.0,
  "max_single_bar_pct": 6.0,
  "min_score": 1.0,
  "require_uptrend_alignment": true,
  "spot_fallback": true,
  "candidate_limit": 30,
  "snapshot_keep": 300,
  "recent_windows_min": [5, 15, 30, 45],

  "buy_ratio_bars": 6, "buy_ratio_min": 0.55,
  "rvol_recent_bars": 3, "rvol_base_bars": 20, "rvol_min": 1.8,
  "accel_lookback": 6, "min_accel_pct": 0.2,
  "breakout_lookback": 24,
  "oi_hist_period": "5m", "oi_lookback_bars": 6, "oi_min_pct": 0.5,
  "funding_max": 0.0003,
  "early_min_signals": 2, "early_weight": 0.2
}
```

Bump the `1h` weight for a faster signal; lower `max_extension_pct` to be stricter about
chasing. Trending data comes from CMC's free public data-API by default; set
`SCREENER_CMC_API_KEY` to use the official Pro API instead. Refresh from `./menu.sh`
(option 6) or by re-running the script.

## Shorts screener (weakest MEXC / HL perps)

`build_shorts.py` is the short-side mirror of the momentum tab — it finds the **top perps to short**:
genuinely weak, still liquid, with **low reversal risk**.

```bash
.venv/bin/python build_shorts.py    # writes shorts_ranking.json
```

It (1) scans the **whole MEXC (~890) + Hyperliquid (~230) perp universe** in two bulk calls for the
weakest coins with decent 24h volume, (2) deep-scores a shortlist on **1h/2h/4h** (Binance futures when
listed — best data + native 2h + OI/funding — else MEXC klines), and (3) scores **weakness** (the inverse
of the momentum composite) plus **breakdown** leading signals (aggressive selling, volume surge, 1h
accelerating down, new-low breakdown, OI rising into weakness, funding not crowded). A coin is flagged
**SHORT** when it is strongly weak, 1h falling, and the **4h downtrend confirms**.

**Reversal risk (info + toggle).** Per the request, capitulating coins are *kept* by default but marked
with a **⚠️** icon (hover for the reasons): **oversold** (RSI / stretched far below the mean),
**crowded short** (deeply negative funding = squeeze risk), a **recent bounce**, or a **capitulation
candle**. A page toggle — *"Hide high reversal-risk"* — filters the high-risk ones out; with the table
paged at 10, it always shows the **top 10 of the current view**.

The same BTC/ETH/HYPE/ZEC regime banner sits on top, and it refreshes on the same 5-min cron. All
thresholds live in `config.json → "shorts"` (volume floor, weakness weights, breakdown thresholds, and
the reversal-risk thresholds: `rsi_oversold`, `funding_squeeze`, `bounce_pct`, `capitulation_bar_pct`, …).

> Note: MEXC klines carry no taker-buy data, so the **SELL** signal only applies to Binance-scored coins.

## Note on ports

The dashboard defaults to port **8000**. To run a second instance alongside it,
use a different port: `PORT=8001 ./start_screener.sh`.
