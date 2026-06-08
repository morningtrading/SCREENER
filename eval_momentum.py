#!/usr/bin/env python3
"""
Evaluate past MOMENTUM (long) picks vs the actual price — "were we right?".

Reads momentum_history/momentum_picks.jsonl (written by build_momentum.py every run) and, for
each coin we flagged as momentum, compares the ENTRY price (first time we flagged it) to the
LIVE price. Long P&L is positive when the price rose (the call was right). Prints a per-coin
table and an overall hit-rate.

Usage:
  python3 eval_momentum.py                  # evaluate every distinct pick
  python3 eval_momentum.py --min-age-hours 4    # only picks that have had >=4h to play out
"""
import json
import argparse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import build_momentum as bm

BASE = Path(__file__).resolve().parent
PICKS = BASE / "momentum_history" / "momentum_picks.jsonl"
FAPI = "https://fapi.binance.com/fapi/v1"
MEXC_TICKER = "https://contract.mexc.com/api/v1/contract/ticker"


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


_hl_mids = None


def hl_mids():
    global _hl_mids
    if _hl_mids is None:
        try:
            body = json.dumps({"type": "allMids"}).encode()
            req = urllib.request.Request(bm.HL_INFO, data=body,
                                         headers={"User-Agent": bm.UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=bm.TIMEOUT) as r:
                _hl_mids = json.loads(r.read().decode())
        except Exception:
            _hl_mids = {}
    return _hl_mids


def current_price(coin):
    for getter in (
        lambda: float(bm.get_json(f"{FAPI}/ticker/price?symbol={coin}USDT")["price"]),
        lambda: float(bm.get_json(f"{MEXC_TICKER}?symbol={coin}_USDT")["data"]["lastPrice"]),
        lambda: float(hl_mids()[coin]),
    ):
        try:
            return getter()
        except Exception:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-age-hours", type=float, default=0.0,
                    help="only evaluate picks at least this many hours old")
    args = ap.parse_args()
    if not PICKS.exists():
        print(f"No picks history yet at {PICKS}")
        return

    first = {}
    for line in PICKS.read_text().splitlines():
        if not line.strip():
            continue
        p = json.loads(line)
        c = p["coin"]
        if c not in first or p["ts"] < first[c]["ts"]:
            first[c] = p

    now = datetime.now(timezone.utc)
    results = []
    for c, p in first.items():
        if not p.get("entry_price"):
            continue
        age_h = (now - parse_ts(p["ts"])).total_seconds() / 3600.0
        if age_h < args.min_age_hours:
            continue
        cur = current_price(c)
        if not cur:
            continue
        ret = (cur / p["entry_price"] - 1.0) * 100.0   # long P&L: positive when price rose
        results.append((c, p, age_h, cur, ret))

    if not results:
        print("No evaluable picks yet (try a smaller --min-age-hours).")
        return

    results.sort(key=lambda x: x[4], reverse=True)
    print(f"{'coin':<10}{'age(h)':>7}{'entry':>13}{'now':>13}{'long P&L%':>11}  early")
    for c, p, age, cur, ret in results:
        print(f"{c:<10}{age:>7.1f}{p['entry_price']:>13.6g}{cur:>13.6g}{ret:>+11.2f}  {'Y' if p.get('early') else ''}")
    wins = sum(1 for r in results if r[4] > 0)
    avg = sum(r[4] for r in results) / len(results)
    print(f"\n{len(results)} picks evaluated · right (price rose) {wins}/{len(results)} "
          f"= {wins / len(results) * 100:.0f}% · avg long P&L {avg:+.2f}%")


if __name__ == "__main__":
    main()
