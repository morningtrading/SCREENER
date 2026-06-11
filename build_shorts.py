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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import build_momentum as bm   # reuse fetch_klines, tf metrics, recent_changes, oi/funding, regime

# Deep-scoring the shortlist is pure HTTP wait (klines for 1h/2h/4h + recent/OI per coin),
# so we fan it out across threads. get_json/fetch_klines are stateless (urllib, no shared
# session/cache), hence thread-safe. Tunable via SHORTS_SCAN_WORKERS.
SCAN_WORKERS = int(os.environ.get("SHORTS_SCAN_WORKERS", "8"))

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "shorts_ranking.json"
MEXC_TICKER = "https://contract.mexc.com/api/v1/contract/ticker"
MEXC_KLINE = "https://contract.mexc.com/api/v1/contract/kline"
MEXC_DETAIL = "https://contract.mexc.com/api/v1/contract/detail"
TIMEOUT, UA = bm.TIMEOUT, bm.UA

# Cache for the *static* universe lists — the Binance-perp symbol set and the
# MEXC CFD-exclusion set only change when a coin is listed/delisted, so we don't
# refetch them every minute. Live ticker data (mexc/hl vol/change/drawdown) is
# NOT cached, so weakness ranking and scores stay fresh. TTL via SHORTS_UNIVERSE_TTL.
CACHE_DIR = BASE / ".cache"
UNIVERSE_TTL = int(os.environ.get("SHORTS_UNIVERSE_TTL", "1800"))   # seconds (30 min)


def _cached_set(name, ttl, fn):
    """Return fn() as a set, served from a JSON file cache when younger than ttl."""
    import time
    f = CACHE_DIR / f"{name}.json"
    try:
        if f.exists() and (time.time() - f.stat().st_mtime) < ttl:
            return set(json.loads(f.read_text()))
    except Exception:
        pass
    val = fn()
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        f.write_text(json.dumps(sorted(val)))
    except Exception:
        pass   # cache is best-effort; a write failure must not break the scan
    return val

# MEXC lists non-crypto CFD perps (stocks, forex, commodities, metals, indices) tagged by
# "trade zone" in conceptPlate. We exclude these from the crypto shorts universe — their klines
# are non-crypto and MEXC even returns fabricated future candles for some of them.
EXCLUDE_ZONES = ("tradfi", "commodit", "stock", "metals", "forex", "preipo", "premarket")

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
        # reversal-risk: now an actionable EXCLUSION, not just an info toggle. The track record
        # showed flagged shorts (squeeze-prone) hold every blow-up and are net-negative even with
        # a tight stop, while clean shorts are strongly positive. "any" excludes high+low, "high"
        # only the high-risk ones, "none" keeps the old info-only behaviour. Excluded coins still
        # show on the page (with the ⚠ and the reason) but are not flagged SHORT or logged as picks.
        "exclude_reversal_risk": "any",
        "rsi_period": 14, "rsi_oversold": 25.0,
        "max_drawdown_ext": 12.0,       # price > this% below its EMA = stretched (bounce-prone)
        "funding_squeeze": -0.0005,     # funding <= this = crowded shorts (squeeze risk)
        "bounce_pct": 1.5,              # 15m already up > this = reversal underway
        "capitulation_bar_pct": 6.0,    # single 1h bar down > this = capitulation (bounce-prone)
        "regime_coins": ["BTC", "ETH", "HYPE", "ZEC"],
        "picks_keep": 20000,            # max lines kept in shorts_history/short_picks.jsonl
    },
    # Shared liquidity thresholds (the same field the rankings / combined pages use). We reuse
    # max_spread_pct here so a candidate's MEXC bid/ask spread must be tight enough to short.
    "filters": {"min_volume_usdt": 1_000_000, "max_spread_pct": 0.10, "min_volatility_pct": 2.0},
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


_ALL = load_config()
CFG = _ALL["shorts"]
MAX_SPREAD_PCT = _ALL["filters"]["max_spread_pct"]   # gate shorts on MEXC bid/ask spread


# ------------------------------------------------------------------- universe (bulk scans)
def commodity_bases():
    """Set of bases for MEXC non-crypto CFD perps (stocks/forex/commodities/metals/indices).

    Identified by EXCLUDE_ZONES tags in each contract's conceptPlate (one bulk detail call).
    Keyed by the symbol's base (same as mexc_universe), e.g. USOIL, METASTOCK, NICKEL, EUR.
    """
    try:
        out = set()
        for c in bm.get_json(MEXC_DETAIL).get("data", []):
            sym = c.get("symbol", "")
            if not sym.endswith("_USDT"):
                continue
            zones = [str(z).lower() for z in (c.get("conceptPlate") or [])]
            if any(bad in z for z in zones for bad in EXCLUDE_ZONES):
                out.add(sym.split("_")[0].upper())
        return out
    except Exception:
        return set()


def mexc_universe():
    """base -> {change24%, vol24 USDT, funding, oi} for every MEXC USDT perp (one call)."""
    out = {}
    try:
        for c in bm.get_json(MEXC_TICKER).get("data", []):
            sym = c.get("symbol", "")
            if not sym.endswith("_USDT"):
                continue
            try:
                # bid1/ask1 are in the same bulk ticker response (same fields the MEXC ranking
                # uses) — derive the % spread so shorts can be gated on liquidity for free.
                bid = float(c.get("bid1") or 0.0)
                ask = float(c.get("ask1") or 0.0)
                spread = (ask - bid) / ((ask + bid) / 2.0) * 100.0 if (bid > 0 and ask > 0 and ask >= bid) else None
                last = float(c["lastPrice"])
                # Drawdown from the 24h high (<= 0): how far price has pulled back. Captures
                # "weak right now" even when a coin is still green on net 24h change — which the
                # old 24h-change gate missed (a recent drop on a coin that had pumped earlier).
                hi = float(c.get("high24Price") or 0.0)
                drawdown = (last / hi - 1.0) * 100.0 if (hi > 0 and last > 0) else None
                out[sym.split("_")[0].upper()] = {
                    "change24": float(c["riseFallRate"]) * 100.0,
                    "vol24": float(c.get("amount24") or 0.0),
                    "funding": float(c.get("fundingRate")) if c.get("fundingRate") is not None else None,
                    "oi": float(c.get("holdVol") or 0.0),
                    "price": last,
                    "spread_pct": round(spread, 5) if spread is not None else None,
                    "drawdown_pct": round(drawdown, 3) if drawdown is not None else None,
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
                    "price": mark,
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
    weakness_score = sum(bucket_w[k] * bucket_v[k] for k in bucket_w) / wsum
    score = weakness_score

    # breakdown acceleration: within a confirmed 4h downtrend, reward a quickening fall
    accel = 0.0
    if trend_down_4h and recent:
        pace = [x for x in (recent.get("5m"), recent.get("15m")) if x is not None]
        if pace:
            accel = -sum(pace) / len(pace)
    accel_contrib = cfg.get("accel_weight", 0.0) * accel
    score += accel_contrib

    fired, det = breakdown_signals(kl_tf.get("1h"), micro, cfg)
    breakdown_contrib = cfg.get("early_weight", 0.0) * len(fired)
    score += breakdown_contrib

    # Shadow score: 4h-weighted / accel-dominant formulation validated by the 2026-06-11
    # track-record decomposition (short_score r=-0.13, accel r=+0.38, weakness_4h r=+0.31).
    # Logged alongside short_score for forward-testing — zero effect on ranking or alerts.
    # Phase 1 of the score-reweight plan; flip to live only after ≥7d of validation data.
    shadow_score = None
    shadow_weakness = shadow_accel_c = shadow_br_c = None
    if cfg.get("shadow_enabled"):
        sw = cfg.get("shadow_weights", {})
        sw_keys = [k for k in bucket_w if sw.get(k, 0.0) > 0]
        sw_sum = sum(sw[k] for k in sw_keys) or 1.0
        shadow_weakness = sum(sw[k] * bucket_v[k] for k in sw_keys) / sw_sum
        shadow_accel_c = cfg.get("shadow_accel_weight", 0.0) * accel
        br = det.get("buy_ratio")
        # low buy_ratio = sellers dominating = good for short; -(ratio-0.5) is positive when <0.5
        shadow_br_c = (cfg.get("shadow_buy_ratio_weight", 0.0) * -(br - 0.5)
                       if br is not None else 0.0)
        shadow_score = round(shadow_weakness + shadow_accel_c + shadow_br_c + breakdown_contrib, 3)

    # Decomposition of the final short_score for the eval/track-record analysis: the three
    # additive parts (weakness + accel + breakdown ≈ short_score) plus the per-timeframe weakness
    # contributions. Logged per pick so a later study can correlate each component — not just the
    # composite — against realized P&L. (The composite is anti-correlated with short profit; this
    # is what lets us find WHICH term is pointing the wrong way.) Info-only: does not affect scoring.
    components = {
        "weakness_score": round(weakness_score, 4),
        "accel_contrib": round(accel_contrib, 4),
        "breakdown_contrib": round(breakdown_contrib, 4),
        "weakness_tf": {k: round(bucket_v[k], 4) for k in bucket_v},
        "weights_tf": {k: round(bucket_w[k], 4) for k in bucket_w},
        "n_breakdown_signals": len(fired),
        "shadow_score": shadow_score,
        "shadow_weakness": round(shadow_weakness, 4) if shadow_weakness is not None else None,
        "shadow_accel_contrib": round(shadow_accel_c, 4) if shadow_accel_c is not None else None,
        "shadow_buy_ratio_contrib": round(shadow_br_c, 4) if shadow_br_c is not None else None,
    }

    ext_1h = per_tf["1h"]["extension"]
    last_1h = per_tf["1h"]["last_bar"]
    roc_1h = per_tf["1h"]["roc"]
    rsi_val = rsi(kl_tf["1h"]["close"], cfg["rsi_period"]) if kl_tf.get("1h") else None
    rlevel, rreasons = reversal_risk(rsi_val, ext_1h, micro.get("funding"), recent, last_1h, cfg)

    # P4: hard rvol floor — low-volume drift is noise (rvol<0.5 bucket: 40% WR, -0.44% avg).
    # rvol_min remains the confirmation-reason threshold; rvol_floor is the hard reject.
    rvol_floor = cfg.get("rvol_floor")
    rvol_val = det.get("rvol")

    reason = ""
    if rvol_floor is not None and rvol_val is not None and rvol_val < rvol_floor:
        reason = f"rvol {rvol_val:.2f} < floor {rvol_floor}"
    elif score < cfg["min_score"]:
        reason = f"score {score:.2f} < {cfg['min_score']}"
    elif roc_1h >= 0:
        reason = "1h not falling"
    elif cfg["require_downtrend_alignment"] and not trend_down_4h:
        reason = "4h trend not down"
    short = reason == ""

    # Reversal-risk exclusion: a weak coin that is also squeeze-prone is NOT an actionable short
    # (it holds the blow-ups). Demote it — keep the row (page still shows it with ⚠ + reason) but
    # drop the SHORT flag so it is neither green nor logged as a pick.
    excl = cfg.get("exclude_reversal_risk", "any")
    if short and ((excl == "any" and rlevel != "none") or (excl == "high" and rlevel == "high")):
        short = False
        reason = f"reversal risk: {rlevel}"

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
        "components": components,
        "data_src": data_src,
    }


# ------------------------------------------------------------------------------------ main
def main():
    mexc = mexc_universe()   # live ticker — fresh every run (drives weakness)
    hl = hl_universe()       # live ticker — fresh every run
    # Static symbol lists (change only on listing/delisting) — cached to save ~7.5s/run.
    perp = _cached_set("perp_bases", UNIVERSE_TTL, bm.futures_perp_bases)
    excluded = _cached_set("commodity_bases", UNIVERSE_TTL, commodity_bases)

    bases = (set(mexc) | set(hl)) - excluded
    cands = []
    for b in bases:
        m, h = mexc.get(b), hl.get(b)
        src = m or h
        vol = max(m["vol24"] if m else 0.0, h["vol24"] if h else 0.0)
        change = m["change24"] if m else h["change24"]
        funding = (m or {}).get("funding") if m else (h or {}).get("funding")
        price = m["price"] if m else h["price"]
        spread_pct = m.get("spread_pct") if m else None     # MEXC bid/ask spread (None for HL-only)
        drawdown_pct = m.get("drawdown_pct") if m else None  # pullback from 24h high (None for HL-only)
        # Weakness axis for selection: how far below the 24h high (MEXC), else fall back to 24h
        # change (HL-only). More negative = weaker right now → deep-score it.
        weakness = drawdown_pct if drawdown_pct is not None else change
        exchanges = (["mexc"] if m else []) + (["hl"] if h else []) + (["binance"] if b in perp else [])
        cands.append({"coin": b, "change24": change, "vol24": vol, "funding": funding,
                      "price": price, "spread_pct": spread_pct, "drawdown_pct": drawdown_pct,
                      "weakness": weakness, "exchanges": exchanges})

    # Liquid enough (volume + tight spread), then rank by weakness — pullback from the 24h high,
    # NOT net 24h change — so coins dropping NOW are scored even if still green on the day.
    # The spread gate only applies when the MEXC spread is known (HL-only coins pass through).
    pool = [c for c in cands
            if c["vol24"] >= CFG["min_volume_usdt"]
            and (c["spread_pct"] is None or c["spread_pct"] <= MAX_SPREAD_PCT)]
    pool.sort(key=lambda c: c["weakness"] if c["weakness"] is not None else 0.0)
    pool = pool[: CFG["scan_shortlist"]]

    def deep_score(c):
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
        return row

    def deep_score_and_log(c):
        r = deep_score(c)
        if not r.get("short"):
            _log_rejected(r, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return r

    # P2: rejected-picks log — near-misses that selection bias would otherwise hide.
    # A "near-miss" is any deep-scored candidate that almost qualified: score >= 70% of
    # min_score (would qualify with a modest threshold shift), or rejected solely by
    # reversal_risk / RSI floor / rvol floor (a single config change would include it).
    # Logging them lets future backtests evaluate filter changes against the trades they
    # would have ADDED, not just removed, breaking the selection-bias loop.
    REJECTED_FILE = BASE / "shorts_history" / "rejected_picks.jsonl"
    _rej_buf: list = []

    def _log_rejected(r: dict, ts: str) -> None:
        reason = r.get("reason", "")
        score = r.get("short_score")
        near_miss = (
            (score is not None and score >= 0.7 * CFG["min_score"])
            or reason.startswith("reversal risk")
            or reason.startswith("rvol")
            or "rsi" in reason.lower()
            or "oversold" in reason.lower()
        )
        if near_miss and r.get("price"):
            _rej_buf.append(json.dumps({
                "ts": ts, "coin": r["coin"], "data_src": r.get("data_src"),
                "entry_price": r["price"], "short_score": score,
                "rejection_reason": reason,
                "reversal_risk": r.get("reversal_risk"), "rsi": r.get("rsi"),
                "rvol": r.get("rvol"), "buy_ratio": r.get("buy_ratio"),
                "oi_change": r.get("oi_change"), "change24_at_call": round(r.get("change24", 0), 2),
                "funding": r.get("funding"), "components": r.get("components"),
                "roc": r.get("roc"),
            }))

    # Fan the per-coin HTTP work out across threads; preserves pool order.
    with ThreadPoolExecutor(max_workers=max(1, SCAN_WORKERS)) as ex:
        rows = list(ex.map(deep_score_and_log, pool))

    # rank: confirmed SHORTs first, then by weakness score (unscored sink); keep store_top
    rows.sort(key=lambda r: (r["short"], r["short_score"] if r["short_score"] is not None else -1e9), reverse=True)
    rows = rows[: CFG["store_top"]]
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # regime banner (reuse momentum's helper)
    spot = bm.spot_symbols()
    regime = {}
    btc_roc_3h = None   # P6: logged into every pick for the dump-suppressor study
    for rc in CFG.get("regime_coins", ["BTC", "ETH", "HYPE", "ZEC"]):
        rcu = rc.upper()
        mkt = "futures" if rcu in perp else ("spot" if f"{rcu}USDT" in spot else None)
        rg = bm.regime_for(rcu, mkt, CFG)
        regime[rcu] = rg
        # P6: approximate 3h ROC from the two closest available windows (2h + 4h → interpolate,
        # or use whichever single TF is closer to 3h). regime_for logs "2h" and "4h" ROC keys.
        if rcu == "BTC" and rg:
            r2 = rg.get("2h"); r4 = rg.get("4h")
            if r2 is not None and r4 is not None:
                btc_roc_3h = round((r2 + r4) / 2.0, 3)   # midpoint ≈ 3h ROC
            elif r4 is not None:
                btc_roc_3h = round(r4, 3)

    out = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": CFG,
        "max_spread_pct": MAX_SPREAD_PCT,
        "regime": regime,
        "total": len(rows),
        "count_short": sum(1 for r in rows if r["short"]),
        "scanned": len(bases),
        "rows": rows,
    }
    OUT_FILE.write_text(json.dumps(out, indent=2))

    # Snapshot the proposed shorts (flagged) with their entry price, so we can later
    # compare to the actual price and score whether the call was right (see eval_shorts.py).
    HIST_DIR = BASE / "shorts_history"
    HIST_DIR.mkdir(exist_ok=True)
    picks_file = HIST_DIR / "short_picks.jsonl"
    new_lines = [json.dumps({
        "ts": out["generated_utc"], "coin": r["coin"], "data_src": r["data_src"],
        "entry_price": r.get("price"), "short_score": r["short_score"],
        "reversal_risk": r["reversal_risk"], "change24_at_call": round(r["change24"], 2),
        "rsi": r.get("rsi"), "funding": r.get("funding"), "exchanges": r.get("exchanges"),
        # Score components + raw signals, logged so the track-record study can decompose which
        # part of short_score drives (or fights) realized P&L — see score_short()'s `components`.
        "components": r.get("components"),
        "extension_1h": r.get("extension_1h"), "last_bar_1h": r.get("last_bar_1h"),
        "rvol": r.get("rvol"), "buy_ratio": r.get("buy_ratio"),
        "oi_change": r.get("oi_change"), "breakdown": r.get("breakdown"),
        "roc": r.get("roc"),
        "btc_roc_3h": btc_roc_3h,   # P6: market regime at entry for dump-suppressor study
    }) for r in rows if r.get("short") and r.get("price")]
    if new_lines:
        with open(picks_file, "a") as fh:
            fh.write("\n".join(new_lines) + "\n")
        keep = int(CFG.get("picks_keep", 20000))      # cap history (~1 week at 5-min cron)
        try:
            existing = picks_file.read_text().splitlines()
            if len(existing) > keep:
                picks_file.write_text("\n".join(existing[-keep:]) + "\n")
        except OSError:
            pass

    # P2: flush rejected near-misses
    if _rej_buf:
        REJECTED_FILE.parent.mkdir(exist_ok=True)
        with open(REJECTED_FILE, "a") as fh:
            fh.write("\n".join(_rej_buf) + "\n")
        keep = int(CFG.get("picks_keep", 20000))
        try:
            existing = REJECTED_FILE.read_text().splitlines()
            if len(existing) > keep:
                REJECTED_FILE.write_text("\n".join(existing[-keep:]) + "\n")
        except OSError:
            pass

    # P6: BTC 3h ROC — already computed in regime loop above; log into picks file.
    # (btc_roc_3h is set by the regime section below; if BTC wasn't in regime_coins it's None)
    top = [r["coin"] for r in rows if r["short"]][:10]
    print(f"Wrote {OUT_FILE}: scanned {len(bases)} perps ({len(excluded)} non-crypto CFDs excluded), "
          f"{len(rows)} listed, {out['count_short']} flagged SHORT, generated {out['generated_utc']}")
    print(f"Top shorts: {', '.join(top) if top else '(none)'}")


if __name__ == "__main__":
    main()
