# Trade Analysis & Recommendations — Last 4 Days (Jun 12–15 2026)

Generated: 2026-06-15

## Overall Scorecard

| Side | Signals | Win Rate | Avg PnL | Profit Factor |
|------|---------|----------|---------|---------------|
| **Longs** | 323 | 41.6% | +0.08% | 1.14 ✅ |
| **Shorts** | 297 | 45.3% | **-0.29%** | **0.77 ❌** |

Longs are barely profitable. Shorts are losing money overall.

---

## Finding 1 — The Flip Exit Is Killing Long Performance

The `flip` exit is the dominant close reason (210/317 closes) but is the worst performer:

| Close Reason | Count | Win Rate | Avg PnL |
|---|---|---|---|
| `flip` | 210 | 36% | -0.14% |
| `stop` | 51 | 0% | -2.00% |
| `tp` | 48 | 100% | +3.00% |
| `horizon` | 8 | **100%** | **+1.35%** |

Positions that survive to the 4h horizon are 100% winners averaging +1.35%. The flip is triggering premature exits on positions that would have been winners. Hold time confirms this strongly:

| Hold Time | Win Rate | Avg PnL |
|---|---|---|
| `< 30 min` | 34% | -0.13% |
| `30m–1h` | 64% | +1.06% |
| `1–2h` | 65% | +0.94% |
| `2–4h` | 72% | +1.13% |

**Recommendation: Add a minimum 30-minute hold before the flip exit can trigger.** Trades need time to breathe — the flip signal is too noisy in the first 30 minutes.

---

## Finding 2 — XMR is Destroying the Shorts Side

XMR alone: **19 short trades, 2 wins (11%), avg -1.16%, total -22.10% loss.** It appears to be in a sustained uptrend that the screener keeps fading. FARTCOIN shorts are 0/4 at -2.08% avg. APT shorts are 29% wins at -1.56% avg.

Short losers to flag:

| Coin | Trades | Wins | Avg PnL | Total |
|---|---|---|---|---|
| **XMR** | 19 | 11% | -1.16% | -22.1% |
| **FARTCOIN** | 4 | 0% | -2.08% | -8.3% |
| **APT** | 7 | 29% | -1.56% | -10.9% |
| **ICP** | 5 | 0% | -1.13% | -5.7% |

**Recommendation: Blacklist XMR and FARTCOIN from shorts. These are trending coins the short screener should not be calling.**

Short winners doing well: WLD (7/7, 100%, +2.03%), VELVET (+2.89%), RAVE (+2.11%), BSB (+0.99%), INJ (+0.42%).

---

## Finding 3 — June 15 Shorts Collapsed (-2.15% avg)

PLAY was triple-stopped in one day (3 × -8% = -24% total from one coin). XMR had 7 losing short trades on the day. This points to a **market regime issue** — the screener fired short signals into a broad upswing.

The stop-loss at -8% for shorts means each stop wipes out ~5–6 horizon wins. With 13 stops across 4 days (4.5% stop rate), that's -104% in stops alone against a backdrop of modestly positive horizon closes.

**Recommendation: Consider a dynamic market regime gate for shorts** — if BTC or the broad market is in a strong uptrend (e.g., >1% in the last 2h), suppress new short signals.

---

## Finding 4 — June 15 16h Long Signal Flood

At 16:00 UTC on June 15, **32 long signals fired in a single hour** — the highest hourly volume by far — with only **19% win rate and -0.68% avg**. The screener appears to be triggering on a volatile market move and generating a large batch of low-quality signals.

**Recommendation: Rate-limit or quality-filter long signals when hourly signal count exceeds ~15–20. High-volume signal hours are significantly lower quality.**

---

## Finding 5 — Early Flag Has Zero Predictive Value

Early-flag trades: 41.7% wins, +0.07% avg. Normal trades: 41.4% wins, +0.10% avg. The `early` flag is not adding signal differentiation and could be deprioritised or removed from decision logic.

---

## Coin-Level Blocklist Recommendations

**Stop longing these (consistent losers):**
- **LIT** — 0 wins across 8 trades, -0.80% avg
- **TON** — 0 wins across 7 trades, -0.50% avg
- **CHIP** — 1/8 wins, -1.01% avg
- **PENGU** — 2/11 wins, -0.55% avg

**Keep longing these (consistent winners):**
- TAO, TIA, FARTCOIN, AAVE, SOL, ZRO, GRASS, PUMP, APT

**Stop shorting these:**
- XMR (blacklist), FARTCOIN, ICP, APT, JTO

**Keep shorting these:**
- WLD, VELVET, RAVE, BSB, HBAR, INJ, XLM

---

## Priority Action Items

1. **[High impact] Minimum 30-min hold before flip exits** — would convert many -0.14% flip losses into +1%+ wins
2. **[High impact] Blacklist XMR from shorts** — -22% cumulative drag, in a clear uptrend
3. **[Medium] Add coin-level blocklist for longs** — LIT, TON, CHIP are repeat losers
4. **[Medium] Market regime gate for shorts** — suppress shorts when BTC is ripping up
5. **[Low] Signal flood filter** — cap new long signals to ~15/hour to avoid low-quality bursts
