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
# stop_loss_pct: hard stop in % P&L (e.g. -2.0). May be null (no stop), a single number applied
# to both sides, or per-side {"long": -2.0, "short": -8.0}. Longs and shorts behave very
# differently — a tight stop helps longs but stops out short bounces before they resolve — so
# the per-side form is the intended one.
CONFIG_FILE = Path(os.environ.get("SCREENER_CONFIG", str(BASE / "config.json")))
_EVAL_DEFAULTS = {
    "horizon_hours": 4.0,
    "flip_grace_min": 15.0,
    "stop_loss_pct": None,
    # Tighter hard stop (% P&L) for SHORT picks that carried reversal risk at the call
    # (reversal_risk != "none"). Those names squeeze — the analysis showed they hold every
    # -8% blow-up while clean shorts are net positive. Null => no special handling (flagged
    # shorts use the normal short stop). Applies only to shorts.
    "reversal_risk_stop_pct": None,
    # Drop reversal-risk-flagged SHORT picks from the track record entirely (models simply
    # not trading them). The flagged bucket is net-negative even with a tight stop, so
    # exclusion is the higher-value policy. Applies only to shorts.
    "exclude_reversal_risk": False,
    # Whether a position closes when the screener stops re-flagging it ("flip"). When false for
    # a side, the position is held to the horizon (or stop) regardless of flip — the short-side
    # winners are horizon-held while flip exits net ~0%, so letting shorts run captures more of
    # the downtrend. Bool (both sides) or per-side {"long": .., "short": ..}.
    "exit_on_flip": True,
    # Minimum time (minutes) a position must be held before a flip exit is allowed. Flip exits
    # within the first N minutes are deferred to entry+N — trades held under 30 min flip out at
    # 34% win rate vs 64-72% for 30m+, so even a deferred flip exit is better than an early one.
    # Null / 0 = no minimum (current behaviour). Per-side dict also accepted.
    "flip_min_hold_min": 0,
    # Take-profit level (% P&L). Fills intra-candle on the FAVORABLE wick (the high for a long,
    # the low for a short), the mirror of the stop. Longs spike fast then fade, so a TP banks the
    # move before flip/horizon hands it back. Null (no TP), a single number (both sides), or
    # per-side {"long": .., "short": ..}. Shorts intentionally have no TP (let winners run).
    "take_profit_pct": None,
}


def load_eval_config():
    cfg = dict(_EVAL_DEFAULTS)
    try:
        with open(CONFIG_FILE) as fh:
            cfg.update(json.load(fh).get("eval", {}))
    except (FileNotFoundError, ValueError):
        pass
    return cfg


ECFG = load_eval_config()


def sl_for(side):
    """Hard-stop level (% P&L) for a side, or None if no stop. Accepts a single number
    (both sides) or a per-side {"long": .., "short": ..} mapping in eval.stop_loss_pct."""
    v = ECFG.get("stop_loss_pct")
    return v.get(side) if isinstance(v, dict) else v


def effective_sl(side, entry):
    """Per-pick hard stop. Same as sl_for(side), except a reversal-risk-flagged SHORT uses the
    tighter `reversal_risk_stop_pct` when configured (the nearer-zero of the two stops)."""
    base = sl_for(side)
    if side == "short" and entry.get("reversal_risk", "none") != "none":
        rr = ECFG.get("reversal_risk_stop_pct")
        if rr is not None:
            return rr if base is None else max(base, rr)   # both negative; max = nearer 0 = tighter
    return base


def flip_exit_for(side):
    """Whether `side` closes a position when the screener stops re-flagging it. Accepts a bool
    (both sides) or a per-side {"long": .., "short": ..} mapping in eval.exit_on_flip."""
    v = ECFG.get("exit_on_flip")
    return v.get(side, True) if isinstance(v, dict) else v


def flip_min_hold_for(side):
    """Minimum hold time (seconds) before a flip exit is allowed for `side`. Accepts a number
    (both sides) or a per-side {"long": .., "short": ..} mapping in eval.flip_min_hold_min."""
    v = ECFG.get("flip_min_hold_min") or 0
    mins = v.get(side, 0) if isinstance(v, dict) else v
    return (mins or 0) * 60.0


def tp_for(side):
    """Take-profit level (% P&L) for a side, or None. Accepts a single number (both sides) or a
    per-side {"long": .., "short": ..} mapping in eval.take_profit_pct."""
    v = ECFG.get("take_profit_pct")
    return v.get(side) if isinstance(v, dict) else v


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def kline_ohlc(coin, src, start_ms):
    """[(openTime_ms, high, low, close), ...] of 15m candles from start_ms — Binance or MEXC.

    High/low are carried so the hard stop-loss can trigger intra-candle (on the adverse wick),
    the way a real stop fills, rather than only when the candle closes through the level.
    """
    if src == "mexc":
        try:
            d = bm.get_json(f"{MEXC_KLINE}/{coin}_USDT?interval=Min15&start={start_ms // 1000}").get("data", {})
            return [(int(t) * 1000, float(h), float(lo), float(c))
                    for t, h, lo, c in zip(d.get("time", []), d.get("high", []), d.get("low", []), d.get("close", []))]
        except Exception:
            return []
    try:
        raw = bm.get_json(f"{FAPI}/klines?symbol={coin}USDT&interval=15m&startTime={start_ms}&limit=500")
        return [(int(k[0]), float(k[2]), float(k[3]), float(k[4])) for k in raw]
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


def split_episodes(picks, grace_s):
    """Split a coin's picks into contiguous flagged episodes (each = a separate position).

    A coin can be flagged, drop off, then re-trigger as a fresh signal later. Each run of
    flags spaced <= `grace_s` apart is one episode; a gap longer than that starts a new one.
    Returns a list of pick-lists, oldest first (the LAST one is the current position). This is
    why a coin flagged right now shows as OPEN even if it had an earlier, already-closed run.
    """
    eps, cur = [], [picks[0]]
    for p in picks[1:]:
        if (parse_ts(p["ts"]) - parse_ts(cur[-1]["ts"])).total_seconds() <= grace_s:
            cur.append(p)
        else:
            eps.append(cur)
            cur = [p]
    eps.append(cur)
    return eps


def episode_close(ep, now, horizon_h, grace_s, flip_exit=True, min_hold_s=0.0):
    """When (and why) one episode closes — (close_dt | None, reason).

    The episode is OPEN while the screener keeps re-flagging it. It closes at the EARLIER of:
      - "flip":    the screener stopped re-flagging it (last flag + grace has passed), or
      - "horizon": `horizon_h` hours after the episode's entry.
    When `flip_exit` is False the flip path is disabled — the position is held to the horizon
    (or stop) regardless of whether the screener keeps re-flagging it.
    When `min_hold_s` > 0, a flip exit cannot occur before entry + min_hold_s: if the last
    re-flag falls inside the minimum hold window the flip close is deferred to entry+min_hold_s
    (priced at that candle rather than the early-exit candle).
    close_dt None => still open ("open").
    """
    entry_dt = parse_ts(ep[0]["ts"])
    ep_end = parse_ts(ep[-1]["ts"])
    horizon_close = entry_dt + timedelta(hours=horizon_h)
    cands = []
    if flip_exit and (now - ep_end).total_seconds() > grace_s:     # no longer being re-flagged
        # Defer the flip close to entry+min_hold_s if the last flag fell inside that window.
        flip_close = max(ep_end, entry_dt + timedelta(seconds=min_hold_s))
        if now >= flip_close:
            cands.append((flip_close, "flip"))
    if now >= horizon_close:
        cands.append((horizon_close, "horizon"))
    if not cands:
        return None, "open"
    return min(cands, key=lambda c: c[0])


def pnl_of(entry, price, side):
    return (price / entry - 1.0) * 100.0 if side == "long" else (entry / price - 1.0) * 100.0


def evaluate(path, side, now, horizon_h, grace_s, min_hold_s=0.0):
    open_rows, settled_rows, series = [], [], {}
    now_ms = int(now.timestamp() * 1000)
    flip_exit = flip_exit_for(side)     # does this side close on momentum-flip, or ride to horizon?
    for c, picks in coin_picks(path).items():
        for ep in split_episodes(picks, grace_s):     # each flagged run is its own position
            entry = ep[0]
            entry_price = entry.get("entry_price")
            if not entry_price:
                continue
            # Exclude reversal-risk-flagged shorts from the track record (policy: don't trade them).
            if (side == "short" and ECFG.get("exclude_reversal_risk")
                    and entry.get("reversal_risk", "none") != "none"):
                continue
            sl = effective_sl(side, entry)     # per-pick hard stop (% P&L), or None
            src = "mexc" if entry.get("data_src") == "mexc" else "binance"
            entry_dt = parse_ts(entry["ts"])
            # Floor to the 15m candle boundary so the entry's own candle is included right away
            # (Binance filters by openTime, so an un-floored start drops a fresh pick until the
            # next 15m close — which left just-flagged coins missing from the open board).
            start_ms = (int(entry_dt.timestamp() * 1000) // 900_000) * 900_000
            ohlc = kline_ohlc(c, src, start_ms)
            # Drop future-dated candles: MEXC returns fabricated future slots for some
            # commodity/CFD perps (USOIL, NICKEL, …), which otherwise stretch the equity
            # curve weeks ahead and freeze exits on prices that don't exist yet.
            ohlc = [k for k in ohlc if k[0] <= now_ms]
            if not ohlc:
                continue
            close_dt, reason = episode_close(ep, now, horizon_h, grace_s, flip_exit, min_hold_s)
            close_ms = int(close_dt.timestamp() * 1000) if close_dt else None
            # Settled position: freeze the path (and the exit price) at the close time.
            kept = [k for k in ohlc if close_ms is None or k[0] <= close_ms] or ohlc[:1]

            # Intra-candle exits — hard stop-loss (`sl`) and take-profit (`tp`), both filled on
            # the wick the way a real bracket order does: the stop on the ADVERSE wick (low for a
            # long, high for a short), the TP on the FAVORABLE wick (high for a long, low for a
            # short). The first candle to breach either level closes the position right there, at
            # the level's exact price — this can settle a position the screener still re-flags as
            # open, and overrides flip/horizon. When a single candle breaches both, the stop wins
            # (conservative: assume the adverse move filled first).
            tp = tp_for(side)
            exit_event = None     # (ts_ms, reason, level_pct)
            for t, hi, lo, _cl in kept:
                adverse = lo if side == "long" else hi
                favor = hi if side == "long" else lo
                if sl is not None and adverse > 0 and pnl_of(entry_price, adverse, side) <= sl:
                    exit_event = (t, "stop", sl)
                    break
                if tp is not None and favor > 0 and pnl_of(entry_price, favor, side) >= tp:
                    exit_event = (t, "tp", tp)
                    break

            if exit_event is not None:
                ev_ms, reason, level = exit_event
                kept = [k for k in kept if k[0] <= ev_ms]
                # exit price that yields exactly `level` for this side
                exit_price = (entry_price * (1 + level / 100.0) if side == "long"
                              else entry_price / (1 + level / 100.0))
                pnl = round(level, 2)
                close_dt = datetime.fromtimestamp(ev_ms / 1000.0, tz=timezone.utc)
                close_ms = ev_ms
                # path = closes up to the exit candle, then the bracket fill itself as the last point
                path_pnl = [(t, round(pnl_of(entry_price, cl, side), 3)) for t, _h, _l, cl in kept[:-1] if cl > 0]
                path_pnl.append((ev_ms, round(level, 3)))
            else:
                # P&L-since-call path (signed by side: rising = the position is winning)
                path_pnl = [(t, round(pnl_of(entry_price, cl, side), 3)) for t, _h, _l, cl in kept if cl > 0]
                exit_price = kept[-1][3]
                pnl = round(pnl_of(entry_price, exit_price, side), 2)

            if not path_pnl:
                continue
            age = (now - entry_dt).total_seconds() / 3600.0
            held = ((close_dt or now) - entry_dt).total_seconds() / 3600.0
            # downsample for the row sparkline
            vals = [v for _, v in path_pnl]
            if len(vals) > SPARK_POINTS:
                step = len(vals) / SPARK_POINTS
                vals = [vals[int(i * step)] for i in range(SPARK_POINTS)] + [vals[-1]]
            row = {
                "coin": c, "ts": entry["ts"], "age_hours": round(age, 1),
                "held_hours": round(held, 1), "entry": entry_price, "now": exit_price,
                "pnl": pnl, "spark": vals, "closed": close_dt is not None, "close_reason": reason,
                "extra": ("early" if entry.get("early") else "") if side == "long" else entry.get("reversal_risk", "none"),
            }
            (settled_rows if close_dt is not None else open_rows).append(row)
            series[f"{c}@{entry['ts']}"] = {"start": start_ms, "close_ms": close_ms, "path": path_pnl}

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
    times = sorted({t for s in series.values() for t, _ in s["path"]})
    filled = {}
    for key, s in series.items():
        d = dict(s["path"])
        last, col = None, {}
        for t in times:
            if t in d:
                last = d[t]
            live = t >= s["start"] and (s["close_ms"] is None or t <= s["close_ms"])
            col[t] = last if live else None
        filled[key] = col
    for t in times:
        vals = [filled[key][t] for key in filled if filled[key][t] is not None]
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
        "longs": evaluate(LONGS, "long", now, h, g, flip_min_hold_for("long")),
        "shorts": evaluate(SHORTS, "short", now, h, g, flip_min_hold_for("short")),
    }
    OUT_FILE.write_text(json.dumps(out, indent=2))
    L, S = out["longs"], out["shorts"]
    print(f"Wrote {OUT_FILE}: horizon {h}h · "
          f"longs open {L['wins']}/{L['count']} (avg {L['avg']:+}%), settled {L['settled']['wins']}/{L['settled']['count']} · "
          f"shorts open {S['wins']}/{S['count']} (avg {S['avg']:+}%), settled {S['settled']['wins']}/{S['settled']['count']} · "
          f"eq pts {len(L['equity'])}/{len(S['equity'])}")


if __name__ == "__main__":
    main()
