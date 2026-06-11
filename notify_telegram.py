#!/usr/bin/env python3
"""Telegram alert for coins newly entering the LONG / SHORT lists.

Runs after the rankings are rebuilt (see refresh_momentum.sh). It compares the
current set of qualifying coins against the set seen on the previous run and
pushes any *new* entrants to Telegram, so your phone makes a sound when a fresh
LONG or SHORT idea crosses the score threshold.

Qualifying =
  LONG  : momentum row with momentum==True and score        > ALERT_MIN_SCORE
  SHORT : shorts  row with short==True    and short_score    > ALERT_MIN_SCORE

Config (env / .env):
  TELEGRAM_BOT_TOKEN   bot token from @BotFather            (required)
  TELEGRAM_CHAT_ID     your chat id (e.g. via @userinfobot) (required)
  ALERT_MIN_SCORE      score threshold, default 1.9
  SCREENER_HOST        host shown in the dashboard links (optional)

If the token/chat id are unset, this script no-ops silently, so it is safe to
wire into the cron chain before you have created the bot.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

try:  # load .env when run standalone (refresh_momentum.sh already sources it)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
MOMENTUM_FILE = BASE / "momentum_ranking.json"
SHORTS_FILE = BASE / "shorts_ranking.json"
STATE_FILE = BASE / ".alert_state.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
# One or more recipients: a personal chat id and/or a group id (negative),
# separated by commas or spaces — every one gets the same alert.
CHAT_IDS = [c for c in os.environ.get("TELEGRAM_CHAT_ID", "").replace(",", " ").split() if c]
# Per-side score thresholds. Shorts score higher than longs, so they default
# apart; ALERT_MIN_SCORE sets the shared fallback for both.
_BASE_MIN = os.environ.get("ALERT_MIN_SCORE", "1.9")
LONG_MIN = float(os.environ.get("ALERT_MIN_SCORE_LONG", "1.3"))
SHORT_MIN = float(os.environ.get("ALERT_MIN_SCORE_SHORT", _BASE_MIN))
HOST = os.environ.get("SCREENER_HOST", "").strip()


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _eval_cfg() -> dict:
    """The eval/exit policy from config.json (stop/take levels + horizon), so the alert's
    SL/TP suggestion matches exactly how the track record is scored. Empty dict if unreadable."""
    cfg_path = Path(os.environ.get("SCREENER_CONFIG", str(BASE / "config.json")))
    try:
        return json.loads(cfg_path.read_text()).get("eval", {})
    except Exception:
        return {}


def _side_pct(cfg: dict, key: str, side: str):
    """A per-side % level (stop_loss_pct / take_profit_pct), accepting a bare number (both
    sides) or a {"long":.., "short":..} mapping, mirroring build_eval. None if unset."""
    v = cfg.get(key)
    return v.get(side) if isinstance(v, dict) else v


def _fmt_price(p) -> str:
    """Adaptive price formatting (~4 significant figures, no sci-notation) across the huge
    range of perp prices, from sub-cent meme coins to four-figure majors."""
    if not isinstance(p, (int, float)) or p <= 0:
        return "?"
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:.4g}"
    import math
    decimals = min(max(3 - math.floor(math.log10(p)), 2), 10)
    return f"{p:.{decimals}f}"


def _level_price(entry, side: str, pct):
    """Price that yields `pct` % P&L for `side` (the inverse of build_eval.pnl_of), or None
    if pct is None. long: entry*(1+pct/100); short: entry/(1+pct/100)."""
    if pct is None or not isinstance(entry, (int, float)) or entry <= 0:
        return None
    return entry * (1 + pct / 100.0) if side == "long" else entry / (1 + pct / 100.0)


def _qualifiers(data: dict, flag: str, score_key: str, min_score: float) -> dict:
    """Return {coin: {"score":.., "price":..}} for rows on the board and over threshold."""
    out = {}
    for r in data.get("rows", []):
        score = r.get(score_key)
        if r.get(flag) and isinstance(score, (int, float)) and score > min_score:
            coin = r.get("coin")
            if coin:
                out[coin] = {"score": round(float(score), 2), "price": r.get("price")}
    return out


def _send_one(chat_id: str, text: str, retries: int = 4) -> bool:
    # Telegram connectivity from the VPS is occasionally flaky (timeouts), so
    # retry with backoff rather than dropping the alert on a single hiccup.
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                return True
            print(f"[notify] {chat_id} error {resp.status_code}: {resp.text[:200]}")
            return False  # an API rejection won't fix itself on retry
        except Exception as exc:  # network hiccup — back off and retry
            print(f"[notify] {chat_id} attempt {attempt}/{retries} failed: {type(exc).__name__}")
            if attempt < retries:
                time.sleep(5 * attempt)
    return False


def _send(text: str) -> bool:
    # Deliver to every configured recipient (personal chat and/or group).
    # Returns True if at least one delivery succeeded.
    ok = False
    for chat_id in CHAT_IDS:
        ok = _send_one(chat_id, text) or ok
    return ok


def _toobit_url(coin: str) -> str:
    # Toobit perpetual futures page, e.g. SIREN -> .../futures/SIREN-SWAP-USDT
    return f"https://www.toobit.com/en-US/futures/{coin}-SWAP-USDT"


def _fmt(label: str, side: str, emoji: str, entries: dict, path: str,
         min_score: float, ecfg: dict) -> str:
    """One alert block for a side. Each line carries the entry price plus suggested SL/TP
    levels derived from the eval exit policy, so the alert matches the scored track record.
    A side with no configured TP (shorts ride to the horizon) shows the horizon instead."""
    link = f"https://{HOST}{path}" if HOST else None
    sl_pct = _side_pct(ecfg, "stop_loss_pct", side)
    tp_pct = _side_pct(ecfg, "take_profit_pct", side)
    horizon = ecfg.get("horizon_hours")
    # policy summary in the header
    sl_txt = f"SL {sl_pct:g}%" if sl_pct is not None else "SL —"
    tp_txt = (f"TP +{tp_pct:g}%" if tp_pct is not None
              else (f"TP ride {horizon:g}h" if horizon else "TP open"))
    head = f"{emoji} <b>New {label}</b> (score &gt; {min_score:g}) · {sl_txt} / {tp_txt}"
    if link:
        head = f'{head} — <a href="{link}">open</a>'

    lines = []
    for coin, info in sorted(entries.items(), key=lambda kv: -kv[1]["score"]):
        price = info.get("price")
        bits = [f'• <a href="{_toobit_url(coin)}">{coin}</a> <b>{info["score"]:.2f}</b>']
        if isinstance(price, (int, float)) and price > 0:
            bits.append(f"entry {_fmt_price(price)}")
            sl = _level_price(price, side, sl_pct)
            tp = _level_price(price, side, tp_pct)
            if sl is not None:
                bits.append(f"SL {_fmt_price(sl)}")
            bits.append(f"TP {_fmt_price(tp)}" if tp is not None
                        else (f"TP {horizon:g}h" if horizon else "TP open"))
        lines.append("  ·  ".join(bits))
    return head + "\n" + "\n".join(lines)


def main() -> None:
    if not BOT_TOKEN or not CHAT_IDS:
        print("[notify] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset — skipping.")
        return

    longs = _qualifiers(_load(MOMENTUM_FILE), "momentum", "score", LONG_MIN)
    shorts = _qualifiers(_load(SHORTS_FILE), "short", "short_score", SHORT_MIN)

    state = _load(STATE_FILE)
    prev_long = set(state.get("long", []))
    prev_short = set(state.get("short", []))

    # First run (no state yet): seed silently so we don't blast the whole board.
    first_run = not STATE_FILE.exists()

    new_long = {c: s for c, s in longs.items() if c not in prev_long}
    new_short = {c: s for c, s in shorts.items() if c not in prev_short}

    if not first_run and (new_long or new_short):
        ecfg = _eval_cfg()
        blocks = []
        if new_long:
            blocks.append(_fmt("LONG", "long", "🟢", new_long, "/momentum", LONG_MIN, ecfg))
        if new_short:
            blocks.append(_fmt("SHORT", "short", "🔴", new_short, "/shorts", SHORT_MIN, ecfg))
        if _send("\n\n".join(blocks)):
            print(f"[notify] sent: {len(new_long)} long, {len(new_short)} short")
    elif first_run:
        print(f"[notify] first run — seeding state "
              f"({len(longs)} long, {len(shorts)} short), no alert sent.")

    STATE_FILE.write_text(json.dumps(
        {"long": sorted(longs), "short": sorted(shorts)}, indent=2))


if __name__ == "__main__":
    main()
