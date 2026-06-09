# SCREENER — User Manual

*A quick, practical guide to the two things people use SCREENER for most:*
***downloading pair data*** *and* ***getting LONG / SHORT trade ideas.***

> This manual is a living document — it will be expanded as the dashboard grows.
> Last updated: 2026-06-09.

---

## What SCREENER is

SCREENER is a web dashboard for crypto futures. It does three jobs:

1. **Serves market data** — per-coin OHLCV (price/volume) files you can download as CSV.
2. **Ranks the universe** — every Binance and MEXC perpetual, scored by trading cost (spread + fees) and volatility.
3. **Generates trade ideas** — a **LONG** list (coins in a real uptrend) and a **SHORT** list (weak, liquid perps to short), plus a **Results** scorecard that tracks whether past ideas won or lost.

Everything is behind a login. Once you're signed in, every page and download just works.

---

## 1 · How to download pairs (CSV)

The data lives as per-coin, per-timeframe files. The fastest way to grab one:

### From the dashboard

1. On the **home page**, open **CSV Download** (the `/summary` page).
2. You'll see a table of every available file — one row per **coin × timeframe** (e.g. `BTC 1h`, `ETH 15m`, `SOL 1d`), with its date range and how fresh it is.
3. To narrow to a single coin, use the **"Jump to a single coin"** dropdown on the home page, or add `?showmeasset=BTC` to the `/summary` URL.
4. Click a row's **CSV** link to download that pair as a `.csv` (converted on the fly), or the **feather** link for the raw binary file.

### What you get

A standard OHLCV CSV — one row per candle:

| column | meaning |
|--------|---------|
| `open_time` | candle open timestamp (UTC) |
| `open` / `high` / `low` / `close` | prices |
| `volume` | base-asset volume |
| *(plus mark price / funding columns where available)* |

Open it in Excel, pandas, TradingView, or feed it straight into a backtester.

### Which pairs are "good" to trade?

Downloading everything isn't the point — you want **liquid, cheap-to-trade** coins. Two ranking pages do that filtering for you:

- **Full Binance Ranking** (`/binance-ranking`) — every Binance perpetual, **green = tradeable** (passes the volume / spread / volatility filters).
- **MEXC Ranking** (`/mexc-ranking`) — the same for MEXC (public API, no key needed).
- **Combined** (`/combined`) — a *trade-on-MEXC, backtest-on-Binance* view, with the Binance CSV links right beside each pick.

A coin is flagged **FILTER PASS** only when **all** of these hold: 24h volume above the floor, spread below the cap, and volatility above the minimum. Those thresholds are tunable (`config.json`).

> **Tip — get the shortlist as JSON:** `/binance-good-pairs.json` and `/mexc-good-pairs.json` return just the "good" coins as a plain pair list — handy for scripting a bulk download.

---

## 2 · How to get LONG recommendations

Open **LONG ideas** (`/momentum`) from the home page.

This list answers: *which trending coins are actually climbing right now — not the ones that already pumped and are rolling over?*

### How it picks

1. Starts from **CoinMarketCap's trending list** — the coins the market is already watching.
2. Pulls **1h / 2h / 4h** candles for each from Binance and scores momentum, with the **heaviest weight on the 1h** move.
3. Flags a coin **UPTREND** (green) only when it's a *genuine advance, not a blow-off top*:
   - the composite score clears the bar **and the 1h is still rising**;
   - **not overextended** (price isn't stretched far above its moving average);
   - **no single vertical candle** doing all the work;
   - the **4h trend confirms** the move;
   - **no recent dump** in the last 15 minutes.

The page lists only **confirmed UPTRENDs** — coins that fail the checks are hidden.

### Reading the early-warning columns

To catch a move *before* it's obvious, each coin also shows leading signals — **Buy% / RVOL / Early**:

| flag | meaning |
|------|---------|
| **BUY** | aggressive taker-buying (demand leading price) |
| **VOL** | relative volume surging |
| **ACC** | the 1h move is accelerating |
| **BRK** | new breakout high |
| **OI▲** | open interest rising (new money in) |
| **F** | funding still low (not yet a crowded long) |

A coin can be **EARLY** before it's a full **UPTREND** — that's the point: the early signals fire first. The **regime banner** at the top (BTC / ETH / HYPE / ZEC across several windows) is context only — read each idea against whether the broader market is risk-on or risk-off.

---

## 3 · How to get SHORT recommendations

Open **SHORT ideas** (`/shorts`) from the home page. This is the mirror image of the LONG list.

It answers: *which perps are genuinely weak, still liquid, and unlikely to snap back?*

### How it picks

1. Scans the **whole MEXC + Hyperliquid perp universe** for the weakest coins that still have real volume.
2. Deep-scores a shortlist on **1h / 2h / 4h** (using Binance data when the coin is listed there — better data plus open-interest and funding).
3. Flags a coin **SHORT** when it's strongly weak, the **1h is falling**, and the **4h downtrend confirms** — plus breakdown signals (aggressive selling, volume surge, new lows, OI building into the weakness).

### The ⚠️ reversal-risk flag

A falling coin can still bounce hard. Capitulating coins are **kept** but marked with a **⚠️** — hover it for the reason:

- **oversold** (stretched far below the mean);
- **crowded short** (deeply negative funding → squeeze risk);
- a **recent bounce**; or
- a **capitulation candle**.

Use the **"Hide high reversal-risk"** toggle to filter those out and show only the cleaner shorts.

---

## 4 · Did the ideas win or lose? — the Results page

Open **Results** (`/results`). This is the honesty check: every LONG and SHORT idea is tracked from its **entry price** against the **live price**, with hit-rate, open + settled positions, and equity curves.

Each side has a **hard stop-loss** built into the evaluation so a single runaway trade can't distort the track record (the long and short stops are tuned separately).

---

## Quick reference — where each thing lives

| I want to… | Go to |
|------------|-------|
| Download a pair as CSV | **CSV Download** (`/summary`) |
| See only tradeable (liquid, cheap) coins | **Binance / MEXC Ranking** |
| Pick on MEXC, backtest on Binance | **Combined** (`/combined`) |
| Find coins to **buy** (uptrends) | **LONG ideas** (`/momentum`) |
| Find coins to **short** (weak perps) | **SHORT ideas** (`/shorts`) |
| Check if past ideas were right | **Results** (`/results`) |
| Machine-readable version of any page | add `.json` (e.g. `/momentum.json`) |

---

## Notes & caveats

- **Rankings are snapshots.** The LONG and SHORT lists refresh on a schedule; what you see is the most recent run.
- **Ideas are signals, not advice.** Always cross-check against the regime banner and your own risk limits.
- **Data coverage varies by exchange.** MEXC klines carry no taker-buy data, so the **SELL/BUY** signal only shows for Binance-scored coins.

*Questions or something unclear? This manual will keep growing — check back for updates.*
