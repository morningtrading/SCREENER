#!/usr/bin/env python3
"""
Momentum screener: full MEXC+HL perp universe  ×  Binance 1h/2h/4h candles.

Pipeline:
  1. Fetch the full MEXC+HL perp universe, sort by 24h strength, take the top N.
  2. For each candidate with a Binance market, pull 1h / 2h / 4h klines.
  3. Score momentum with a strong weight on 1h (all weights/thresholds in config.json).
  4. Flag coins in a genuine UPTREND, not a post-pump blow-off (overextension and
     single-candle-spike guards + higher-timeframe confirmation).

Writes momentum_ranking.json for the data server (/momentum).

No third-party deps — stdlib urllib/json only (same style as build_binance_ranking.py).
"""
import os
import json
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# Deep-scoring the candidate pool is pure HTTP wait (klines for 1h/2h/4h + recent/OI per
# coin). Threads overlap that wait; each request uses its own urllib call (no shared mutable
# state), hence thread-safe — same pattern as the shorts screener. Tunable via MOM_SCAN_WORKERS.
SCAN_WORKERS = int(os.environ.get("MOM_SCAN_WORKERS", "10"))

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "momentum_ranking.json"

FAPI = "https://fapi.binance.com/fapi/v1"          # USDⓈ-M futures
SAPI = "https://api.binance.com/api/v3"            # spot (fallback)
MEXC_DETAIL = "https://contract.mexc.com/api/v1/contract/detail"   # MEXC perp universe
HL_INFO = "https://api.hyperliquid.xyz/info"       # Hyperliquid universe (POST)
FDATA = "https://fapi.binance.com/futures/data"    # OI history, long/short ratios
TIMEOUT = 12
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# --- Tunable parameters: config.json, NOT hardcoded (same merge pattern as the ranker). ---
CONFIG_FILE = Path(os.environ.get("SCREENER_CONFIG", str(BASE / "config.json")))
_DEFAULTS = {
    "momentum": {
        "timeframes": ["1h", "2h", "4h"],
        "weights": {"1h": 0.5, "2h": 0.3, "4h": 0.2, "recent": 0.12},  # strong 1h; small recent bucket
        "accel_weight": 0.2,           # bonus/penalty for 5-15m pace within a confirmed 4h uptrend
        "max_recent_drop_pct": 1.5,    # veto: reject if the last 15m dumped more than this (post-pump)
        "roc_lookback_bars": 6,        # rate-of-change lookback (bars)
        "ema_fast": 9,
        "ema_slow": 21,
        "slope_bars": 3,               # bars over which EMA-fast slope is measured
        "klines_limit": 60,            # candles fetched per timeframe
        "trend_down_factor": 0.3,      # score multiplier when a TF is NOT in an uptrend
        "damp_floor": 0.2,             # min overextension damping multiplier
        "max_extension_pct": 12.0,     # reject: 1h price > this % above its EMA21 (overextended)
        "max_single_bar_pct": 6.0,     # reject: last 1h candle moved more than this (one-bar pump)
        "min_score": 1.0,              # min composite score to flag as momentum
        "require_uptrend_alignment": True,  # require the 4h trend to confirm (not just a 1h blip)
        "spot_fallback": True,         # score coins via Binance spot when they have no perpetual
        "broad_scan_top": 60,          # deep-score the N strongest (by 24h change) perps from the MEXC+HL universe
        "broad_min_volume_usdt": 2000000,  # min 24h volume for a coin to enter the shortlist
        "max_noise_pct": None,         # max-noise entry gate: demote a long if its median 5m candle range exceeds this % (null = off)
        "noise_bars": 12,              # 5m bars (≈1h) over which the median candle-range "noise" is measured
        "recent_windows_min": [5, 15, 30, 45],  # rolling % change windows (from 5m candles) for the dot strip
        # --- Early-detection leading signals (Tier 1 + OI/funding) ---
        "buy_ratio_bars": 6,           # 5m bars for the taker-buy ratio (aggressive demand)
        "buy_ratio_min": 0.55,         # fire 'buy' when taker-buy share >= this
        "rvol_recent_bars": 3,         # 5m bars treated as "now" for relative volume
        "rvol_base_bars": 20,          # 5m bars used as the volume baseline
        "rvol_min": 1.8,               # fire 'vol' when recent vol is >= this x baseline
        "accel_lookback": 6,           # 1h bars per leg when measuring acceleration (2nd derivative)
        "min_accel_pct": 0.2,          # fire 'accel' when the 1h move speeds up by >= this (pp)
        "breakout_lookback": 24,       # 1h bars for the Donchian (new-high) breakout
        "oi_hist_period": "5m",        # open-interest history granularity
        "oi_lookback_bars": 6,         # OI-history bars to measure the change over (~30 min)
        "oi_min_pct": 0.5,             # fire 'oi' when open interest rose >= this %
        "funding_max": 0.0003,         # fire 'fund' when funding <= this (not crowded-long yet)
        "early_min_signals": 2,        # >= this many leading signals -> flagged EARLY
        "early_weight": 0.2,           # small score bonus per leading signal that fires
        "regime_coins": ["BTC", "ETH", "HYPE", "ZEC"],  # reference coins for the market-regime banner
        "picks_keep": 20000,           # max lines kept in momentum_history/momentum_picks.jsonl
        # Coins permanently excluded from the long universe regardless of score. Use when a coin
        # has a persistent pattern of false momentum signals (repeated losses, structural weakness).
        "coin_blacklist": [],
    }
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


CFG = load_config()["momentum"]


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


# --------------------------------------------------------------------------- Binance data
def futures_perp_bases():
    """base -> futures symbol, for PERPETUAL/USDT/TRADING contracts (reuses the ranker's filter)."""
    info = get_json(f"{FAPI}/exchangeInfo")
    return {
        s["baseAsset"]: s["symbol"]
        for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    }


def spot_symbols():
    """Set of tradable spot symbols (lightweight all-symbol price list)."""
    try:
        return {row["symbol"] for row in get_json(f"{SAPI}/ticker/price")}
    except Exception:
        return set()


def mexc_bases():
    """Set of base assets listed as MEXC USDT-margined perpetuals (symbols like BTC_USDT)."""
    try:
        d = get_json(MEXC_DETAIL)
        out = set()
        for c in d.get("data", []):
            sym = c.get("symbol", "")
            if sym.endswith("_USDT"):
                out.add(sym.split("_")[0].upper())
        return out
    except Exception:
        return set()


def hl_bases():
    """Set of coins listed on Hyperliquid perps (POST {"type":"allMids"} → coin->mid)."""
    try:
        body = json.dumps({"type": "allMids"}).encode()
        req = urllib.request.Request(HL_INFO, data=body,
                                     headers={"User-Agent": UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            mids = json.loads(r.read().decode())
        # keys are coin names; HL prefixes some spot-only names with "@" — keep alnum tickers.
        return {k.upper() for k in mids.keys() if k and not k.startswith("@")}
    except Exception:
        return set()


def funding_map():
    """base -> last funding rate, from one bulk premiumIndex call (USDⓈ-M perps only)."""
    try:
        out = {}
        for p in get_json(f"{FAPI}/premiumIndex"):
            sym = p.get("symbol", "")
            if sym.endswith("USDT"):
                try:
                    out[sym[:-4].upper()] = float(p["lastFundingRate"])
                except (KeyError, ValueError, TypeError):
                    pass
        return out
    except Exception:
        return {}


def oi_change(base, cfg):
    """% change in open interest over the lookback (futures only); None if unavailable.

    Rising OI alongside a rising price = new money entering (a real move, not just a squeeze).
    """
    period = cfg.get("oi_hist_period", "5m")
    lb = cfg.get("oi_lookback_bars", 6)
    try:
        rows = get_json(f"{FDATA}/openInterestHist?symbol={base}USDT&period={period}&limit={lb + 1}")
        oi = [float(r["sumOpenInterest"]) for r in rows]
    except Exception:
        return None
    if len(oi) < 2 or oi[0] <= 0:
        return None
    return round((oi[-1] / oi[0] - 1.0) * 100.0, 3)


def fetch_klines(base, market, interval, limit):
    """Return per-bar arrays we use: close, high, low, vol, and taker-buy base vol.

    Binance kline indices: 2=high 3=low 4=close 5=volume 9=taker-buy-base-volume.
    Returns None on failure or empty.
    """
    sym = f"{base}USDT"
    url = (f"{FAPI}/klines" if market == "futures" else f"{SAPI}/klines")
    try:
        raw = get_json(f"{url}?symbol={sym}&interval={interval}&limit={limit}")
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None
    try:
        return {
            "close": [float(k[4]) for k in raw],
            "high": [float(k[2]) for k in raw],
            "low": [float(k[3]) for k in raw],
            "vol": [float(k[5]) for k in raw],
            "tbv": [float(k[9]) for k in raw],   # taker buy base volume (aggressive buying)
        }
    except (IndexError, ValueError, TypeError):
        return None


# ------------------------------------------------------------------------------- momentum
def ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def tf_metrics(kl, cfg):
    """Per-timeframe momentum metrics from a klines dict (uses closes)."""
    closes = kl["close"] if kl else None
    if not closes or len(closes) < max(cfg["ema_slow"], cfg["roc_lookback_bars"] + 1, cfg["slope_bars"] + 1):
        return None
    close = closes[-1]
    lb = cfg["roc_lookback_bars"]
    roc = (close / closes[-1 - lb] - 1.0) * 100.0 if closes[-1 - lb] else 0.0
    ef = ema_series(closes, cfg["ema_fast"])
    es = ema_series(closes, cfg["ema_slow"])
    ema_fast, ema_slow = ef[-1], es[-1]
    sb = cfg["slope_bars"]
    slope = (ef[-1] / ef[-1 - sb] - 1.0) * 100.0 if ef[-1 - sb] else 0.0
    extension = (close - ema_slow) / ema_slow * 100.0 if ema_slow else 0.0
    last_bar = (close / closes[-2] - 1.0) * 100.0 if closes[-2] else 0.0
    trend_up = (ema_fast > ema_slow) and (close > ema_slow)
    return {
        "roc": roc, "slope": slope, "extension": extension,
        "last_bar": last_bar, "trend_up": trend_up,
    }


def tf_score(m, cfg):
    """Single-timeframe score: momentum (roc+slope), dampened by overextension & misalignment."""
    raw = 0.7 * m["roc"] + 0.3 * m["slope"]
    trend_mult = 1.0 if m["trend_up"] else cfg["trend_down_factor"]
    damp = clamp(1.0 - max(0.0, m["extension"]) / cfg["max_extension_pct"], cfg["damp_floor"], 1.0)
    return raw * trend_mult * damp


def recent_changes(base, market, cfg):
    """From one 5m-candle fetch: the recent-% windows (dot strip) plus two leading micro-signals.

    Returns {"windows": {"5m":.., ...}, "buy_ratio": 0..1 or None, "rvol": float or None}:
      - buy_ratio: taker-buy / total volume over the last buy_ratio_bars (aggressive demand);
      - rvol:      recent volume vs its longer baseline (a surge often precedes a breakout).
    """
    windows = cfg.get("recent_windows_min", [5, 15, 30, 45])
    bars = [max(1, int(w) // 5) for w in windows]          # 5m candles → bars per window
    need = max(max(bars) + 1, cfg.get("rvol_recent_bars", 3) + cfg.get("rvol_base_bars", 20), cfg.get("buy_ratio_bars", 6))
    kl = fetch_klines(base, market, "5m", need + 2)
    if not kl or len(kl["close"]) < max(bars) + 1:
        return {"windows": {}, "buy_ratio": None, "rvol": None, "noise_pct": None}
    closes, vol, tbv = kl["close"], kl["vol"], kl["tbv"]
    last = closes[-1]
    win = {}
    for w, nb in zip(windows, bars):
        ref = closes[-1 - nb]
        win[f"{w}m"] = round((last / ref - 1.0) * 100.0, 3) if ref else 0.0

    nb = cfg.get("buy_ratio_bars", 6)
    tv = sum(vol[-nb:])
    buy_ratio = round(sum(tbv[-nb:]) / tv, 3) if tv > 0 else None

    rn, bn = cfg.get("rvol_recent_bars", 3), cfg.get("rvol_base_bars", 20)
    rvol = None
    if len(vol) >= rn + bn:
        recent_v = sum(vol[-rn:]) / rn
        base_v = sum(vol[-(rn + bn):-rn]) / bn
        rvol = round(recent_v / base_v, 2) if base_v > 0 else None

    # Noise = the coin's typical 5m candle range (median of (high-low)/close over the last
    # `noise_bars` bars). It proxies how wide the natural wiggle is: when noise exceeds the long
    # hard-stop, the stop gets hit by chop, not by a real reversal — the thin-but-high-volume
    # micro-caps (KAT, SLX, VELVET…) that dominate the long stop-outs. Used as a max-noise entry
    # gate (see max_noise_pct) and logged on each pick so it can be evaluated/tuned later.
    noise_pct = None
    hi, lo = kl.get("high"), kl.get("low")
    if hi and lo:
        nbars = max(1, int(cfg.get("noise_bars", 12)))
        ranges = [(h - l) / c * 100.0 for h, l, c in zip(hi[-nbars:], lo[-nbars:], closes[-nbars:]) if c > 0]
        if ranges:
            ranges.sort()
            noise_pct = round(ranges[len(ranges) // 2], 3)     # median bar range %

    return {"windows": win, "buy_ratio": buy_ratio, "rvol": rvol, "noise_pct": noise_pct}


def regime_for(base, market, cfg):
    """A reference coin's % change across the same windows the page uses
    (5/15/30/45m + 1h/2h/4h ROC). Pure context for the regime banner — info only."""
    out = {}
    if not market:
        return out
    out.update(recent_changes(base, market, cfg).get("windows", {}))
    for tf in cfg["timeframes"]:
        kl = fetch_klines(base, market, tf, cfg["klines_limit"])
        m = tf_metrics(kl, cfg) if kl else None
        if m:
            out[tf] = round(m["roc"], 3)
            out[f"{tf}_up"] = bool(m["trend_up"])
    return out


def recent_momentum(recent):
    """Length-weighted recent drift (%): longer windows dominate so a lone 5m blip can't.

    e.g. {"5m":.., "15m":.., "30m":.., "45m":..} -> single %-like number, or None if empty.
    """
    if not recent:
        return None
    num = den = 0.0
    for k, v in recent.items():
        if v is None:
            continue
        w = float(int(k.rstrip("m")))      # weight by window length in minutes
        num += w * v
        den += w
    return num / den if den else None


def early_signals_for(kl1h, micro, cfg):
    """Compute the leading-indicator signals and which of them fire (the 'early' confluence).

    Returns (fired:list[str], detail:dict). Signals:
      buy  — taker-buy ratio >= buy_ratio_min (aggressive demand, leads price)
      vol  — relative volume >= rvol_min (a surge often precedes the breakout)
      accel— 1h move is accelerating (2nd derivative >= min_accel_pct)
      brk  — price made a new breakout_lookback-bar high (Donchian breakout)
      oi   — open interest up >= oi_min_pct (new money, not just a squeeze)
      fund — funding <= funding_max (not yet crowded-long: room to run)
    """
    fired = []
    detail = {"buy_ratio": micro.get("buy_ratio"), "rvol": micro.get("rvol"),
              "oi_change": micro.get("oi_change"), "funding": micro.get("funding"),
              "accel_1h": None, "breakout": False}

    if detail["buy_ratio"] is not None and detail["buy_ratio"] >= cfg.get("buy_ratio_min", 0.55):
        fired.append("buy")
    if detail["rvol"] is not None and detail["rvol"] >= cfg.get("rvol_min", 1.8):
        fired.append("vol")

    if kl1h:
        c, h = kl1h["close"], kl1h["high"]
        alb = cfg.get("accel_lookback", 6)
        if len(c) >= 2 * alb + 1 and c[-1 - alb] and c[-1 - 2 * alb]:
            recent_roc = (c[-1] / c[-1 - alb] - 1.0) * 100.0
            prior_roc = (c[-1 - alb] / c[-1 - 2 * alb] - 1.0) * 100.0
            detail["accel_1h"] = round(recent_roc - prior_roc, 3)
            if detail["accel_1h"] >= cfg.get("min_accel_pct", 0.2):
                fired.append("accel")
        blb = cfg.get("breakout_lookback", 24)
        if len(h) >= blb + 1:
            detail["breakout"] = c[-1] > max(h[-1 - blb:-1])
            if detail["breakout"]:
                fired.append("brk")

    if detail["oi_change"] is not None and detail["oi_change"] >= cfg.get("oi_min_pct", 0.5):
        fired.append("oi")
    if detail["funding"] is not None and detail["funding"] <= cfg.get("funding_max", 0.0003):
        fired.append("fund")
    return fired, detail


def score_coin(base, market, cfg, recent=None, micro=None):
    """Fetch all timeframes for a coin and compute composite score + momentum verdict.

    The 5/15/30/45m `recent` strip feeds the score (recent bucket + accel term + dump veto);
    `micro` carries the leading signals (buy ratio, rvol, OI change, funding) which, with the
    1h breakout/acceleration, form the 'early' confluence and a small early score bonus.
    """
    micro = micro or {}
    tfs = cfg["timeframes"]
    weights = cfg["weights"]
    per_tf = {}
    kl_tf = {}
    for tf in tfs:
        kl = fetch_klines(base, market, tf, cfg["klines_limit"])
        kl_tf[tf] = kl
        per_tf[tf] = tf_metrics(kl, cfg) if kl else None
    if any(per_tf[tf] is None for tf in tfs):
        return None  # incomplete data — treat as unscored

    trend_4h = per_tf.get("4h", {}).get("trend_up", False)

    # Weighted composite over 1h/2h/4h ...
    bucket_w = {tf: weights.get(tf, 0.0) for tf in tfs}
    bucket_v = {tf: tf_score(per_tf[tf], cfg) for tf in tfs}
    # (1) ... plus a small "recent" bucket (length-weighted recent drift).
    rec_mom = recent_momentum(recent)
    rec_w = weights.get("recent", 0.0)
    if rec_w and rec_mom is not None:
        bucket_w["recent"] = rec_w
        bucket_v["recent"] = rec_mom
    wsum = sum(bucket_w.values()) or 1.0
    score = sum(bucket_w[k] * bucket_v[k] for k in bucket_w) / wsum

    # (2) Acceleration: inside a confirmed 4h uptrend, reward rising / penalise fading 5–15m pace.
    accel = 0.0
    if trend_4h and recent:
        r5, r15 = recent.get("5m"), recent.get("15m")
        pace = [x for x in (r5, r15) if x is not None]
        if pace:
            accel = sum(pace) / len(pace)
    score += cfg.get("accel_weight", 0.0) * accel

    # Leading-indicator confluence (Tier 1 + OI/funding) and a small early bonus.
    fired, detail = early_signals_for(kl_tf.get("1h"), micro, cfg)
    score += cfg.get("early_weight", 0.0) * len(fired)
    early = len(fired) >= cfg.get("early_min_signals", 2)

    ext_1h = per_tf.get("1h", {}).get("extension", 0.0)
    last_1h = per_tf.get("1h", {}).get("last_bar", 0.0)
    roc_1h = per_tf.get("1h", {}).get("roc", 0.0)
    r15 = (recent or {}).get("15m")
    max_drop = cfg.get("max_recent_drop_pct", 0.0)

    # Post-pump guard: flag as momentum only if a real, confirmed, non-overextended uptrend.
    reason = ""
    if score < cfg["min_score"]:
        reason = f"score {score:.2f} < {cfg['min_score']}"
    elif roc_1h <= 0:
        reason = "1h not rising"
    elif ext_1h > cfg["max_extension_pct"]:
        reason = f"overextended (1h {ext_1h:.1f}% > {cfg['max_extension_pct']}%)"
    elif abs(last_1h) > cfg["max_single_bar_pct"]:
        reason = f"single-bar spike (1h {last_1h:.1f}%)"
    elif cfg["require_uptrend_alignment"] and not trend_4h:
        reason = "4h trend not up"
    elif max_drop and r15 is not None and r15 < -max_drop:   # (3) recent dump veto
        reason = f"recent 15m dump ({r15:.1f}%)"
    momentum = reason == ""

    return {
        "score": round(score, 3),
        "momentum": momentum,
        "reason": reason,
        "extension_1h": round(ext_1h, 2),
        "last_bar_1h": round(last_1h, 2),
        "recent_mom": round(rec_mom, 3) if rec_mom is not None else None,
        "buy_ratio": detail["buy_ratio"],
        "rvol": detail["rvol"],
        "accel_1h": detail["accel_1h"],
        "breakout": detail["breakout"],
        "oi_change": detail["oi_change"],
        "funding": detail["funding"],
        "early_signals": fired,
        "early": early,
        "price": kl_tf["1h"]["close"][-1] if kl_tf.get("1h") else None,
        "roc": {tf: round(per_tf[tf]["roc"], 2) for tf in tfs},
        "trend": {tf: per_tf[tf]["trend_up"] for tf in tfs},
    }


# ----------------------------------------------------------------------------------- main
def main():
    perp = futures_perp_bases()
    spot = spot_symbols() if CFG["spot_fallback"] else set()
    mexc = mexc_bases()        # which trending coins are also listed on MEXC perps
    hl = hl_bases()            # ...and on Hyperliquid
    funding = funding_map()    # base -> funding rate (one bulk call, futures only)

    pairlist = set()
    try:
        pf = os.environ.get("SCREENER_PAIRS_FILE", str(BASE / "pairs.json"))
        pairlist = {p.split("/")[0].upper() for p in json.load(open(pf))["pairs"]}
    except Exception:
        pass

    # Candidate pool: full MEXC+HL perp universe, ranked by 24h strength, top N deep-scored.
    # The uptrend/extension gates in score_coin decide what actually flags as momentum.
    candidates = []
    try:
        import build_shorts as bs   # lazy: avoids a circular import at module load
        uni = {}
        for src in (bs.mexc_universe(), bs.hl_universe()):
            for b, t in src.items():
                # keep the higher-volume ticker when a coin is on both venues
                if b not in uni or (t.get("vol24") or 0) > (uni[b].get("vol24") or 0):
                    uni[b] = t
        min_vol = CFG.get("broad_min_volume_usdt", 0) or 0
        blacklist = set(CFG.get("coin_blacklist", []))
        extra = [b for b, t in uni.items()
                 if b and (t.get("vol24") or 0) >= min_vol and b not in blacklist]
        # strongest first (most up on the day); the deep score rejects post-pump blow-offs
        extra.sort(key=lambda b: uni[b].get("change24") or 0.0, reverse=True)
        for b in extra[: int(CFG.get("broad_scan_top", 60))]:
            t = uni[b]
            candidates.append({"symbol": b, "name": b,
                               "price": t.get("price"), "change24": t.get("change24"),
                               "volume24h": t.get("vol24"), "candidate_src": "universe"})
    except Exception as e:
        print(f"[error] universe scan failed ({e}); aborting")
        raise SystemExit(1)

    def evaluate(c):
        base = (c.get("symbol") or "").upper()
        if not base or not base.isascii() or not base.isalnum():
            market = "none"
        elif base in perp:
            market = "futures"
        elif f"{base}USDT" in spot:
            market = "spot"
        else:
            market = "none"

        # Which exchanges list this coin (for the /momentum "Exchanges" column).
        exchanges = []
        if base and (base in perp or f"{base}USDT" in spot):
            exchanges.append("binance")
        if base and base in mexc:
            exchanges.append("mexc")
        if base and base in hl:
            exchanges.append("hl")

        row = {
            "coin": base or (c.get("name") or "?"),
            "name": c.get("name"),
            "price": c.get("price"),
            "change24": c.get("change24"),
            "volume24h": c.get("volume24h"),
            "candidate_src": c.get("candidate_src", "universe"),
            "market": market,
            "exchanges": exchanges,
            "in_pairlist": base in pairlist,
            "score": None, "momentum": False, "reason": "",
            "extension_1h": None, "last_bar_1h": None, "roc": {}, "trend": {},
            "recent": {}, "recent_mom": None,
            "buy_ratio": None, "rvol": None, "accel_1h": None, "breakout": False,
            "oi_change": None, "funding": None, "early_signals": [], "early": False,
            "noise_pct": None,
        }
        if market != "none":
            rec = recent_changes(base, market, CFG)
            row["recent"] = rec.get("windows", {})
            row["noise_pct"] = rec.get("noise_pct")
            micro = {
                "buy_ratio": rec.get("buy_ratio"),
                "rvol": rec.get("rvol"),
                "oi_change": oi_change(base, CFG) if market == "futures" else None,
                "funding": funding.get(base) if market == "futures" else None,
            }
            res = score_coin(base, market, CFG, rec.get("windows", {}), micro)
            if res is None:
                row["reason"] = "no/short candles"
            else:
                row.update(res)
            # Max-noise entry gate: a coin whose typical 5m range is wider than `max_noise_pct`
            # gets noise-stopped before its move resolves, so it is not actionable as a long even
            # when momentum scores. Demote it (drop the green flag) rather than dropping the row,
            # so it still shows on the page with the reason. Off when max_noise_pct is null.
            mn = CFG.get("max_noise_pct")
            if row.get("momentum") and mn and row.get("noise_pct") is not None and row["noise_pct"] > mn:
                row["momentum"] = False
                row["reason"] = f"noisy: 5m range {row['noise_pct']:.2f}% > {mn}% (stop-prone)"
        else:
            row["reason"] = "no Binance market"
        return row

    # Fan the per-coin HTTP work out across threads — the pool can now be ~90 coins (trending +
    # broad universe), not ~30, so a sequential loop would blow the 1-min cron budget.
    with ThreadPoolExecutor(max_workers=max(1, SCAN_WORKERS)) as ex:
        rows = list(ex.map(evaluate, candidates))

    # Sort: momentum coins first, then by score desc (unscored sink to the bottom).
    rows.sort(key=lambda r: (r["momentum"], r["score"] if r["score"] is not None else -1e9), reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # Market-regime reference coins (info-only banner).
    regime = {}
    for rc in CFG.get("regime_coins", ["BTC", "ETH", "HYPE", "ZEC"]):
        rcu = rc.upper()
        mkt = "futures" if rcu in perp else ("spot" if f"{rcu}USDT" in spot else None)
        regime[rcu] = regime_for(rcu, mkt, CFG)

    out = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": CFG,
        "regime": regime,
        "total": len(rows),
        "count_momentum": sum(1 for r in rows if r["momentum"]),
        "rows": rows,
    }
    OUT_FILE.write_text(json.dumps(out, indent=2))

    # Snapshot the proposed longs (momentum-flagged) with their entry price, so we can later
    # compare to the actual price and score whether the call was right (see eval_momentum.py).
    HIST_DIR = BASE / "momentum_history"
    HIST_DIR.mkdir(exist_ok=True)
    picks_file = HIST_DIR / "momentum_picks.jsonl"
    new_lines = [json.dumps({
        "ts": out["generated_utc"], "coin": r["coin"], "market": r.get("market"),
        "entry_price": r.get("price"), "score": r["score"],
        "change24": r.get("change24"), "early": r.get("early"),
        "early_signals": r.get("early_signals"), "exchanges": r.get("exchanges"),
        "noise_pct": r.get("noise_pct"), "candidate_src": r.get("candidate_src"),
    }) for r in rows if r.get("momentum") and r.get("price")]
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

    hot = [r["coin"] for r in rows if r["momentum"]]
    print(f"Wrote {OUT_FILE}: {len(rows)} candidates, {out['count_momentum']} momentum "
          f"(w1h={CFG['weights'].get('1h')}), generated {out['generated_utc']}")
    print(f"Momentum (uptrend, not post-pump): {', '.join(hot) if hot else '(none right now)'}")


if __name__ == "__main__":
    main()
