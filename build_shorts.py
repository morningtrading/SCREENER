#!/usr/bin/env python3
"""
SHORT screener: the weakest perps with decent volume and low reversal risk.

Pipeline:
  1. Scan the whole MEXC (~893) + Hyperliquid (~230) perp universe (one bulk call each)
     for coins that are already weak with decent 24h volume.
  2. Deep-score a shortlist on 1h/2h/4h candles — Binance futures when the coin is listed
     there (best data + native 2h + OI/funding), else MEXC klines.
  3. Score WEAKNESS (the inverse of the momentum composite) + breakdown leading signals,
     and compute a separate REVERSAL-RISK level (oversold / crowded-short / bounce /
     capitulation) shown as a warning — info only, with a page toggle to filter it out.

Writes shorts_ranking.json for the data server (/shorts). Reuses build_momentum's helpers.
"""
import os
import json
from datetime import datetime, timezone
from pathlib import Path

import build_momentum as bm   # reuse fetch_klines, tf metrics, recent_changes, oi/funding, regime

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "shorts_ranking.json"
MEXC_TICKER = "https://contract.mexc.com/api/v1/contract/ticker"
MEXC_KLINE = "https://contract.mexc.com/api/v1/contract/kline"
TIMEOUT, UA = bm.TIMEOUT, bm.UA

CONFIG_FILE = Path(os.environ.get("SCREENER_CONFIG", str(BASE / "config.json")))
_DEFAULTS = {
    "shorts": {
        "timeframes": ["1h", "2h", "4h"],
        "weights": {"1h": 0.5, "2h": 0.3, "4h": 0.2, "recent": 0.12},
        "min_volume_usdt": 2_000_000,   # decent 24h USDT volume to be shortable
        "max_24h_change_pct": 0.0,      # candidate must be down on the day (deep score does the real work)
        "scan_shortlist": 50,           # how many weakest candidates to deep-score
        "store_top": 40,                # rows kept in the JSON (page shows top 10, pages through the rest)
        "roc_lookback_bars": 6,
        "ema_fast": 9, "ema_slow": 21, "slope_bars": 3,
        "klines_limit": 60,
        "trend_down_factor": 0.3, "damp_floor": 0.2,
        "max_extension_pct": 12.0,      # damp weakness when already stretched below the mean
        "accel_weight": 0.2,
        "min_score": 1.0,
        "require_downtrend_alignment": True,
        "recent_windows_min": [5, 15, 30, 45],
        # breakdown leading signals
        "sell_ratio_max": 0.45,         # taker-buy share <= this -> aggressive selling
        "rvol_recent_bars": 3, "rvol_base_bars": 20, "rvol_min": 1.8,
        "accel_lookback": 6, "min_accel_pct": 0.2,
        "breakdown_lookback": 24,
        "oi_min_pct": 0.5,              # OI rising while price falls -> new shorts/conviction
        "funding_min": 0.0,             # funding >= this (longs paying) -> room to fall
        "early_min_signals": 2, "early_weight": 0.2,
        # reversal-risk (info + toggle, not a hard filter)
        "rsi_period": 14, "rsi_oversold": 25.0,
        "max_drawdown_ext": 12.0,       # price > this% below its EMA = stretched (bounce-prone)
        "funding_squeeze": -0.0005,     # funding <= this = crowded shorts (squeeze risk)
        "bounce_pct": 1.5,              # 15m already up > this = reversal underway
        "capitulation_bar_pct": 6.0,    # single 1h bar down > this = capitulation (bounce-prone)
        "regime_coins": ["BTC", "ETH", "HYPE", "ZEC"],
    }
}


def load_config():
    cfg = {s: dict(v) for s, v in _DEFAULTS.items()}
    try:
        user = json.load(open(CONFIG_FILE))
        for s in cfg:
            cfg[s].update(user.get(s, {}))
    except FileNotFoundError:
        pass
    return cfg


CFG = load_config()["shorts"]


# ------------------------------------------------------------------- universe (bulk scans)
def mexc_universe():
    """base -> {change24%, vol24 USDT, funding, oi} for every MEXC USDT perp (one call)."""
    out = {}
    try:
        for c in bm.get_json(MEXC_TICKER).get("data", []):
            sym = c.get("symbol", "")
            if not sym.endswith("_USDT"):
                continue
            try:
                out[sym.split("_")[0].upper()] = {
                    "change24": float(c["riseFallRate"]) * 100.0,
                    "vol24": float(c.get("amount24") or 0.0),
                    "funding": float(c.get("fundingRate")) if c.get("fundingRate") is not None else None,
                    "oi": float(c.get("holdVol") or 0.0),
                }
            except (KeyError, ValueError, TypeError):
                pass
    except Exception:
        pass
    return out


def hl_universe():
    """base -> {change24%, vol24 USDT, funding, oi} for every Hyperliquid perp (one POST)."""
    out = {}
    try:
        import urllib.request
        body = json.dumps({"type": "metaAndAssetCtxs"}).encode()
        req = urllib.request.Request(bm.HL_INFO, data=body,
                                     headers={"User-Agent": UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            meta, ctxs = json.loads(r.read().decode())
        for u, c in zip(meta.get("universe", []), ctxs):
            try:
                prev = float(c["prevDayPx"]); mark = float(c["markPx"])
                out[u["name"].upper()] = {
                    "change24": (mark / prev - 1.0) * 100.0 if prev else 0.0,
                    "vol24": float(c.get("dayNtlVlm") or 0.0),
                    "funding": float(c["funding"]) if c.get("funding") is not None else None,
                    "oi": float(c.get("openInterest") or 0.0),
                }
            except (KeyError, ValueError, TypeError):
                pass
    except Exception:
        pass
    return out


# ----------------------------------------------------------------------- klines / metrics
def fetch_mexc_klines(base, interval, limit):
    """MEXC contract kline -> {close,high,low,vol} (no taker-buy). Intervals: Min5/Min60/Hour4."""
    try:
        d = bm.get_json(f"{MEXC_KLINE}/{base}_USDT?interval={interval}").get("data", {})
        c = d.get("close") or []
        if not c:
            return None
        n = limit
        return {
            "close": [float(x) for x in c[-n:]],
            "high": [float(x) for x in d["high"][-n:]],
            "low": [float(x) for x in d["low"][-n:]],
            "vol": [float(x) for x in d["vol"][-n:]],
        }
    except Exception:
        return None


_MEXC_TF = {"1h": "Min60", "2h": None, "4h": "Hour4"}   # MEXC has no native 2h


def get_kl(base, data_src, tf, cfg):
    if data_src == "binance":
        return bm.fetch_klines(base, "futures", tf, cfg["klines_limit"])
    if data_src == "mexc":
        iv = _MEXC_TF.get(tf)
        return fetch_mexc_klines(base, iv, cfg["klines_limit"]) if iv else None
    return None


def short_metrics(kl, cfg):
    """Like bm.tf_metrics but also exposes trend_down (ema_fast<ema_slow and close<ema_slow)."""
    closes = kl["close"] if kl else None
    if not closes or len(closes) < max(cfg["ema_slow"], cfg["roc_lookback_bars"] + 1, cfg["slope_bars"] + 1):
        return None
    close = closes[-1]
    lb = cfg["roc_lookback_bars"]
    roc = (close / closes[-1 - lb] - 1.0) * 100.0 if closes[-1 - lb] else 0.0
    ef = bm.ema_series(closes, cfg["ema_fast"]); es = bm.ema_series(closes, cfg["ema_slow"])
    ema_fast, ema_slow = ef[-1], es[-1]
    sb = cfg["slope_bars"]
    slope = (ef[-1] / ef[-1 - sb] - 1.0) * 100.0 if ef[-1 - sb] else 0.0
    extension = (close - ema_slow) / ema_slow * 100.0 if ema_slow else 0.0
    last_bar = (close / closes[-2] - 1.0) * 100.0 if closes[-2] else 0.0
    return {"roc": roc, "slope": slope, "extension": extension, "last_bar": last_bar,
            "trend_down": (ema_fast < ema_slow) and (close < ema_slow),
            "trend_up": (ema_fast > ema_slow) and (close > ema_slow)}


def short_tf_score(m, cfg):
    """Per-timeframe WEAKNESS: negative roc/slope reward, gated by downtrend, damped if stretched down."""
    raw = -(0.7 * m["roc"] + 0.3 * m["slope"])
    trend_mult = 1.0 if m["trend_down"] else cfg["trend_down_factor"]
    below = max(0.0, -m["extension"])
    damp = bm.clamp(1.0 - below / cfg["max_extension_pct"], cfg["damp_floor"], 1.0)
    return raw * trend_mult * damp


def rsi(closes, period):
    if not closes or len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    seed = deltas[:period]
    avg_gain = sum(d for d in seed if d > 0) / period
    avg_loss = sum(-d for d in seed if d < 0) / period
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + (d if d > 0 else 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + (-d if d < 0 else 0.0)) / period
    if avg_loss == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + avg_gain / avg_loss), 1)


def recent_for(base, data_src, cfg):
    if data_src == "binance":
        return bm.recent_changes(base, "futures", cfg)
    # MEXC 5m (no taker-buy)
    windows = cfg.get("recent_windows_min", [5, 15, 30, 45])
    bars = [max(1, int(w) // 5) for w in windows]
    need = max(max(bars) + 1, cfg.get("rvol_recent_bars", 3) + cfg.get("rvol_base_bars", 20))
    kl = fetch_mexc_klines(base, "Min5", need + 2)
    if not kl or len(kl["close"]) < max(bars) + 1:
        return {"windows": {}, "buy_ratio": None, "rvol": None}
    closes, vol = kl["close"], kl["vol"]
    last = closes[-1]
    win = {f"{w}m": (round((last / closes[-1 - nb] - 1.0) * 100.0, 3) if closes[-1 - nb] else 0.0)
           for w, nb in zip(windows, bars)}
    rn, bn = cfg.get("rvol_recent_bars", 3), cfg.get("rvol_base_bars", 20)
    rvol = None
    if len(vol) >= rn + bn:
        bv = sum(vol[-(rn + bn):-rn]) / bn
        rvol = round((sum(vol[-rn:]) / rn) / bv, 2) if bv > 0 else None
    return {"windows": win, "buy_ratio": None, "rvol": rvol}


def breakdown_signals(kl1h, micro, cfg):
    """Inverse of momentum's early signals — leading evidence of a breakdown."""
    fired = []
    d = {"buy_ratio": micro.get("buy_ratio"), "rvol": micro.get("rvol"),
         "oi_change": micro.get("oi_change"), "funding": micro.get("funding"),
         "accel_1h": None, "breakdown": False}
    if d["buy_ratio"] is not None and d["buy_ratio"] <= cfg["sell_ratio_max"]:
        fired.append("sell")
    if d["rvol"] is not None and d["rvol"] >= cfg["rvol_min"]:
        fired.append("vol")
    if kl1h:
        c, lo = kl1h["close"], kl1h["low"]
        alb = cfg["accel_lookback"]
        if len(c) >= 2 * alb + 1 and c[-1 - alb] and c[-1 - 2 * alb]:
            d["accel_1h"] = round((c[-1] / c[-1 - alb] - 1.0) * 100.0 - (c[-1 - alb] / c[-1 - 2 * alb] - 1.0) * 100.0, 3)
            if d["accel_1h"] <= -cfg["min_accel_pct"]:
                fired.append("accel")
        blb = cfg["breakdown_lookback"]
        if len(lo) >= blb + 1:
            d["breakdown"] = c[-1] < min(lo[-1 - blb:-1])
            if d["breakdown"]:
                fired.append("brk")
    if d["oi_change"] is not None and d["oi_change"] >= cfg["oi_min_pct"]:
        fired.append("oi")
    if d["funding"] is not None and d["funding"] >= cfg["funding_min"]:
        fired.append("fund")
    return fired, d


def reversal_risk(rsi_val, ext_1h, funding, recent, last_1h, cfg):
    reasons, strong = [], False
    if rsi_val is not None and rsi_val < cfg["rsi_oversold"]:
        reasons.append(f"oversold (RSI {rsi_val:.0f})"); strong = True       # oversold = bounce-prone
    if ext_1h is not None and ext_1h < -cfg["max_drawdown_ext"]:
        reasons.append(f"stretched {ext_1h:.0f}% below mean")
    if funding is not None and funding <= cfg["funding_squeeze"]:
        reasons.append(f"crowded short (funding {funding:.4f})")
        if funding <= 2 * cfg["funding_squeeze"]:
            strong = True                                                    # deeply crowded = squeeze
    r15 = (recent or {}).get("15m")
    if r15 is not None and r15 > cfg["bounce_pct"]:
        reasons.append(f"15m bounce +{r15:.1f}%"); strong = True             # already reversing
    if last_1h is not None and last_1h < -cfg["capitulation_bar_pct"]:
        reasons.append(f"capitulation bar {last_1h:.1f}%")
    level = "high" if (strong or len(reasons) >= 2) else ("low" if reasons else "none")
    return level, reasons


def score_short(base, data_src, cfg, recent, micro):
    tfs = cfg["timeframes"]
    weights = cfg["weights"]
    per_tf, kl_tf = {}, {}
    for tf in tfs:
        kl = get_kl(base, data_src, tf, cfg)
        kl_tf[tf] = kl
        per_tf[tf] = short_metrics(kl, cfg) if kl else None
    avail = [tf for tf in tfs if per_tf[tf] is not None]
    if "1h" not in avail or "4h" not in avail:
        return None  # need at least the 1h and 4h backbone

    trend_down_4h = per_tf["4h"]["trend_down"]
    bucket_w = {tf: weights.get(tf, 0.0) for tf in avail}
    bucket_v = {tf: short_tf_score(per_tf[tf], cfg) for tf in avail}
    rec_mom = bm.recent_momentum(recent)
    if weights.get("recent", 0.0) and rec_mom is not None:
        bucket_w["recent"] = weights["recent"]
        bucket_v["recent"] = -rec_mom            # falling recently = weakness
    wsum = sum(bucket_w.values()) or 1.0
    score = sum(bucket_w[k] * bucket_v[k] for k in bucket_w) / wsum

    # breakdown acceleration: within a confirmed 4h downtrend, reward a quickening fall
    accel = 0.0
    if trend_down_4h and recent:
        pace = [x for x in (recent.get("5m"), recent.get("15m")) if x is not None]
        if pace:
            accel = -sum(pace) / len(pace)
    score += cfg.get("accel_weight", 0.0) * accel

    fired, det = breakdown_signals(kl_tf.get("1h"), micro, cfg)
    score += cfg.get("early_weight", 0.0) * len(fired)

    ext_1h = per_tf["1h"]["extension"]
    last_1h = per_tf["1h"]["last_bar"]
    roc_1h = per_tf["1h"]["roc"]
    rsi_val = rsi(kl_tf["1h"]["close"], cfg["rsi_period"]) if kl_tf.get("1h") else None
    rlevel, rreasons = reversal_risk(rsi_val, ext_1h, micro.get("funding"), recent, last_1h, cfg)

    reason = ""
    if score < cfg["min_score"]:
        reason = f"score {score:.2f} < {cfg['min_score']}"
    elif roc_1h >= 0:
        reason = "1h not falling"
    elif cfg["require_downtrend_alignment"] and not trend_down_4h:
        reason = "4h trend not down"
    short = reason == ""

    return {
        "short_score": round(score, 3),
        "short": short,
        "reason": reason,
        "extension_1h": round(ext_1h, 2),
        "last_bar_1h": round(last_1h, 2),
        "rsi": rsi_val,
        "buy_ratio": det["buy_ratio"],
        "rvol": det["rvol"],
        "accel_1h": det["accel_1h"],
        "breakdown": det["breakdown"],
        "oi_change": det["oi_change"],
        "funding": det["funding"],
        "breakdown_signals": fired,
        "reversal_risk": rlevel,
        "risk_reasons": rreasons,
        "roc": {tf: round(per_tf[tf]["roc"], 2) for tf in avail},
        "trend": {tf: bool(per_tf[tf]["trend_down"]) for tf in avail},
        "data_src": data_src,
    }


# ------------------------------------------------------------------------------------ main
def main():
    mexc = mexc_universe()
    hl = hl_universe()
    perp = bm.futures_perp_bases()

    bases = set(mexc) | set(hl)
    cands = []
    for b in bases:
        m, h = mexc.get(b), hl.get(b)
        src = m or h
        vol = max(m["vol24"] if m else 0.0, h["vol24"] if h else 0.0)
        change = m["change24"] if m else h["change24"]
        funding = (m or {}).get("funding") if m else (h or {}).get("funding")
        exchanges = ([("mexc")] if m else []) + (["hl"] if h else []) + (["binance"] if b in perp else [])
        cands.append({"coin": b, "change24": change, "vol24": vol, "funding": funding,
                      "exchanges": exchanges})

    # decent volume + already weak; rank weakest first; deep-score the shortlist
    pool = [c for c in cands if c["vol24"] >= CFG["min_volume_usdt"] and c["change24"] <= CFG["max_24h_change_pct"]]
    pool.sort(key=lambda c: c["change24"])
    pool = pool[: CFG["scan_shortlist"]]

    rows = []
    for c in pool:
        b = c["coin"]
        data_src = "binance" if b in perp else ("mexc" if b in mexc else None)
        row = dict(c)
        row.update({"data_src": data_src or "none", "short_score": None, "short": False,
                    "reason": "no candles", "reversal_risk": "none", "risk_reasons": [],
                    "rsi": None, "buy_ratio": None, "rvol": None, "oi_change": None,
                    "recent": {}, "roc": {}, "trend": {}, "breakdown_signals": []})
        if data_src:
            rec = recent_for(b, data_src, CFG)
            row["recent"] = rec.get("windows", {})
            micro = {"buy_ratio": rec.get("buy_ratio"), "rvol": rec.get("rvol"),
                     "oi_change": bm.oi_change(b, CFG) if data_src == "binance" else None,
                     "funding": c["funding"]}
            res = score_short(b, data_src, CFG, rec.get("windows", {}), micro)
            if res:
                row.update(res)
        rows.append(row)

    # rank: confirmed SHORTs first, then by weakness score (unscored sink); keep store_top
    rows.sort(key=lambda r: (r["short"], r["short_score"] if r["short_score"] is not None else -1e9), reverse=True)
    rows = rows[: CFG["store_top"]]
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # regime banner (reuse momentum's helper)
    spot = bm.spot_symbols()
    regime = {}
    for rc in CFG.get("regime_coins", ["BTC", "ETH", "HYPE", "ZEC"]):
        rcu = rc.upper()
        mkt = "futures" if rcu in perp else ("spot" if f"{rcu}USDT" in spot else None)
        regime[rcu] = bm.regime_for(rcu, mkt, CFG)

    out = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": CFG,
        "regime": regime,
        "total": len(rows),
        "count_short": sum(1 for r in rows if r["short"]),
        "scanned": len(bases),
        "rows": rows,
    }
    OUT_FILE.write_text(json.dumps(out, indent=2))
    top = [r["coin"] for r in rows if r["short"]][:10]
    print(f"Wrote {OUT_FILE}: scanned {len(bases)} perps, {len(rows)} listed, "
          f"{out['count_short']} flagged SHORT, generated {out['generated_utc']}")
    print(f"Top shorts: {', '.join(top) if top else '(none)'}")


if __name__ == "__main__":
    main()
