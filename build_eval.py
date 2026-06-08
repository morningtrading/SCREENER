#!/usr/bin/env python3
"""
Score past picks vs the live price and write eval_results.json for the /results page.

Reads both pick histories (momentum_history/ and shorts_history/), takes each coin's FIRST
call as the entry, fetches the current price, and computes P&L:
  long  P&L = now/entry - 1   (positive when price rose  -> momentum call was right)
  short P&L = entry/now - 1   (positive when price fell   -> short call was right)
Run on the 5-min cron (after build_momentum.py / build_shorts.py).
"""
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import build_momentum as bm

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "eval_results.json"
LONGS = BASE / "momentum_history" / "momentum_picks.jsonl"
SHORTS = BASE / "shorts_history" / "short_picks.jsonl"
FAPI = "https://fapi.binance.com/fapi/v1"
MEXC_TICKER = "https://contract.mexc.com/api/v1/contract/ticker"

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


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def first_picks(path):
    first = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            p = json.loads(line)
            c = p["coin"]
            if c not in first or p["ts"] < first[c]["ts"]:
                first[c] = p
    return first


def evaluate(path, side, now):
    rows = []
    for c, p in first_picks(path).items():
        entry = p.get("entry_price")
        if not entry:
            continue
        cur = current_price(c)
        if not cur:
            continue
        age = (now - parse_ts(p["ts"])).total_seconds() / 3600.0
        pnl = (cur / entry - 1.0) * 100.0 if side == "long" else (entry / cur - 1.0) * 100.0
        rows.append({
            "coin": c, "ts": p["ts"], "age_hours": round(age, 1),
            "entry": entry, "now": cur, "pnl": round(pnl, 2),
            "extra": ("early" if p.get("early") else "") if side == "long" else p.get("reversal_risk", "none"),
        })
    rows.sort(key=lambda r: r["pnl"], reverse=True)
    wins = sum(1 for r in rows if r["pnl"] > 0)
    avg = round(sum(r["pnl"] for r in rows) / len(rows), 2) if rows else 0.0
    return {"count": len(rows), "wins": wins, "avg": avg, "rows": rows}


def main():
    now = datetime.now(timezone.utc)
    out = {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "longs": evaluate(LONGS, "long", now),
        "shorts": evaluate(SHORTS, "short", now),
    }
    OUT_FILE.write_text(json.dumps(out, indent=2))
    L, S = out["longs"], out["shorts"]
    print(f"Wrote {OUT_FILE}: longs {L['wins']}/{L['count']} right (avg {L['avg']:+}%), "
          f"shorts {S['wins']}/{S['count']} right (avg {S['avg']:+}%)")


if __name__ == "__main__":
    main()
