#!/usr/bin/env python3
"""
Evaluate past MOMENTUM (long) picks vs price — "were we right?" (CLI view of /results).

Reuses build_eval's exit policy so the CLI and the /results page agree: each pick is an OPEN
position only while the screener keeps re-flagging it, then it SETTLES (P&L frozen at the close
price) at the earlier of the horizon or momentum flipping off. Open picks track to the live
price. Prints the open board and the settled track record, with an overall hit-rate for each.

Usage:
  python3 eval_momentum.py                     # longs (open + settled)
  python3 eval_momentum.py --side short        # shorts
  python3 eval_momentum.py --min-age-hours 1   # hide OPEN picks younger than N hours
"""
import argparse
from datetime import datetime, timezone

import build_eval as be


def _print_group(title, g, settled):
    rows = g.get("rows", [])
    n = g.get("count", 0)
    if not rows:
        print(f"\n{title}: none")
        return
    print(f"\n{title}: {g['wins']}/{n} right = {g['wins'] / n * 100:.0f}% · avg {g['avg']:+.2f}%")
    tcol = "held(h)" if settled else "age(h)"
    pcol = "exit" if settled else "now"
    print(f"{'coin':<10}{tcol:>8}{'entry':>13}{pcol:>13}{'P&L%':>10}  {'why' if settled else 'tag'}")
    for r in rows:
        t = r.get("held_hours" if settled else "age_hours", 0.0)
        if settled:
            tag = "4h-horizon" if r.get("close_reason") == "horizon" else "mom-off"
        else:
            tag = "early" if r.get("extra") == "early" else ""
        print(f"{r['coin']:<10}{t:>8.1f}{r['entry']:>13.6g}{r['now']:>13.6g}{r['pnl']:>+10.2f}  {tag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--min-age-hours", type=float, default=0.0,
                    help="hide OPEN picks younger than this many hours")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    h, g = be.ECFG["horizon_hours"], be.ECFG["flip_grace_min"] * 60.0
    path = be.LONGS if args.side == "long" else be.SHORTS
    res = be.evaluate(path, args.side, now, h, g, be.flip_min_hold_for(args.side))
    if args.min_age_hours:
        res["rows"] = [r for r in res["rows"] if r["age_hours"] >= args.min_age_hours]
        res["count"] = len(res["rows"])
        res["wins"] = sum(1 for r in res["rows"] if r["pnl"] > 0)
        res["avg"] = round(sum(r["pnl"] for r in res["rows"]) / len(res["rows"]), 2) if res["rows"] else 0.0
    _print_group(f"OPEN {args.side} picks (horizon {h:g}h)", res, settled=False)
    _print_group(f"SETTLED {args.side} picks", res["settled"], settled=True)


if __name__ == "__main__":
    main()
