#!/usr/bin/env python3
import json
import time
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
try:
    from dotenv import load_dotenv
    import os
    _root = os.environ.get("SCREENER_PROJECT_ROOT", os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.environ.get("SCREENER_ENV_FILE", os.path.join(_root, ".env")))
except Exception:
    pass

# Try Hyperliquid SDK if available
HL_SDK_AVAILABLE = False
try:
    # The official SDK may expose Info/Exchange clients; import defensively
    import hyperliquid
    HL_SDK_AVAILABLE = True
except Exception:
    HL_SDK_AVAILABLE = False

PAIRLIST_URLS = [
    "http://permanent:9999/pairs.json",
    "http://localhost:9999/pairs.json",
]
BINANCE_FAPI_EXCHANGEINFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_FAPI_DEPTH = "https://fapi.binance.com/fapi/v1/depth"
BINANCE_SPOT_DEPTH = "https://api.binance.com/api/v3/depth"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"

# Fee structures (as percentages)
BINANCE_FEES = {
    "spot_maker": 0.10,     # 0.10%
    "spot_taker": 0.10,     # 0.10%
    "futures_maker": 0.02,  # 0.02%
    "futures_taker": 0.04,  # 0.04%
}

HYPERLIQUID_FEES = {
    "maker": 0.025,  # 0.025%
    "taker": 0.05,   # 0.05%
}

TIMEOUT = 5
MAX_PAIRS = None  # set to an int to limit processing

# Simple rate limiter to avoid 429s
class RateLimiter:
    def __init__(self, max_calls: int = 20, period: float = 1.0):
        from collections import deque
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()

    def wait_if_needed(self):
        now = time.time()
        while self.calls and self.calls[0] < now - self.period:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            sleep_time = self.period - (now - self.calls[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
                self.calls.popleft()
        self.calls.append(time.time())

_HL_RL = RateLimiter()
_SESSION = requests.Session()


def fetch_pairs() -> List[str]:
    for url in PAIRLIST_URLS:
        try:
            r = requests.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and "pairs" in data:
                return data["pairs"]
            if isinstance(data, list):
                return data
        except Exception as e:
            print(f"WARN: Failed to fetch pairs from {url}: {e}")
    raise RuntimeError("Could not fetch pairs from any server URL")


def parse_coin(pair_entry: str) -> Optional[str]:
    # Examples: "BTC/USDT:USDT", "ETH/USDT:USDT"
    base = pair_entry.split("/")[0].strip().upper()
    if base in ("USDT", "USD"):
        return None
    return base


def build_binance_usdt_symbol_map() -> Dict[str, str]:
    # Map base asset -> USDT symbol; prefer futures, fallback to spot symbol composition
    mapping: Dict[str, str] = {}
    try:
        r = requests.get(BINANCE_FAPI_EXCHANGEINFO, timeout=TIMEOUT)
        r.raise_for_status()
        info = r.json()
        for s in info.get("symbols", []):
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
                base = s.get("baseAsset", "").upper()
                symbol = s.get("symbol")
                # futures symbol already correct (e.g., BTCUSDT)
                if base and symbol:
                    mapping[base] = symbol
    except Exception as e:
        print(f"WARN: Failed to fetch Binance futures exchangeInfo: {e}")
    return mapping


def binance_best_bid_ask(symbol: str) -> Optional[Tuple[float, float]]:
    # Try futures depth first, fallback to spot
    params = {"symbol": symbol, "limit": 5}
    try:
        r = requests.get(BINANCE_FAPI_DEPTH, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if bids and asks:
            bid = float(bids[0][0])
            ask = float(asks[0][0])
            return bid, ask
    except Exception:
        pass

    # fallback to spot
    try:
        r = requests.get(BINANCE_SPOT_DEPTH, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if bids and asks:
            bid = float(bids[0][0])
            ask = float(asks[0][0])
            return bid, ask
    except Exception as e:
        print(f"WARN: Binance depth failed for {symbol}: {e}")
    return None


def hyperliquid_best_bid_ask(coin: str) -> Optional[Tuple[float, float]]:
    coin = coin.upper()
    # Hyperliquid API expects single object (not array)
    try:
        _HL_RL.wait_if_needed()
        payload = {"type": "l2Book", "coin": coin}  # Note: capital B in l2Book
        r = _SESSION.post(HYPERLIQUID_INFO, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        
        if isinstance(data, dict) and "levels" in data:
            levels = data["levels"]
            # levels is [bids, asks] where each is a list of {"px": "price", "sz": "size", "n": count}
            if isinstance(levels, list) and len(levels) >= 2:
                bids = levels[0]  # First element is bids
                asks = levels[1]  # Second element is asks
                
                if bids and asks and len(bids) > 0 and len(asks) > 0:
                    # Best bid is the first (highest) bid price
                    # Best ask is the first (lowest) ask price
                    best_bid = float(bids[0]["px"])
                    best_ask = float(asks[0]["px"])
                    return best_bid, best_ask
                    
    except Exception as e:
        print(f"WARN: Hyperliquid l2Book failed for {coin}: {e}")
    
    return None


def hyperliquid_funding_rate(coin: str) -> Optional[float]:
    coin = coin.upper()
    # Get latest funding rate from funding history
    try:
        _HL_RL.wait_if_needed()
        payload = {"type": "fundingHistory", "coin": coin, "startTime": 0}
        r = _SESSION.post(HYPERLIQUID_INFO, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        
        if isinstance(data, list) and len(data) > 0:
            # Get the most recent funding rate
            latest = data[-1]
            if isinstance(latest, dict) and "fundingRate" in latest:
                funding_rate = float(latest["fundingRate"])
                return funding_rate * 100  # Convert to percentage
                    
    except Exception as e:
        print(f"WARN: Hyperliquid funding rate failed for {coin}: {e}")
    
    return None


def compute_spread(bid: float, ask: float) -> Tuple[float, float]:
    spread_abs = max(0.0, ask - bid)
    mid = (ask + bid) / 2.0 if (ask > 0 and bid > 0) else 0.0
    spread_pct = (spread_abs / mid * 100.0) if mid > 0 else 0.0
    return spread_abs, spread_pct


def compute_net_profits(hl_spread_pct: Optional[float], bz_spread_pct: Optional[float]) -> dict:
    """Calculate net profits after fees for different strategies"""
    results = {}
    
    # Hyperliquid market making (buy at bid, sell at ask)
    if hl_spread_pct is not None:
        # Total fees: maker (buy) + taker (sell) or taker (buy) + maker (sell)
        # Conservative: assume taker + maker
        total_fees = HYPERLIQUID_FEES["maker"] + HYPERLIQUID_FEES["taker"]
        net_profit = max(0, hl_spread_pct - total_fees)
        results["hl_market_making"] = net_profit
    else:
        results["hl_market_making"] = None
    
    # Binance market making (futures)
    if bz_spread_pct is not None:
        total_fees = BINANCE_FEES["futures_maker"] + BINANCE_FEES["futures_taker"]
        net_profit = max(0, bz_spread_pct - total_fees)
        results["bz_market_making"] = net_profit
    else:
        results["bz_market_making"] = None
    
    # Arbitrage: buy on one exchange, sell on another
    if hl_spread_pct is not None and bz_spread_pct is not None:
        spread_diff = abs(hl_spread_pct - bz_spread_pct)
        # Fees: taker on both exchanges (immediate execution)
        arb_fees = HYPERLIQUID_FEES["taker"] + BINANCE_FEES["futures_taker"]
        net_arb = max(0, spread_diff - arb_fees)
        results["arbitrage"] = net_arb
    else:
        results["arbitrage"] = None
    
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Rank spreads from Hyperliquid and Binance for server-provided pairs")
    parser.add_argument("--limit", type=int, default=MAX_PAIRS or 0, help="Limit number of pairs processed (0 = all)")
    parser.add_argument("--sleep", type=float, default=0.05, help="Sleep between requests to avoid rate limits")
    args = parser.parse_args()

    print("Fetching pairs from server...")
    pairs = fetch_pairs()
    print(f"Pairs fetched: {len(pairs)}")

    # Build Binance symbol map once
    print("Fetching Binance exchange info (USDT symbols)...")
    base_to_symbol = build_binance_usdt_symbol_map()
    print(f"Binance USDT symbols mapped: {len(base_to_symbol)}")

    rows = []
    processed = 0

    for p in pairs:
        coin = parse_coin(p)
        if not coin:
            continue

        if args.limit and processed >= args.limit:
            break

        hl = hyperliquid_best_bid_ask(coin)
        hl_funding = hyperliquid_funding_rate(coin)
        # Binance symbol from map; fallback compose coin+USDT
        symbol = base_to_symbol.get(coin, f"{coin}USDT")
        bz = binance_best_bid_ask(symbol)

        hl_spread_abs = hl_spread_pct = bz_spread_abs = bz_spread_pct = None
        hl_bid = hl_ask = bz_bid = bz_ask = None

        if hl:
            hl_bid, hl_ask = hl
            hl_spread_abs, hl_spread_pct = compute_spread(hl_bid, hl_ask)
        if bz:
            bz_bid, bz_ask = bz
            bz_spread_abs, bz_spread_pct = compute_spread(bz_bid, bz_ask)

        # Calculate net profits after fees
        net_profits = compute_net_profits(hl_spread_pct, bz_spread_pct)

        rows.append({
            "coin": coin,
            "pair": p,
            "binance_symbol": symbol,
            "hl_bid": hl_bid,
            "hl_ask": hl_ask,
            "hl_spread_abs": hl_spread_abs,
            "hl_spread_pct": hl_spread_pct,
            "hl_funding_rate": hl_funding,
            "hl_maker_fee": HYPERLIQUID_FEES["maker"],
            "hl_taker_fee": HYPERLIQUID_FEES["taker"],
            "hl_net_profit": net_profits["hl_market_making"],
            "bz_bid": bz_bid,
            "bz_ask": bz_ask,
            "bz_spread_abs": bz_spread_abs,
            "bz_spread_pct": bz_spread_pct,
            "bz_maker_fee": BINANCE_FEES["futures_maker"],
            "bz_taker_fee": BINANCE_FEES["futures_taker"],
            "bz_net_profit": net_profits["bz_market_making"],
            "arbitrage_net": net_profits["arbitrage"],
        })

        processed += 1
        time.sleep(args.sleep)

    # Sort by Hyperliquid spread percentage descending
    rows_sorted = sorted(rows, key=lambda r: (r["hl_spread_pct"] or -1), reverse=True)

    # Print table header
    print("\nRanking by Hyperliquid spread % (descending):")
    print("=" * 150)
    print(f"{'#':>3}  {'Coin':<8}  {'HL %':>8}  {'HL Net':>8}  {'Funding %':>10}  {'BZ %':>8}  {'BZ Net':>8}  {'Arb Net':>8}")
    print("-" * 150)

    for i, r in enumerate(rows_sorted, 1):
        def fmt(x, digits=3):
            return f"{x:.{digits}f}" if isinstance(x, (int, float)) and x is not None else "-"
        def fmt_funding(x):
            return f"{x:.5f}" if isinstance(x, (int, float)) and x is not None else "-"
        print(
            f"{i:>3}  {r['coin']:<8}  {fmt(r['hl_spread_pct']):>8}  {fmt(r['hl_net_profit']):>8}  "
            f"{fmt_funding(r['hl_funding_rate']):>10}  {fmt(r['bz_spread_pct']):>8}  {fmt(r['bz_net_profit']):>8}  {fmt(r['arbitrage_net']):>8}"
        )

    # Save CSV for later review
    out_dir = os.path.dirname(os.path.abspath(__file__))
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(out_dir, f"rank_spreads_{ts}.csv")
    try:
        import csv
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "coin", "pair", "binance_symbol",
                "hl_bid", "hl_ask", "hl_spread_abs", "hl_spread_pct", "hl_funding_rate",
                "hl_maker_fee", "hl_taker_fee", "hl_net_profit",
                "bz_bid", "bz_ask", "bz_spread_abs", "bz_spread_pct",
                "bz_maker_fee", "bz_taker_fee", "bz_net_profit",
                "arbitrage_net"
            ])
            for r in rows_sorted:
                w.writerow([
                    r["coin"], r["pair"], r["binance_symbol"],
                    r["hl_bid"], r["hl_ask"], r["hl_spread_abs"], r["hl_spread_pct"], r["hl_funding_rate"],
                    r["hl_maker_fee"], r["hl_taker_fee"], r["hl_net_profit"],
                    r["bz_bid"], r["bz_ask"], r["bz_spread_abs"], r["bz_spread_pct"],
                    r["bz_maker_fee"], r["bz_taker_fee"], r["bz_net_profit"],
                    r["arbitrage_net"],
                ])
        print(f"\nSaved CSV: {out_path}")
    except Exception as e:
        print(f"WARN: Failed to write CSV: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
