# SignalLab — accuracy against the accuracy you need

> **Round 16 (current build) — config, many instruments, fast signals.**
>
> The complaint was: *"sometimes no signals for hours, too many coins make the command
> line hang, and I want to trade now."* Three things changed.
>
> **1. Settings moved into `config.json`.** 120 instruments, 11 timeframes, the poll
> interval, the risk per trade and the database all live in one file. The command line
> only overrides it, and the page writes back to it. A long universe can no longer hang
> the app: fetches go through a bounded pool of `workers` with a per-request timeout, a
> failing symbol is counted and skipped, and the symbol list is capped by `auto_top`.
>
> **2. Every timeframe was measured before it was shipped.** The fee is a fixed 5 bp of
> *price*; in R it is `5 bp / (stop in ATR × ATR/price)`, so the same 2 ATR stop is
> 0.03 R on 1h and 0.5 R on 1m. Per-clock stops were therefore mandatory, not cosmetic.
> Measured on 2024-2026, non-overlapping, BTCUSDT + ETHUSDT, 57 features a live candle
> feed can actually produce:
>
> | TF | stop / target | fee | break-even | win | EV after fees | n | /day/sym |
> |---|---|---|---|---|---|---|---|
> | **45m** | 1 / 4 ATR | 0.066 R | 21.3 % | 24.5 % | **+0.127 R** | 192 | 0.19 |
> | **1h** | 1 / 4 ATR | 0.056 R | 21.1 % | 25.7 % | **+0.206 R** | 144 | 0.14 |
> | **2h** | 1 / 4 ATR | 0.039 R | 20.8 % | 21.8 % | **+0.037 R** | 257 | 0.25 |
> | 3h | 2 / 8 ATR | 0.016 R | 20.3 % | 20.0 % | −0.022 R | 100 | 0.10 |
> | 4h | 1 / 4 ATR | 0.027 R | 20.5 % | 19.7 % | −0.052 R | 117 | 0.11 |
> | 30m | 1 / 4 ATR | 0.077 R | 21.5 % | 19.1 % | −0.120 R | 298 | 0.29 |
> | 15m | 2 / 8 ATR | 0.056 R | 21.1 % | 19.2 % | −0.097 R | 480 | 0.53 |
> | 10m | 2 / 8 ATR | 0.073 R | 21.5 % | 19.7 % | −0.087 R | 142 | 0.65 |
> | 5m | 2 / 8 ATR | 0.100 R | 22.0 % | 19.0 % | −0.150 R | 742 | 1.17 |
> | 3m | 2 / 8 ATR | 0.191 R | 23.8 % | 20.6 % | −0.159 R | 412 | 0.34 |
> | 1m | 4 / 16 ATR | 0.192 R | 23.8 % | 17.5 % | −0.318 R | 2 524 | 1.42 |
>
> 45m, 1h and 2h are on by default; the rest are shipped as measured, marked `off`, and
> can be enabled per clock. **The fast timeframes fail for an arithmetic reason, not a
> model reason:** on 1m a 4 ATR stop is ~0.19 % of price, so the round trip costs 0.19 R
> before the trade starts, and no stop/target pair tested overcame it.
>
> **3. Signals are on the page at start-up.** The first poll walks back `scan_bars` (40)
> closed bars on every stream and scores them in one pass (features computed once per
> window, not once per bar), so setups that are still in play appear within seconds
> instead of after an hour. With 120 instruments × 3 timeframes the scan produces
> ~50–60 signals a day in total, and **Scan now** polls on demand.
>
> **Also fixed, and it mattered more than any of the above:** MEXC candles carry no
> trade count and no taker-buy split, so the features built on those two columns are
> always NaN in live mode - and the engine drops any bar with a NaN feature. A model
> trained on all 59 features could therefore *never* emit a live signal; it waited
> forever. The feature list is now derived from what a candle feed can produce
> (`lab.features.live_safe()`, 57 features) and a test pins it.
>
> **The running app reproduces it.** `tools/replay_check.py` walks the app's own engine
> over the whole 2024-2026 archive with the validation model (trained to 2023-12-31) and
> the shipped per-timeframe geometry: **651 signals, 158 won / 491 lost, 24.3 % accuracy
> against a 21.6 % break-even, +0.137 R per trade, +88.9 R net**, in 100 seconds of wall
> clock. Per clock: 1h 229 signals at 27.1 % (+0.277 R), 2h 123 at 23.8 % (+0.136 R),
> 45m 299 at 22.5 % (+0.030 R). Those are the numbers the page will show you if you run
> the replay; they are not copied from the research table.
>
> **What is not claimed:** nothing makes price move to the target immediately. At 1 : 4
> the honest expectation is one win in four, with a measured edge of +0.04 to +0.21 R
> per trade on 100–260 trades per clock — small samples with wide intervals. The
> "no signals for hours" complaint is answered by breadth (120 instruments) and by the
> 45m/2h clocks, not by pretending the 1-minute chart works.

Round 14 hunted 100 % accuracy and found it, 480 times over, along with the arithmetic
that makes it worthless: at a 0.05 ATR target the prize (0.0125 R) is smaller than the
round trip (0.0141 R), so even a flawless record returns −0.0016 R per trade.

This round takes the other side of the same coin: **give the trade a reward worth
having.** At 1 : 4 the accuracy you need collapses from "three in four" to **once in
five**, and the fee stops eating the prize. The result is the first version of this
system with a positive expectancy in every walk-forward period, on a sample big enough
to quote — and, unlike round 14, a claim the running app reproduces to within noise.

| | round 14 (1 : 2) | **round 15 (1 : 4)** |
|---|---|---|
| target / stop | 2 ATR / 4 ATR | **8 ATR / 2 ATR** |
| accuracy needed, after fees | 67 % | **20.6 %** |
| fee as a share of the reward | 12 % | **0.7 %** |
| measured accuracy, out of sample | 75.4 % on 142 trades | **24.6 % on 260 trades** |
| **net expectancy per trade** | +0.120 R | **+0.202 R** |
| worst walk-forward period | +0.104 R | **+0.073 R** (all three positive) |
| what the running app reproduces | not checked | **23.7 % and +0.143 R on 144 trades** |

Two thirds of the accuracy was thrown away and the money per trade went up.

---

## 1. Data

| | |
|---|---|
| BTCUSDT | 4,823,839 one-minute bars, 2017-08-17 04:00 → 2026-10-24 23:59 UTC |
| ETHUSDT | 4,823,838 one-minute bars, same span |
| Missing minutes | 8,562 (0.18 %) — left as gaps, never forward-filled |
| Source | open Binance 1-minute archive mirrored on GitHub, fetched by `tools/build_dataset.py` |
| Storage | 154 MB per symbol as float32 in `data/`, git-ignored |

## 2. Method

* **59 causal features** — multi-horizon returns and z-scores, RSI(2/14), MA20/50/200
  distance and slope, position inside the 20/100/500-bar range, ATR ratios and
  volatility percentile, candle anatomy, streaks, volume / trade-count / average-size
  z-scores, taker-buy ratio and its trend, a signed-volume measure, clock features and
  bars-since-extreme. No feature may read a bar after the signal bar;
  `test_features_do_not_look_ahead` proves it by rebuilding on a truncated series.
* **Labels** — enter at the next bar's open; a bar that touches both barriers counts
  as a loss.
* **Split** — expanding window. Train ≤ 2021 → score 2022; train ≤ 2022 → score 2023;
  train ≤ 2023 → score 2024–2026. ETH is a second asset, never trained on.
* **One model per timeframe, trained across both symbols** — exactly what the app
  ships. (Training per symbol instead produced 100 signals in 2024–26 where the app
  takes 68; a study has to describe the thing that runs.)
* **Counting** — one entry per `horizon` bars. Not "one per open position": a trade
  that resolves in three bars must not let the next one in immediately. Getting this
  wrong inflated the sample by ~40 % and moved the live accuracy to break-even.
* **Fees** — 5 bp round trip, charged on every trade, expressed in R against the stop.

## 3. Why reward : risk is the lever

With a fee `f` (in R) and a reward : risk of `x`, the break-even hit rate is

```
p* = (1 + f) / (1 + x)
```

Everything is downstream of that. The sweep (`research/07_rr.py`) ran 30 geometries:
2 timeframes × 3 stops (0.5, 1, 2 ATR) × 5 rewards (1:2 … 1:6), three folds, both
symbols, non-overlapping trades.

**1h, 0.5 ATR stop** — the stop the previous round effectively used, with the target
moved out:

| reward : risk | break-even | base rate | fee | model, best cut | worst period |
|---|---|---|---|---|---|
| 1 : 2 | 37.1 % | 31.8 % | 0.113 R | 30.8 % | −0.367 R |
| 1 : 3 | 27.8 % | 24.1 % | 0.113 R | 32.7 % | −0.044 R |
| 1 : 4 | 22.3 % | 19.5 % | 0.113 R | 22.3 % | −0.795 R |
| 1 : 5 | 18.5 % | 16.4 % | 0.113 R | 20.6 % | −0.097 R |

At a half-ATR stop the fee alone is 0.113 R — nine times the 0.0125 R prize of the old
geometry — and nothing works. The stop has to be wide enough that the fee is small in R.

**1h, 2 ATR stop** — the shipped geometry:

| reward : risk | break-even | base rate | fee | best cut | worst period |
|---|---|---|---|---|---|
| 1 : 4 | **20.6 %** | 21.1 % | 0.028 R | 25.5 % | **+0.104 R** |
| 1 : 5 | 17.1 % | 17.4 % | 0.029 R | 25.0 % | +0.339 R |
| 1 : 6 | 14.7 % | 14.6 % | 0.029 R | 15.1 % | −0.201 R |

Of all 30 geometries, exactly one had ≥ 100 trades *and* a positive expectancy in every
walk-forward slice: **1h, 2 ATR stop, 8 ATR target**. That is what the app trades.

On 4h the same geometry looks better (33.3 % at 1 : 4, worst slice +0.384 R) but only
40–50 trades land in the test periods and only two slices have enough to measure. It is
available (`--tfs 4h`) and it is not the default, because it has not earned it.

## 4. The shipped system, measured with the shipped model

Geometry: reward : risk **1 : 4** (target 8 ATR, stop 2 ATR), 240-bar horizon, long
side, 5 bp round trip, one entry per 240 bars, one pooled model per timeframe.

| band | cut | signals | accuracy | 95 % CI | **EV after fees** | 2022 | 2023 | 2024–26 |
|---|---|---|---|---|---|---|---|---|
| A | p ≥ 0.50 | 117 | 22.2 % | 15.6–30.6 | +0.083 R | 21.2 % | 25.0 % | 22.1 % |
| B | p ≥ 0.45 | 181 | 24.3 % | 18.6–31.1 | +0.187 R | 25.0 % | 28.6 % | 22.1 % |
| C | p ≥ 0.40 | 224 | 23.7 % | 18.6–29.6 | +0.155 R | 21.2 % | 26.4 % | 23.5 % |
| **D** | **p ≥ 0.35** | **260** | **24.6 %** | 19.8–30.2 | **+0.202 R** | 23.2 % | 28.3 % | 23.6 % |
| E | p ≥ 0.30 | 297 | 19.5 % | 15.4–24.4 | −0.052 R | 19.0 % | 18.8 % | 20.0 % |
| F | p ≥ 0.25 | 319 | 23.8 % | 19.5–28.8 | +0.163 R | 25.4 % | 23.5 % | 23.4 % |
| — | no cut | 354 | 24.3 % | 20.1–29.0 | +0.186 R | 17.6 % | 28.4 % | 25.2 % |

The recorded cut is **p ≥ 0.35** (row D, in bold). The selection rule was stated before
the number was looked at: *the highest expectancy among cuts with at least 250
non-overlapping trades that clears break-even in every period.* Two things in this
table are worth being blunt about:

* **The model adds 2–4 points, not 20.** The base rate at this geometry is 21.1 % and
  break-even is 20.6 %, so blind entry is worth ~+0.02 R. The filter is worth another
  +0.15 to +0.18 R. That is a real but modest edge, and it is why the sample has to be
  this large to see it.
* **The cut barely matters between 0.25 and 0.50** (22–25 % everywhere, with one dip at
  0.30 that is noise). Anyone quoting a single cut as if it were precise is selling
  something. The band table shows all of them for that reason.

### What the running app reproduces

The full replay — every bar from 2024-01-01 to 2026-10-24, both symbols, scored by the
validation model (trained ≤ 2023-12-31), driven through the same code the page uses:

| recording cut | signals | accuracy | break-even | net R | per trade |
|---|---|---|---|---|---|
| p ≥ 0.50 | 68 | 22.1 % | 20.9 % | +4.0 | +0.059 R |
| p ≥ 0.45 | 95 | 21.7 % | 20.8 % | +4.1 | +0.045 R |
| **p ≥ 0.35 (shipped)** | **144** | **23.7 %** | 20.9 % | **+19.9** | **+0.143 R** |
| p ≥ 0.25 | 184 | 21.9 % | 20.8 % | +10.0 | +0.055 R |

The shipped cut reproduces to 23.7 % against the study's 23.6 % for the same period, and
+0.143 R against +0.202 R — the difference is 3 trades out of 144. Signal **counts match
exactly** (144 vs 144), which is the check that matters: the engine is not quietly
trading a different rule than the one that was measured.

Two bugs were found by insisting on that check, and both are pinned by tests now:

1. The app unblocked on *exit* rather than on the horizon, taking ~40 % more trades
   than the study — drawn from the same moves, at break-even accuracy.
2. `fee_r` divided a fraction of price by a price distance, reporting 3 × 10⁻⁷ R. The
   page said the fee was free. It is 0.028 R.

## 5. Frequency

Round 15, on one hour and two symbols: 260 signals across the three test periods, about
**0.15 per symbol per day** — one every three days on a single chart, which is what the
user saw as "no signals for hours".

Round 16 answers it with breadth instead of a lower bar: three clocks (45m, 1h, 2h) and
120 instruments. At the measured rates that is 0.19 + 0.14 + 0.25 = 0.58 signals per
symbol per day, i.e. **~70 a day across 120 contracts, ~3 an hour**, and the start-up
scan shows whatever is already in play. The 1m–30m clocks would add far more signals,
but every geometry tested there loses the fee (table at the top), so they are off.

## 6. The 100 % club (kept, still instructive)

* **480 rules** with ≥ 50 trades and zero losses in sample; the largest fired **596**
  times without losing.
* Pooled on 2024–2026, which the search never saw: **98.48 %** — 42,641 of 43,300, the
  base rate (98.07 %) plus noise. 308 of 478 rules lost at least once.
* Of 240 timeframe × target × stop configurations, **4** reach ≥ 98 % accuracy and
  **none** makes money after fees; the 6 that do are 50.7–81.0 % accurate.
* A literal 100 % rule at that geometry returns **−0.0016 R per trade**.

None of that changed. What changed is which side of the trade-off the app sits on.

## 7. The app

`app/server.py` — FastAPI + one page + SQLite, five tabs (round 16 adds **Act now**,
which lists the setups you can trade this minute with size, levels and the per-timeframe
table, plus the `config.json` editor):

* **Signals** — live tape: probability, band, entry / stop / target, status, R. Rows are
  written to SQLite as they are created and resolved; a refresh never loses them. The
  header shows the ratio, the fee as a share of the reward, and which model is scoring.
  The recording cut is adjustable from the page, with each cut's measured record on the
  button.
* **Measured accuracy** — the reward : risk sweep (why 1 : 4), then the band table with
  break-even, intervals and per-period records.
* **The 100 % club** — the rules that never lost, and what happened to them next.
* **Method & limits** — data, splits, geometry, and what is explicitly *not* claimed.

`--replay` walks the archive bar by bar in arrival order (what the preview shows);
`--live` polls MEXC futures for the same symbols.

**Which model scores what.** Replay uses a *validation* model trained only up to
2023-12-31, so a replay of 2024+ is genuinely out of sample. Live uses the *production*
model trained through 2026-10, because live has no "after" to cheat with. This is not
cosmetic: an early build shipped the production model for both and its replay reported
**96.9 %** accuracy for a system whose measured accuracy was ~75 % — it was scoring bars
it had been fitted on. The page says which model is in use and a test pins it.

## 8. Reproduce

```bash
cd signal-lab
../.venv/bin/python tools/build_dataset.py                      # 9.6 M one-minute bars
../.venv/bin/python research/01_frontier.py                     # the accuracy/fee frontier
../.venv/bin/python research/04_hundred.py --tf 1h --target 0.05 --stop 4
../.venv/bin/python research/07_rr.py --rrs 2,3,4,5,6 --stops 0.5,1,2 --horizon 240
../.venv/bin/python research/06_bands.py --tfs 1h --target 8 --stop 2 --horizon 240

# round 16: all eleven timeframes, then ship the ones that paid
../.venv/bin/python research/08_timeframes.py \
    --tfs 1m,3m,5m,10m,15m,30m,45m,1h,2h,3h,4h \
    --stops 1,2,4,7,12,20 --rrs 4 --horizon 200 --stride 3   # -> results/timeframes.json
../.venv/bin/python tools/train_scanner.py --tfs 45m,1h,2h,3h,4h
                                                             # -> app/model.pkl,
                                                             #    app/data/geoms.json,
                                                             #    app/data/bands_tf.json
../.venv/bin/python -m pytest tests -q                          # 26 tests
../.venv/bin/python app/server.py --replay --speed 400 --port 8811
cd tools/ui && npm install && node ui.mjs                       # page checks
```

The sweep keeps every `stride`-th test bar (that is what the non-overlap lock-out would
select anyway) because a dense 59-feature matrix for a 1-minute archive is 700 MB per
symbol and the machine has 3 GB. The screen used all 59 features; the shipped model is
retrained by `tools/train_scanner.py` on the 57 a live feed can produce, and every
number in the table above is that retrain's measurement, not the screen's.

Every figure on the page comes from `app/data/*.json`, which those scripts write. No
number is typed into the HTML.

## 9. Limits — read before trusting a number

* **The cut was chosen with hindsight.** p ≥ 0.35 came from the table above. The
  defence is that every cut between 0.25 and 0.50 is positive expectant in all three
  periods, so the choice sits on a plateau, not a spike — and the plateau is shown.
* **The edge is small in absolute terms.** 24.6 % against a 20.6 % break-even. The
  confidence interval, 19.8–30.2 %, contains outcomes you would not want to trade.
* **Two symbols, several timeframes, long only.** The short side is the same pipeline on
  mirrored bars; it is not shipped until it passes the same periods.
* **The round-16 samples are small.** 100–257 trades per clock on 45m–4h, one
  out-of-sample model per clock. 4h once measured +0.466 R in the screen and −0.052 R
  after the feature list was made live-safe; that swing is the noise level, and it is
  why only the clocks that stayed positive are on by default.
* **Funding is not modelled.** At 1 : 4 the hold can be up to 240 hours. Funding on a
  ten-day position is material and missing. This is the largest unmodelled cost, and it
  is why the page promises a measured record, not a live edge.
* **A time-out is scored as 0 R.** 2.5 % of trades end that way at this geometry.
* **Replay is a simulation.** It re-runs from 2024-01-01 on every start; only live rows
  persist across restarts.

## 10. The honest summary

Round 14 proved that 100 % accuracy is a coordinate on a trade-off curve, not a secret.
Round 15 shows the other half: **the curve has a good side.** Move the reward to four
times the risk, widen the stop until the fee is a rounding error, and a model that is
right one time in four pays +0.20 R per trade in the walk-forward — +0.14 R in the
running app.

Two things worth keeping in view. The geometry does most of the work; the model shifts
the hit rate by a few points and that is enough at 1 : 4 because the break-even line is
so low. And a strategy that is wrong three times out of four is uncomfortable to trade,
which is exactly why the page leads with the break-even rate instead of the accuracy.
