#!/usr/bin/env python3
"""
Rank the MEXC USDT-margined perpetual futures universe by trading cost & volatility.

Public API, no key required. One bulk ticker call provides bid/ask, 24h turnover,
and 24h range for every contract, so no per-symbol requests are needed.

  - spread %     = (ask-bid)/mid*100                 (from contract/ticker bid1/ask1)
  - total_cost % = spread % + round-trip taker fee
  - volatility % = 24h (high-low)/fairPrice*100
  - quote_volume = 24h turnover in USDT (amount24)

"Good" (tradeable) = volume, spread, and volatility all pass the config.json thresholds.
Writes mexc_ranking.json for the data server to display.
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
OUT_FILE = BASE / "mexc_ranking.json"

DETAIL_URL = "https://contract.mexc.com/api/v1/contract/detail"
TICKER_URL = "https://contract.mexc.com/api/v1/contract/ticker"
TIMEOUT = 12

# --- Tunable parameters: loaded from config.json, NOT hardcoded. ---
CONFIG_FILE = Path(os.environ.get("SCREENER_CONFIG", str(BASE / "config.json")))
_DEFAULTS = {
    "fees": {"mexc_taker_pct": 0.02},
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
TAKER_FEE = CFG["fees"].get("mexc_taker_pct", 0.02)   # MEXC futures taker fee (%) — set in config.json
ROUNDTRIP_TAKER = TAKER_FEE * 2
MIN_VOLUME = CFG["filters"]["min_volume_usdt"]
MAX_SPREAD_PCT = CFG["filters"]["max_spread_pct"]
MIN_VOLATILITY_PCT = CFG["filters"]["min_volatility_pct"]


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "screener-mexc-rank/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def main():
    # 1. Universe: USDT-quoted, USDT-settled perpetuals
    detail = get_json(DETAIL_URL).get("data", [])
    usdt = {c["symbol"] for c in detail
            if c.get("quoteCoin") == "USDT" and c.get("settleCoin") == "USDT"}

    # 2. Bulk ticker: bid/ask + 24h turnover + 24h range for all contracts (one call)
    ticks = get_json(TICKER_URL).get("data", [])

    pairlist = set()
    try:
        pairlist = {p.split("/")[0].upper() for p in json.load(open(PAIRS_FILE))["pairs"]}
    except Exception:
        pass

    rows = []
    for t in ticks:
        sym = t.get("symbol")
        if sym not in usdt:
            continue
        try:
            bid = float(t["bid1"]); ask = float(t["ask1"])
            high = float(t["high24Price"]); low = float(t["lower24Price"])
            amt = float(t["amount24"])  # 24h turnover in USDT
            fair = float(t.get("fairPrice") or t.get("lastPrice") or 0)
        except (KeyError, ValueError, TypeError):
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0
        denom = fair if fair > 0 else mid
        volatility_pct = (high - low) / denom * 100.0 if denom > 0 else None
        total_cost = spread_pct + ROUNDTRIP_TAKER
        good = (
            (amt >= MIN_VOLUME)
            and (0 < spread_pct <= MAX_SPREAD_PCT)
            and (volatility_pct is not None and volatility_pct >= MIN_VOLATILITY_PCT)
        )
        coin = sym.split("_")[0]
        rows.append({
            "coin": coin,
            "symbol": sym,
            "bid": bid,
            "ask": ask,
            "spread_pct": round(spread_pct, 5),
            "fee_roundtrip_pct": ROUNDTRIP_TAKER,
            "total_cost_pct": round(total_cost, 5),
            "volatility_pct": round(volatility_pct, 3) if volatility_pct is not None else None,
            "quote_volume": round(amt, 0),
            "good": good,
            "in_pairlist": coin in pairlist,
        })

    rows.sort(key=lambda r: r["total_cost_pct"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    out = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exchange": "mexc_futures",
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
          f"(vol>={MIN_VOLUME:,.0f} & spread<={MAX_SPREAD_PCT}% & vol>={MIN_VOLATILITY_PCT}%), "
          f"taker_fee={TAKER_FEE}%, generated {out['generated_utc']}")


if __name__ == "__main__":
    main()
