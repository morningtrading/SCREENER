#!/usr/bin/env python3
"""
Rank the FULL Binance USDⓈ-M futures universe (all PERPETUAL *USDT symbols),
not just the local pairlist.

For every symbol:
  - spread %        = (ask-bid)/mid*100        (from bulk bookTicker)
  - total_cost %    = spread % + round-trip taker fee (0.08%)
  - volatility %    = 24h (high-low)/weightedAvgPrice*100   (range-based vol index)
  - quote_volume    = 24h USDT volume

"Good" (tradeable) = volume, spread, and volatility all pass the config.json thresholds.
Writes binance_ranking.json for the data server to display.
"""
import os
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
# Locations come from env vars / a local .env; defaults are relative to this folder.
PROJECT_ROOT = Path(os.environ.get("SCREENER_PROJECT_ROOT", str(BASE)))
PAIRS_FILE = Path(os.environ.get("SCREENER_PAIRS_FILE", str(PROJECT_ROOT / "pairs.json")))
OUT_FILE = BASE / "binance_ranking.json"

FAPI = "https://fapi.binance.com/fapi/v1"
TIMEOUT = 10

# --- Tunable parameters: loaded from config.json, NOT hardcoded. ---
# Override the config path with SCREENER_CONFIG. Any missing key falls back to
# the defaults below, so the tool still runs if config.json is absent.
CONFIG_FILE = Path(os.environ.get("SCREENER_CONFIG", str(BASE / "config.json")))
_DEFAULTS = {
    "fees": {"taker_pct": 0.04},
    "filters": {"min_volume_usdt": 1_000_000, "max_spread_pct": 0.10, "min_volatility_pct": 2.0},
}


def load_config():
    cfg = {section: dict(values) for section, values in _DEFAULTS.items()}
    try:
        with open(CONFIG_FILE) as fh:
            user = json.load(fh)
        for section in cfg:
            cfg[section].update(user.get(section, {}))
    except FileNotFoundError:
        pass
    return cfg


CFG = load_config()
TAKER_FEE = CFG["fees"]["taker_pct"]            # Binance USDⓈ-M futures taker fee (%)
ROUNDTRIP_TAKER = TAKER_FEE * 2
MIN_VOLUME = CFG["filters"]["min_volume_usdt"]        # 24h quote (USDT) volume floor
MAX_SPREAD_PCT = CFG["filters"]["max_spread_pct"]     # spread must be tighter than this
MIN_VOLATILITY_PCT = CFG["filters"]["min_volatility_pct"]  # 24h range floor (filters stablecoins/gold)


def get_json(path):
    req = urllib.request.Request(f"{FAPI}/{path}", headers={"User-Agent": "screener-binance-rank/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def main():
    # 1. Universe: PERPETUAL, USDT-quoted, TRADING
    info = get_json("exchangeInfo")
    perp = {
        s["symbol"]: s["baseAsset"]
        for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    }

    # 2. Bulk best bid/ask (one call for all symbols)
    book = {b["symbol"]: b for b in get_json("ticker/bookTicker") if b.get("symbol") in perp}

    # 3. Bulk 24h stats (volume + range) (one call for all symbols)
    stats = {t["symbol"]: t for t in get_json("ticker/24hr") if t.get("symbol") in perp}

    pairlist = set()
    try:
        pairlist = {p.split("/")[0].upper() for p in json.load(open(PAIRS_FILE))["pairs"]}
    except Exception:
        pass

    rows = []
    for symbol, base in perp.items():
        b = book.get(symbol)
        t = stats.get(symbol)
        if not b or not t:
            continue
        try:
            bid = float(b["bidPrice"]); ask = float(b["askPrice"])
            wap = float(t["weightedAvgPrice"]); high = float(t["highPrice"])
            low = float(t["lowPrice"]); qvol = float(t["quoteVolume"])
        except (KeyError, ValueError, TypeError):
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0
        volatility_pct = (high - low) / wap * 100.0 if wap > 0 else None
        total_cost = spread_pct + ROUNDTRIP_TAKER
        good = (
            (qvol >= MIN_VOLUME)
            and (0 < spread_pct <= MAX_SPREAD_PCT)
            and (volatility_pct is not None and volatility_pct >= MIN_VOLATILITY_PCT)
        )
        rows.append({
            "coin": base,
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "spread_pct": round(spread_pct, 5),
            "fee_roundtrip_pct": ROUNDTRIP_TAKER,
            "total_cost_pct": round(total_cost, 5),
            "volatility_pct": round(volatility_pct, 3) if volatility_pct is not None else None,
            "quote_volume": round(qvol, 0),
            "good": good,
            "in_pairlist": base in pairlist,
        })

    rows.sort(key=lambda r: r["total_cost_pct"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    out = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exchange": "binance_futures",
        "fees": {"taker_pct": TAKER_FEE, "roundtrip_taker_pct": ROUNDTRIP_TAKER},
        "min_volume": MIN_VOLUME,
        "max_spread_pct": MAX_SPREAD_PCT,
        "min_volatility_pct": MIN_VOLATILITY_PCT,
        "total_symbols": len(rows),
        "count_good": sum(1 for r in rows if r["good"]),
        "rows": rows,
    }
    json.dump(out, open(OUT_FILE, "w"), indent=2)
    print(f"Wrote {OUT_FILE}: {len(rows)} symbols, {out['count_good']} good "
          f"(vol>={MIN_VOLUME:,.0f} & spread<={MAX_SPREAD_PCT}%), generated {out['generated_utc']}")
    good_not_tracked = [r["coin"] for r in rows if r["good"] and not r["in_pairlist"]]
    print(f"Good coins NOT in current pairlist ({len(good_not_tracked)}): {', '.join(good_not_tracked[:30])}")


if __name__ == "__main__":
    main()
