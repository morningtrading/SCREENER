#!/usr/bin/env python3
"""
Score past picks vs the live price and write eval_results.json for the /results page.

For each coin's FIRST call (the entry), this pulls the price path since the call (15m candles
from the trigger time) to compute:
  - per-pick P&L now + a P&L-since-call series (the row sparkline)
  - per-side EQUITY curve: the average P&L across all positions open at each time
    (long P&L = price up, short P&L = price down).
Run on the 5-min cron after build_momentum.py / build_shorts.py.
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
MEXC_KLINE = "https://contract.mexc.com/api/v1/contract/kline"
SPARK_POINTS = 48          # max points kept per row sparkline


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def kline_closes(coin, src, start_ms):
    """[(openTime_ms, close), ...] of 15m candles from start_ms — Binance or MEXC."""
    if src == "mexc":
        try:
            d = bm.get_json(f"{MEXC_KLINE}/{coin}_USDT?interval=Min15&start={start_ms // 1000}").get("data", {})
            return [(int(t) * 1000, float(c)) for t, c in zip(d.get("time", []), d.get("close", []))]
        except Exception:
            return []
    try:
        raw = bm.get_json(f"{FAPI}/klines?symbol={coin}USDT&interval=15m&startTime={start_ms}&limit=500")
        return [(int(k[0]), float(k[4])) for k in raw]
    except Exception:
        return []


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


def pnl_of(entry, price, side):
    return (price / entry - 1.0) * 100.0 if side == "long" else (entry / price - 1.0) * 100.0


def evaluate(path, side, now):
    rows, series_by_coin = [], {}
    for c, p in first_picks(path).items():
        entry = p.get("entry_price")
        if not entry:
            continue
        src = "mexc" if p.get("data_src") == "mexc" else "binance"
        start_ms = int(parse_ts(p["ts"]).timestamp() * 1000)
        closes = kline_closes(c, src, start_ms)
        if not closes:
            continue
        # P&L-since-call path (signed by side: rising = the position is winning)
        path_pnl = [(t, round(pnl_of(entry, px, side), 3)) for t, px in closes if px > 0]
        if not path_pnl:
            continue
        now_price = closes[-1][1]
        pnl_now = round(pnl_of(entry, now_price, side), 2)
        age = (now - parse_ts(p["ts"])).total_seconds() / 3600.0
        # downsample for the row sparkline
        vals = [v for _, v in path_pnl]
        if len(vals) > SPARK_POINTS:
            step = len(vals) / SPARK_POINTS
            vals = [vals[int(i * step)] for i in range(SPARK_POINTS)] + [vals[-1]]
        rows.append({
            "coin": c, "ts": p["ts"], "age_hours": round(age, 1),
            "entry": entry, "now": now_price, "pnl": pnl_now, "spark": vals,
            "extra": ("early" if p.get("early") else "") if side == "long" else p.get("reversal_risk", "none"),
        })
        series_by_coin[c] = {"start": start_ms, "path": path_pnl}

    rows.sort(key=lambda r: r["pnl"], reverse=True)
    wins = sum(1 for r in rows if r["pnl"] > 0)
    avg = round(sum(r["pnl"] for r in rows) / len(rows), 2) if rows else 0.0

    # Equity curve: average P&L across all positions open at each 15m timestamp.
    equity = []
    times = sorted({t for s in series_by_coin.values() for t, _ in s["path"]})
    filled = {}
    for c, s in series_by_coin.items():
        d = dict(s["path"])
        last, col = None, {}
        for t in times:
            if t in d:
                last = d[t]
            col[t] = last if t >= s["start"] else None
        filled[c] = col
    for t in times:
        vals = [filled[c][t] for c in filled if filled[c][t] is not None]
        if vals:
            equity.append({"t": t, "eq": round(sum(vals) / len(vals), 2)})

    return {"count": len(rows), "wins": wins, "avg": avg, "rows": rows, "equity": equity}


def main():
    now = datetime.now(timezone.utc)
    out = {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "longs": evaluate(LONGS, "long", now),
        "shorts": evaluate(SHORTS, "short", now),
    }
    OUT_FILE.write_text(json.dumps(out, indent=2))
    L, S = out["longs"], out["shorts"]
    print(f"Wrote {OUT_FILE}: longs {L['wins']}/{L['count']} (avg {L['avg']:+}%, eq pts {len(L['equity'])}), "
          f"shorts {S['wins']}/{S['count']} (avg {S['avg']:+}%, eq pts {len(S['equity'])})")


if __name__ == "__main__":
    main()
