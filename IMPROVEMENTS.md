# Screener Improvement Roadmap

Generated after the 2026-06-11 overnight analysis session. Based on backtests over the full 3-day short history (575 settled positions, build_eval engine, real 15m klines).

## Key findings this session
- `short_score` is **anti-predictive** (r=−0.13): Q1 lowest-score = 74% WR +1.39% avg; Q4 highest-score = 44% WR +0.13% avg
- Score weights 1h:0.5/2h:0.3 on both inverted TFs (exhaustion proxies); 4h:0.2 is the only predictive TF (r=+0.31)
- `accel_contrib` is the best predictor (r=+0.38) but underweighted
- `buy_ratio` (r=−0.29) is strong and unused in the score
- RSI floor raised 25→40→42 this session (commit c4f03b8); backtested 62%→74% WR
- BTC regime gate **refuted**: shorts are best in risk-on (70% WR); only loss zone is BTC down >1%/3h (n=75, 44% WR)
- `rvol_min: 1.8` is uncalibrated (3/91 picks reach it); noise floor is rvol < 0.5 (40% WR, −0.44% avg)
- All eval P&L is **gross** — fees + funding not subtracted

---

## P1 — Fix the inverted score core
**Status:** not started — needs shadow-score phase first (selection bias risk)

Reweight weakness_tf toward 4h, boost accel_contrib, add buy_ratio. Cannot flip live without shadow-testing because the backtest only sees picks the current filter allowed.

> **Mini prompt:** *"In build_shorts.py, add a `shadow_score` computed alongside `short_score` with reweighted components: weakness_tf weights 1h:0.15/2h:0.15/4h:0.5, accel weight ×4, and buy_ratio added as a new term (lower=better, weight ~0.3). Log it into short_picks.jsonl next to the existing components — zero behavior change. After 5–7 days, backtest: would ranking/alerting by shadow_score have beaten short_score on settled P&L? Use the build_eval engine for outcomes."*

---

## P2 — Log rejected candidates (kill selection bias)
**Status:** not started — highest-urgency logging task (clock starts when deployed)

Every current backtest studies only trades the filter *allowed*. Logging near-misses makes all future studies trustworthy and is required to safely validate P1.

> **Mini prompt:** *"In build_shorts.py and build_momentum.py, log REJECTED candidates that came within 30% of qualifying (score ≥ 0.7×min_score, or excluded only by reversal_risk/RSI) into a separate rejected_picks.jsonl with the same fields + rejection reason. Cap file size with the same picks_keep rotation. This is info-only; no behavior change. Goal: future backtests can evaluate filter changes against the trades they would have added, not just removed."*

---

## P3 — Net out fees + funding in eval
**Status:** not started — all current numbers are gross; real edge may be ~half

Average settled short = +0.68%; round-trip taker fee ~0.04–0.1%; shorts hold 4h through funding. Can't trust tuning decisions on gross P&L.

> **Mini prompt:** *"In build_eval.py, add config keys eval.fee_pct_roundtrip (default 0.1) and eval.apply_funding (bool). Subtract the fee from every settled pnl, and for shorts estimate funding paid over held_hours from the funding rate logged at entry (funding × held_hours/8h interval × 100). Show gross and net in eval_results.json rows; dashboards display net. Re-run the 3-day track record and report how much of the +388% survives."*

---

## P4 — rvol floor at 0.5 + min volume floor
**Status:** not started — evidence in hand, safe to ship

`rvol_min: 1.8` only adds a reason, never rejects. rvol < 0.5 bucket: 40% WR, −0.44% avg (n=25). Shorts also have no notional volume floor; longs require 2M USDT.

> **Mini prompt:** *"In build_shorts.py, make rvol a hard qualification gate: reject short candidates with rvol < shorts.rvol_floor (new config key, default 0.5), keeping rvol_min 1.8 as the existing confirmation-reason threshold. Log rejected ones to rejected_picks.jsonl. Also add a min 24h-volume floor for shorts (shorts.min_volume_usdt, default 2000000, same as longs' broad_min_volume_usdt)."*

---

## P5 — Walk-forward config discipline
**Status:** not started — process improvement, no code risk

RSI changed 25→40→42 in three days, each tuned on the same window that proposed it. Protection: never judge a change on the data that proposed it.

> **Mini prompt:** *"Create a CHANGELOG_TUNING.md and a small eval_compare.py: given a config change date, it splits eval history into before/after and reports WR/avg/total per side for each period, flagging changes whose post-deployment performance dropped >30% vs the backtest that justified them. Add a cron line appending a daily one-line summary (date, side, settled N, WR, net avg) to stats_daily.csv so trend regressions are visible without re-running backtests."*

---

## P6 — Log BTC 3h ROC for dump-suppressor study
**Status:** not started — logging only, clock starts when deployed

BTC down >1%/3h → 44% WR, −0.30% avg (n=75, coherent mechanism: shorting into capitulation bounce). Needs ≥2 weeks more data before gating.

> **Mini prompt:** *"In build_shorts.py, compute BTC's 3h ROC at scan time (reuse bm.fetch_klines) and log it as btc_roc_3h in every short_picks.jsonl record. No behavior change. After ≥2 weeks, backtest a soft suppressor: raise effective min_score by shorts.btc_dump_penalty when btc_roc_3h < −1.0, and report WR/avg/total kept-vs-removed using the build_eval engine."*

---

## P7 — Short exit policy sweep
**Status:** not started — backtestable today with the cached-kline harness

Shorts rode 4h with no TP last night: 35/43 bled to horizon (−94% total). A trailing stop or wide TP may bank the move before melt-up reversals while keeping the winners' tail.

> **Mini prompt:** *"Using the cached-series backtest harness pattern (monkeypatch build_eval.kline_ohlc with a per-coin full-series cache), sweep short exit policies on the full pick history: horizon 2/3/4h × TP none/+3/+5% × trailing stop 3/4/5% from peak P&L. Report N/WR/avg/total per cell vs the current (4h, no TP, −8% SL). Watch for the policy that cuts worst-night drawdown most while keeping ≥85% of total P&L."*

---

## P8 — Long side score decomposition
**Status:** not started — cheap insurance, do when convenient

Longs look healthy but the same audit that found the shorts score inverted has never been run on longs.

> **Mini prompt:** *"Run the same score-component decomposition on the LONG side: join settled long positions from eval_results/momentum history to momentum_picks.jsonl entries, compute Pearson r and quartile WR/avg-P&L for score, roc per TF, accel_1h, buy_ratio, rvol, oi_change. Flag any anti-predictive component. Use the bt4.py pattern from the jobs tmp dir as the template."*

---

## Sequencing note

| When | Items |
|---|---|
| This week (logging, clock starts) | P2, P6 |
| This week (safe, small) | P3, P4 |
| Next week (shadow phase) | P1 — log shadow_score, don't flip yet |
| Ongoing | P5 — every time config changes |
| When convenient | P7 (backtest), P8 (backtest) |

**Meta-risk:** 3 days of data + gross P&L eval are making every tuning decision look more certain than it is. P2 + P3 + P5 address this directly.
