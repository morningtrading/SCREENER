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
MIN_SCORE = float(os.environ.get("ALERT_MIN_SCORE", "1.9"))
HOST = os.environ.get("SCREENER_HOST", "").strip()


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _qualifiers(data: dict, flag: str, score_key: str) -> dict:
    """Return {coin: score} for rows that are on the board and over threshold."""
    out = {}
    for r in data.get("rows", []):
        score = r.get(score_key)
        if r.get(flag) and isinstance(score, (int, float)) and score > MIN_SCORE:
            coin = r.get("coin")
            if coin:
                out[coin] = round(float(score), 2)
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


def _fmt(side: str, emoji: str, entries: dict, path: str) -> str:
    link = f"https://{HOST}{path}" if HOST else None
    head = f"{emoji} <b>New {side}</b> (score &gt; {MIN_SCORE:g})"
    if link:
        head = f'{head} — <a href="{link}">open</a>'
    lines = [f'• <a href="{_toobit_url(coin)}">{coin}</a> — {score:.2f}'
             for coin, score in sorted(entries.items(), key=lambda kv: -kv[1])]
    return head + "\n" + "\n".join(lines)


def main() -> None:
    if not BOT_TOKEN or not CHAT_IDS:
        print("[notify] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset — skipping.")
        return

    longs = _qualifiers(_load(MOMENTUM_FILE), "momentum", "score")
    shorts = _qualifiers(_load(SHORTS_FILE), "short", "short_score")

    state = _load(STATE_FILE)
    prev_long = set(state.get("long", []))
    prev_short = set(state.get("short", []))

    # First run (no state yet): seed silently so we don't blast the whole board.
    first_run = not STATE_FILE.exists()

    new_long = {c: s for c, s in longs.items() if c not in prev_long}
    new_short = {c: s for c, s in shorts.items() if c not in prev_short}

    if not first_run and (new_long or new_short):
        blocks = []
        if new_long:
            blocks.append(_fmt("LONG", "🟢", new_long, "/momentum"))
        if new_short:
            blocks.append(_fmt("SHORT", "🔴", new_short, "/shorts"))
        if _send("\n\n".join(blocks)):
            print(f"[notify] sent: {len(new_long)} long, {len(new_short)} short")
    elif first_run:
        print(f"[notify] first run — seeding state "
              f"({len(longs)} long, {len(shorts)} short), no alert sent.")

    STATE_FILE.write_text(json.dumps(
        {"long": sorted(longs), "short": sorted(shorts)}, indent=2))


if __name__ == "__main__":
    main()
