#!/usr/bin/env python3
"""
Score past picks vs price and write eval_results.json for the /results page.

For each coin's FIRST call (the entry), this pulls the price path since the call (15m candles
from the trigger time) to compute:
  - per-pick P&L + a P&L-since-call series (the row sparkline)
  - per-side EQUITY curve: the average P&L across all positions open at each time
    (long P&L = price up, short P&L = price down).

Exit policy (so stale calls don't linger as "open" forever): each pick is an OPEN position
only while the screener keeps re-flagging it. It SETTLES — P&L frozen at the close price — at
the earlier of (a) the horizon (`eval.horizon_hours` after entry) or (b) momentum flipping off
(no re-flag within `eval.flip_grace_min`). Open picks track to the live price; settled picks
are frozen and reported separately. Run on the 5-min cron after build_momentum/build_shorts.
"""
import os
import json
import urllib.request
from datetime import datetime, timedelta, timezone
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

# Exit policy — tunable via config.json "eval" (NOT hardcoded; same merge style as the ranker).
CONFIG_FILE = Path(os.environ.get("SCREENER_CONFIG", str(BASE / "config.json")))
_EVAL_DEFAULTS = {"horizon_hours": 4.0, "flip_grace_min": 15.0}


def load_eval_config():
    cfg = dict(_EVAL_DEFAULTS)
    try:
        with open(CONFIG_FILE) as fh:
            cfg.update(json.load(fh).get("eval", {}))
    except (FileNotFoundError, ValueError):
        pass
    return cfg


ECFG = load_eval_config()


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


def coin_picks(path):
    """coin -> all its pick snapshots, sorted oldest-first."""
    by_coin = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            p = json.loads(line)
            by_coin.setdefault(p["coin"], []).append(p)
    for picks in by_coin.values():
        picks.sort(key=lambda p: p["ts"])
    return by_coin


def position_close(picks, now, horizon_h, grace_s):
    """When (and why) this position closes — (close_dt | None, reason).

    A pick stays OPEN while the screener keeps re-flagging it (each re-flag within `grace_s`
    of the previous one extends the open episode). It closes at the EARLIER of:
      - "flip":    the screener stopped re-flagging it (last flag + grace has passed), or
      - "horizon": `horizon_h` hours after entry.
    close_dt None => still open ("open").
    """
    entry_dt = parse_ts(picks[0]["ts"])
    ep_end = entry_dt                       # end of the first contiguous flagged episode
    for p in picks[1:]:
        t = parse_ts(p["ts"])
        if (t - ep_end).total_seconds() <= grace_s:
            ep_end = t
        else:
            break                            # a gap > grace: episode ended at ep_end
    horizon_close = entry_dt + timedelta(hours=horizon_h)
    cands = []
    if (now - ep_end).total_seconds() > grace_s:     # no longer being re-flagged
        cands.append((ep_end, "flip"))
    if now >= horizon_close:
        cands.append((horizon_close, "horizon"))
    if not cands:
        return None, "open"
    return min(cands, key=lambda c: c[0])


def pnl_of(entry, price, side):
    return (price / entry - 1.0) * 100.0 if side == "long" else (entry / price - 1.0) * 100.0


def evaluate(path, side, now, horizon_h, grace_s):
    open_rows, settled_rows, series_by_coin = [], [], {}
    for c, picks in coin_picks(path).items():
        entry = picks[0]
        ep = entry.get("entry_price")
        if not ep:
            continue
        src = "mexc" if entry.get("data_src") == "mexc" else "binance"
        entry_dt = parse_ts(entry["ts"])
        start_ms = int(entry_dt.timestamp() * 1000)
        closes = kline_closes(c, src, start_ms)
        if not closes:
            continue
        close_dt, reason = position_close(picks, now, horizon_h, grace_s)
        close_ms = int(close_dt.timestamp() * 1000) if close_dt else None
        # Settled pick: freeze the path (and the exit price) at the close time.
        kept = [(t, px) for t, px in closes if close_ms is None or t <= close_ms] or closes[:1]
        # P&L-since-call path (signed by side: rising = the position is winning)
        path_pnl = [(t, round(pnl_of(ep, px, side), 3)) for t, px in kept if px > 0]
        if not path_pnl:
            continue
        exit_price = kept[-1][1]
        pnl = round(pnl_of(ep, exit_price, side), 2)
        age = (now - entry_dt).total_seconds() / 3600.0
        held = ((close_dt or now) - entry_dt).total_seconds() / 3600.0
        # downsample for the row sparkline
        vals = [v for _, v in path_pnl]
        if len(vals) > SPARK_POINTS:
            step = len(vals) / SPARK_POINTS
            vals = [vals[int(i * step)] for i in range(SPARK_POINTS)] + [vals[-1]]
        row = {
            "coin": c, "ts": entry["ts"], "age_hours": round(age, 1),
            "held_hours": round(held, 1), "entry": ep, "now": exit_price,
            "pnl": pnl, "spark": vals, "closed": close_dt is not None, "close_reason": reason,
            "extra": ("early" if entry.get("early") else "") if side == "long" else entry.get("reversal_risk", "none"),
        }
        (settled_rows if close_dt is not None else open_rows).append(row)
        series_by_coin[c] = {"start": start_ms, "close_ms": close_ms, "path": path_pnl}

    def summarize(rows, key):
        rows = sorted(rows, key=key, reverse=True)
        n = len(rows)
        wins = sum(1 for r in rows if r["pnl"] > 0)
        avg = round(sum(r["pnl"] for r in rows) / n, 2) if n else 0.0
        return {"count": n, "wins": wins, "avg": avg, "rows": rows}

    openg = summarize(open_rows, lambda r: r["pnl"])          # open board: best P&L first
    settledg = summarize(settled_rows, lambda r: r["ts"])     # track record: most recent first

    # Equity curve: average P&L across positions OPEN at each 15m timestamp — a position
    # drops out of the average once it settles (so a closed loser stops dragging the curve).
    equity = []
    times = sorted({t for s in series_by_coin.values() for t, _ in s["path"]})
    filled = {}
    for c, s in series_by_coin.items():
        d = dict(s["path"])
        last, col = None, {}
        for t in times:
            if t in d:
                last = d[t]
            live = t >= s["start"] and (s["close_ms"] is None or t <= s["close_ms"])
            col[t] = last if live else None
        filled[c] = col
    for t in times:
        vals = [filled[c][t] for c in filled if filled[c][t] is not None]
        if vals:
            equity.append({"t": t, "eq": round(sum(vals) / len(vals), 2)})

    # Open-position stats sit at the top level (the active board); settled is its own group.
    return {
        "count": openg["count"], "wins": openg["wins"], "avg": openg["avg"], "rows": openg["rows"],
        "settled": settledg, "equity": equity,
    }


def main():
    now = datetime.now(timezone.utc)
    h, g = ECFG["horizon_hours"], ECFG["flip_grace_min"] * 60.0
    out = {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "horizon_hours": h,
        "longs": evaluate(LONGS, "long", now, h, g),
        "shorts": evaluate(SHORTS, "short", now, h, g),
    }
    OUT_FILE.write_text(json.dumps(out, indent=2))
    L, S = out["longs"], out["shorts"]
    print(f"Wrote {OUT_FILE}: horizon {h}h · "
          f"longs open {L['wins']}/{L['count']} (avg {L['avg']:+}%), settled {L['settled']['wins']}/{L['settled']['count']} · "
          f"shorts open {S['wins']}/{S['count']} (avg {S['avg']:+}%), settled {S['settled']['wins']}/{S['settled']['count']} · "
          f"eq pts {len(L['equity'])}/{len(S['equity'])}")


if __name__ == "__main__":
    main()
