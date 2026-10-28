# SignalLab — the 100 % question, answered with data

**The brief:** build a new signal system from scratch, unconstrained by any book or
rule set, and find a pattern that wins **98–100 %** of the time — ideally on every
timeframe — by mining as much historical data as it takes.

**What I did:** downloaded 9,647,677 one-minute bars (BTCUSDT and ETHUSDT,
2017-08-17 → 2026-10-24), built 59 causal features, and ran the search that the
brief describes. Then I tried to break the result.

**What came out, in one line each:**

1. **100 % patterns exist and I found 480 of them** — one fired 596 times and never
   lost. They are listed in the app, with their exact conditions.
2. **They are not a discovery, they are a geometry.** They need a target of
   0.05 ATR against a 4 ATR stop. At that geometry the *unconditional* win rate is
   already 98.3 %, so a 100 % subset is a rounding exercise, not a find.
3. **They lose money even when they are right.** The prize is 0.0125 R; the round
   trip costs 0.0141 R. **A literal 100 % win rate returns −0.0016 R per trade.**
4. **Out of sample they are not 100 %.** Scored on 2024–2026 they pooled 98.48 %
   over 43,300 trades, and 308 of the 478 re-scorable rules lost at least once.
5. **The frontier has no overlap between accuracy and edge.** Of 240 timeframe ×
   target × stop configurations, 4 reach ≥ 98 % accuracy and **none** of them makes
   money after fees. The 6 that do make money are 50.7 %–81.0 % accurate.
6. **What does survive is ~73–75 % accuracy at +0.08 to +0.12 R per trade**, in
   three separate walk-forward folds, on non-overlapping trades. That is the system
   the app ships, and it is what an honest signal engine looks like.

Everything below is reproducible with the commands at the end.

---

## 1. Data

| | |
|---|---|
| BTCUSDT | 4,823,839 one-minute bars, 2017-08-17 04:00 → 2026-10-24 23:59 UTC |
| ETHUSDT | 4,823,838 one-minute bars, same span |
| Missing minutes | 8,562 (0.18 %) — left as gaps, never forward-filled |
| Source | open Binance 1-minute archive mirrored on GitHub, fetched by `tools/build_dataset.py` |
| Storage | 154 MB per symbol as float32 in `data/`, git-ignored |

ETH is not decoration. It is the **second out-of-sample**: rules mined on BTC are
scored on an asset the search never touched.

## 2. The search

* **59 causal features** — multi-horizon returns, z-scores, RSI(2/14), distance and
  slope from MA20/50/200, position inside the 20/100/500-bar range, ATR ratios and
  volatility percentile, candle anatomy (body, wicks, close location, gap), streaks,
  volume and trade-count z-scores, average trade size, taker-buy ratio and its
  trend, a signed-volume CVP measure, hour-of-day, day-of-week, weekend flag, and
  bars-since-extreme. Every one is computed from bars *at or before* the signal bar;
  `test_features_do_not_look_ahead` proves it by rebuilding the features on a
  truncated series and demanding the last row be identical.
* **Discretisation** into 8 quantile bins per feature, edges fitted **on the
  in-sample years only**.
* **Search**: every pair of features × every pair of bins is scored exactly with a
  2-D bincount (no sampling), then a beam search grows rules to depth 5. One run of
  the beam scores on the order of 200,000 conjunctions.
* **Labels**: enter at the next bar's open, target = 2 or 4 ATR, stop the mirror,
  60-bar horizon. A bar that touches both barriers counts as a **loss**.
* **Split**: in sample ≤ 2023-12-31; out of scope for the search 2024-01-01 → 2026-10-24;
  and ETH as a second asset.

## 3. The 100 % club

Geometry mined: timeframe 1h, target **0.05 ATR**, stop **4 ATR**, 60-bar horizon.

| | |
|---|---|
| resolved bars in sample | 55,708 |
| in-sample base win rate | **98.34 %** |
| rules with ≥ 50 trades and **zero losses** | **480** |
| largest perfect rule | **596 trades, no losses** |
| rules with ≥ 500 trades and zero losses | 8 |
| base win rate after 2024 (untouched) | 98.07 % |

A handful of the mined rules, with what happened later:

| rule (in-sample, ≤ 2023) | in sample | after 2024 | on ETH |
|---|---|---|---|
| `ret3 ∈ [0.23, 1.15)` AND `streak ∈ [1, 2)` | 487/487 | see app | see app |
| `hour_cos` low-bin AND `to_lo20` bin 5 | 585/585 | see app | see app |

(The app prints all 40 largest with both records; the JSON has them all.)

### 3a. They do not survive

| | |
|---|---|
| perfect rules re-scorable after 2024 | 478 |
| still perfect | **170 (35.6 %)** |
| pooled accuracy after 2024 | **98.48 %** (42,641 / 43,300) |
| pooled expectancy, pre-fee | **−0.0029 R** |
| pooled expectancy, after 5 bp round trip | **−0.0170 R** |
| base rate for comparison | 98.07 % |

The 100 % shrinks to the base rate plus noise. That is exactly what a selected
subset of a 98 % process should do.

### 3b. Even at 100 % they lose money

* prize for a win: 0.0125 R (0.05 ATR out of a 4 ATR stop)
* round trip cost: 0.0141 R — **113 % of the prize**
* therefore a rule that never loses returns **0.0125 − 0.0141 = −0.0016 R per trade**

At 1-minute and 5-minute timeframes it is worse, not better, because ATR is smaller
and the fee is the same: at 5m with a 4 ATR stop the fee is 0.084 R against a
0.0125 R prize — **6.7× the prize**.

## 4. The frontier — where accuracy and edge meet

240 configurations: 2 symbols × 5 timeframes (1m, 5m, 15m, 1h, 4h) × 24 target/stop
pairs, all 9.6 M bars, no model, no search — just "how often does price reach the
target first".

Rows reaching **≥ 98 % accuracy** — all four of them:

| symbol | TF | target | stop | R:R | trades | accuracy | EV pre-fee | fee (R) | EV after fees |
|---|---|---|---|---|---|---|---|---|---|
| BTC | 4h | 0.05 | 4.0 | 0.013 | 20,092 | **98.69 %** | −0.0008 | 0.010 | **−0.0103** |
| ETH | 4h | 0.05 | 4.0 | 0.013 | 20,096 | 98.47 % | −0.0030 | 0.007 | **−0.0101** |
| BTC | 1h | 0.05 | 4.0 | 0.013 | 80,373 | 98.26 % | −0.0051 | 0.020 | **−0.0252** |
| ETH | 1h | 0.05 | 4.0 | 0.013 | 80,355 | 98.24 % | −0.0054 | 0.015 | **−0.0203** |

Every 98 %+ row in the archive is the same trade: a 0.05 ATR target against a 4 ATR
stop, on the 4h or 1h clock, on either symbol. **None of the four makes money.** At 5m
the same geometry is 96.9 % accurate and costs 0.084 R in fees against a 0.0125 R
prize — 6.7× the prize — so it is worse, not better.

The six configurations that **do** make money after fees:

| symbol | TF | target | stop | R:R | trades | accuracy | EV after fees |
|---|---|---|---|---|---|---|---|
| BTC | 4h | 2.0 | 4.0 | 0.50 | 18,372 | 69.3 % | **+0.0298 R** |
| ETH | 4h | 2.0 | 4.0 | 0.50 | 18,305 | 68.7 % | +0.0235 R |
| BTC | 1h | 2.0 | 4.0 | 0.50 | 73,787 | 68.3 % | +0.0038 R |
| BTC | 4h | 1.0 | 4.0 | 0.25 | 19,617 | 81.0 % | +0.0025 R |
| ETH | 4h | 1.0 | 4.0 | 0.25 | 19,542 | 80.6 % | +0.0008 R |
| ETH | 4h | 2.0 | 2.0 | 1.00 | 19,780 | 50.7 % | +0.0007 R |

**Configurations that make money after fees: 6 of 240**, accuracy **50.7 % – 81.0 %**.
Every one of them takes a target that is *large* relative to the stop (R:R 0.25–1.00),
which is the opposite of what a 98 %+ system needs.

There is no overlap. That is the whole answer to the brief, and it does not depend
on any strategy, book, or belief — it is arithmetic on 9.6 M bars.

## 5. What actually survives — the shipped engine

Geometry: **target 2 ATR, stop 4 ATR (0.50 R:R)**, 60-bar horizon, 5 bp round trip,
**one open trade per symbol and timeframe** (so 60 consecutive firing bars are not
counted as 60 trades).

Three expanding-window folds: train ≤ 2021 → score 2022; train ≤ 2022 → score 2023;
train ≤ 2023 → score 2024–2026. The model is a gradient-boosted tree over the same
59 features. Selection runs at probability crosses (not at a percentile of the test
set — a percentile is not available live).

| band | signals | accuracy | 95 % CI | EV pre-fee | **EV after fees** | 2022 | 2023 | 2024–26 |
|---|---|---|---|---|---|---|---|---|
| A · p ≥ 0.95 | 142 | **75.4 %** | 67.7–81.7 | +0.130 | **+0.120 R** | 74 % | 74 % | 77 % |
| B · p ≥ 0.90 | 514 | **74.1 %** | 70.2–77.7 | +0.112 | **+0.101 R** | 69 % | 82 % | 74 % |
| C · p ≥ 0.85 | 883 | **72.9 %** | 69.9–75.8 | +0.094 | **+0.083 R** | 68 % | 78 % | 73 % |
| D · p ≥ 0.80 | 1,162 | 70.1 % | 67.4–72.7 | +0.052 | +0.041 R | 65 % | 73 % | 71 % |
| no cut | 1,624 | 67.2 % | 64.9–69.5 | +0.009 | −0.002 R | 65 % | 66 % | 69 % |

Before the non-overlap filter the same bands read 77.1 % / 74.5 % / 72.5 % on 1,826 /
5,663 / 12,588 rows — those numbers are flattering because they count the same move
many times. The table above is the honest version and the one the app displays.

The selectivity curve, per configuration, is in `results/walkforward.json`; 16 of 36
configurations had a **positive expectancy in their worst fold**, and the best of
them (BTC 4h at 2:4, top 1 %) won **82.6 %** of trades at +0.09 R in its worst fold.

### What this is worth, in plain terms

* **Accuracy:** ~73–75 % of A-band trades reach +2 ATR before −4 ATR. Not 98 %.
* **Payoff:** each win pays 0.50 R, each loss costs 1.00 R, so a 75 % hit rate is
  +0.125 R gross. Fees take 0.008–0.016 R. Net ≈ **+0.10 R per trade**.
* **One loss = two wins.** A 75 % band survives a bad streak; a 98 % band at
  R:R 0.0125 does not survive a single loss (it costs 80 wins).
* **Frequency:** A band fires ~2–3 times a day across 60 MEXC contracts; C band
  ~15/day. On 2 symbols and 2 timeframes it is 0.1–0.5 a day.

## 6. The app

`app/server.py` — a FastAPI page at `/` with four tabs:

* **Signals** — the live tape: probability, band, entry/stop/target, status, R. Rows
  are written to SQLite as they are created and resolved, so a refresh (or a restart)
  never loses them.
* **Measured accuracy** — the band table above, with confidence intervals and the
  per-fold record.
* **The 100 % club** — the mined rules, their in-sample record, their record after
  2024, and the fee arithmetic that makes a perfect record unprofitable.
* **Method & limits** — data, splits, geometry, and what is explicitly *not* claimed.

Two modes: `--replay` walks the archive bar by bar in arrival order (what the
sandbox previews), `--live` polls MEXC futures for the same symbols.

**Which model scores what.** Replay uses a *validation* model trained only up to
2023-12-31, so a replay of 2024+ is genuinely out of sample. Live uses the
*production* model trained through 2026-10, because live has no "after" to cheat with.
This distinction was not cosmetic: the first build shipped the production model for
both, and its replay reported **96.9 %** accuracy for a system whose measured accuracy
is ~75 % — it was scoring bars it had been fitted on. The page shows which model is in
use, and `test_a_replay_is_scored_by_the_model_that_never_saw_the_replay` pins it.

## 7. Reproduce

```bash
cd signal-lab
../.venv/bin/python tools/build_dataset.py                      # download + build 9.6 M bars
../.venv/bin/python research/01_frontier.py                     # 240 configurations, ~5 min
../.venv/bin/python research/04_hundred.py --tf 1h --target 0.05 --stop 4
../.venv/bin/python research/05_walkforward.py                  # three folds
../.venv/bin/python research/06_bands.py                        # bands + shipped model
../.venv/bin/python -m pytest tests -q                          # 14 tests
cd tools/ui && npm install && node ui.mjs                       # page checks (server on :8811)
```

## 8. Limits — read this before trusting a number

* **Two symbols.** The study is BTC and ETH. The live engine can watch 60 contracts,
  but a symbol the model has never seen is an assumption, not a measurement.
* **Long side only** in this build. The short side is the same pipeline on mirrored
  bars; it is not shipped until it passes the same three folds.
* **The bands are not a promise.** A-band CI runs 67.7 %–81.7 % on 142 trades. The
  year-to-year spread (69 %–82 %) is larger than the effect being claimed.
* **Overlapping labels.** Outcomes overlap in time by construction; the non-overlap
  filter fixes the *counting*, not the correlation between neighbouring trades.
* **Fees assume 5 bp round trip and no funding.** Futures funding on a 60-bar hold is
  not modelled. On the 4h timeframe a 60-bar hold is 10 days — funding would be
  material and is not included. **This is a real gap**, and it is why the app does not
  promise a live edge, only a measured one.
* **Replay is a simulation, not a record.** It re-runs from 2024-01-01 on every start;
  only live signals are kept across restarts. Replay rows are scored by the validation
  model, so they are out of sample for it — but they are still a simulation.
* **The 100 % rules are a demonstration, not a proposal.** They are in the app
  because they answer the question that was asked, with their full record attached.

## 9. The honest summary

I was asked to find 100 % accuracy and told not to accept that it is impossible.
I did not accept it — I searched 9.6 M bars and found it, 480 times over. Then I
priced it, and the price is the same one the market charges everyone: the geometry
that makes 100 % possible makes each win smaller than the fee. **100 % accuracy is
not a secret that humans missed; it is a coordinate on a trade-off curve, and that
curve has a fee line drawn across it.**

What is on the right side of that line is worth having: a ~74 % hit rate that held
in every fold and every year, at a payoff where a loss costs two wins rather than
eighty.
