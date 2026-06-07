#!/usr/bin/env python3
"""
Momentum screener: CoinMarketCap trending  ×  Binance 1h/2h/4h candles.

Pipeline:
  1. Scrape CMC's trending list (the candidate set the market is watching).
  2. For each trending coin with a Binance market, pull 1h / 2h / 4h klines.
  3. Score momentum with a strong weight on 1h (all weights/thresholds in config.json).
  4. Flag coins in a genuine UPTREND, not a post-pump blow-off (overextension and
     single-candle-spike guards + higher-timeframe confirmation).

Writes momentum_ranking.json for the data server (/momentum). Also stores each raw CMC
snapshot under momentum/ so trending membership can be compared over time.

No third-party deps — stdlib urllib/json only (same style as build_binance_ranking.py).
"""
import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT_FILE = BASE / "momentum_ranking.json"
SNAP_DIR = BASE / "momentum"

# CMC trending: the same data-API endpoint the website itself calls (no key needed).
CMC_URL = "https://api.coinmarketcap.com/data-api/v3/topsearch/rank"
# Optional official Pro API (used only if SCREENER_CMC_API_KEY is set).
CMC_PRO_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/trending/latest"
CMC_API_KEY = os.environ.get("SCREENER_CMC_API_KEY", "").strip()

FAPI = "https://fapi.binance.com/fapi/v1"          # USDⓈ-M futures
SAPI = "https://api.binance.com/api/v3"            # spot (fallback)
MEXC_DETAIL = "https://contract.mexc.com/api/v1/contract/detail"   # MEXC perp universe
HL_INFO = "https://api.hyperliquid.xyz/info"       # Hyperliquid universe (POST)
TIMEOUT = 12
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# --- Tunable parameters: config.json, NOT hardcoded (same merge pattern as the ranker). ---
CONFIG_FILE = Path(os.environ.get("SCREENER_CONFIG", str(BASE / "config.json")))
_DEFAULTS = {
    "momentum": {
        "timeframes": ["1h", "2h", "4h"],
        "weights": {"1h": 0.5, "2h": 0.3, "4h": 0.2},  # strong weight on 1h
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
        "candidate_limit": 30,         # max trending coins to evaluate
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


# ----------------------------------------------------------------------------- CMC scrape
def fetch_trending():
    """Return (rows, source) where rows is a list of dicts with coin/cmc fields."""
    if CMC_API_KEY:
        try:
            d = get_json(CMC_PRO_URL, headers={"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"})
            out = []
            for c in d.get("data", []):
                q = (c.get("quote") or {}).get("USD", {})
                out.append({
                    "symbol": c.get("symbol"), "name": c.get("name"), "slug": c.get("slug"),
                    "cmc_rank": c.get("cmc_rank"), "price": q.get("price"),
                    "cmc_change_24h": q.get("percent_change_24h"),
                    "cmc_change_7d": q.get("percent_change_7d"),
                    "volume24h": q.get("volume_24h"), "market_cap": q.get("market_cap"),
                })
            return out, "cmc_pro_api"
        except Exception as e:
            print(f"[warn] CMC Pro API failed ({e}); falling back to public data-API")
    d = get_json(CMC_URL)
    rows = (d.get("data") or {}).get("cryptoTopSearchRanks") or []
    out = []
    for c in rows:
        pc = c.get("priceChange") or {}
        out.append({
            "symbol": c.get("symbol"), "name": c.get("name"), "slug": c.get("slug"),
            "cmc_rank": c.get("rank"), "price": pc.get("price"),
            "cmc_change_24h": pc.get("priceChange24h"),
            "cmc_change_7d": pc.get("priceChange7d"),
            "volume24h": pc.get("volume24h"), "market_cap": c.get("marketCap"),
        })
    return out, "cmc_data_api"


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


def fetch_klines(base, market, interval, limit):
    sym = f"{base}USDT"
    url = (f"{FAPI}/klines" if market == "futures" else f"{SAPI}/klines")
    try:
        raw = get_json(f"{url}?symbol={sym}&interval={interval}&limit={limit}")
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None
    # close price is index 4
    try:
        return [float(k[4]) for k in raw]
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


def tf_metrics(closes, cfg):
    """Per-timeframe momentum metrics from a list of closes."""
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


def score_coin(base, market, cfg):
    """Fetch all timeframes for a coin and compute composite score + momentum verdict."""
    tfs = cfg["timeframes"]
    weights = cfg["weights"]
    per_tf = {}
    for tf in tfs:
        closes = fetch_klines(base, market, tf, cfg["klines_limit"])
        m = tf_metrics(closes, cfg) if closes else None
        per_tf[tf] = m
    if any(per_tf[tf] is None for tf in tfs):
        return None  # incomplete data — treat as unscored

    wsum = sum(weights.get(tf, 0.0) for tf in tfs) or 1.0
    score = sum(weights.get(tf, 0.0) * tf_score(per_tf[tf], cfg) for tf in tfs) / wsum

    ext_1h = per_tf.get("1h", {}).get("extension", 0.0)
    last_1h = per_tf.get("1h", {}).get("last_bar", 0.0)
    roc_1h = per_tf.get("1h", {}).get("roc", 0.0)
    trend_4h = per_tf.get("4h", {}).get("trend_up", False)

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
    momentum = reason == ""

    return {
        "score": round(score, 3),
        "momentum": momentum,
        "reason": reason,
        "extension_1h": round(ext_1h, 2),
        "last_bar_1h": round(last_1h, 2),
        "roc": {tf: round(per_tf[tf]["roc"], 2) for tf in tfs},
        "trend": {tf: per_tf[tf]["trend_up"] for tf in tfs},
    }


# ----------------------------------------------------------------------------------- main
def main():
    SNAP_DIR.mkdir(exist_ok=True)
    try:
        trending, source = fetch_trending()
    except Exception as e:
        print(f"[error] could not fetch CMC trending ({e}); keeping last momentum_ranking.json")
        raise SystemExit(1)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (SNAP_DIR / f"cmc_trending_{stamp}.json").write_text(json.dumps(trending, indent=2))
    (SNAP_DIR / "cmc_trending_latest.json").write_text(json.dumps(trending, indent=2))

    trending = trending[: CFG["candidate_limit"]]
    perp = futures_perp_bases()
    spot = spot_symbols() if CFG["spot_fallback"] else set()
    mexc = mexc_bases()        # which trending coins are also listed on MEXC perps
    hl = hl_bases()            # ...and on Hyperliquid

    pairlist = set()
    try:
        pf = os.environ.get("SCREENER_PAIRS_FILE", str(BASE / "pairs.json"))
        pairlist = {p.split("/")[0].upper() for p in json.load(open(pf))["pairs"]}
    except Exception:
        pass

    rows = []
    for c in trending:
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
            "cmc_rank": c.get("cmc_rank"),
            "price": c.get("price"),
            "cmc_change_24h": c.get("cmc_change_24h"),
            "volume24h": c.get("volume24h"),
            "market": market,
            "exchanges": exchanges,
            "in_pairlist": base in pairlist,
            "score": None, "momentum": False, "reason": "",
            "extension_1h": None, "last_bar_1h": None, "roc": {}, "trend": {},
        }
        if market != "none":
            res = score_coin(base, market, CFG)
            if res is None:
                row["reason"] = "no/short candles"
            else:
                row.update(res)
        else:
            row["reason"] = "no Binance market"
        rows.append(row)

    # Sort: momentum coins first, then by score desc (unscored sink to the bottom).
    rows.sort(key=lambda r: (r["momentum"], r["score"] if r["score"] is not None else -1e9), reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    out = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "config": CFG,
        "total": len(rows),
        "count_momentum": sum(1 for r in rows if r["momentum"]),
        "rows": rows,
    }
    OUT_FILE.write_text(json.dumps(out, indent=2))
    hot = [r["coin"] for r in rows if r["momentum"]]
    print(f"Wrote {OUT_FILE}: {len(rows)} trending coins, {out['count_momentum']} momentum "
          f"(src={source}, w1h={CFG['weights'].get('1h')}), generated {out['generated_utc']}")
    print(f"Momentum (uptrend, not post-pump): {', '.join(hot) if hot else '(none right now)'}")


if __name__ == "__main__":
    main()
