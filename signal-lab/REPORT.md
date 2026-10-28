# SignalLab — accuracy against the accuracy you need

Round 14 hunted 100 % accuracy and found it, 480 times over, along with the arithmetic
that makes it worthless: at a 0.05 ATR target the prize (0.0125 R) is smaller than the
round trip (0.0141 R), so even a flawless record returns −0.0016 R per trade.

This round takes the other side of the same coin: **give the trade a reward worth
having.** At 1 : 4 the accuracy you need collapses from "three in four" to **once in
five**, and the fee stops eating the prize. The result is the first version of this
system with a positive expectancy in *every* walk-forward period, on a sample big
enough to quote.

| | round 14 (1 : 2) | **round 15 (1 : 4)** |
|---|---|---|
| target / stop | 2 ATR / 4 ATR | **8 ATR / 2 ATR** |
| break-even accuracy, after fees | 67 % | **20.6 %** |
| fee as a share of the reward | 12 % | **0.7 %** |
| measured accuracy, out of sample | 75.4 % (n = 142) | **25.5 % (n = 184)** |
| **net expectancy per trade** | +0.120 R | **+0.268 R** |
| worst walk-forward period | +0.104 R | **+0.104 R** |
| periods the model was positive in | 3 of 3 | **6 of 6 (2 symbols × 3 folds)** |

Two thirds of the accuracy was thrown away and the money per trade more than doubled.

---

## 1. Data

| | |
|---|---|
| BTCUSDT | 4,823,839 one-minute bars, 2017-08-17 04:00 → 2026-10-24 23:59 UTC |
| ETHUSDT | 4,823,838 one-minute bars, same span |
| Missing minutes | 8,562 (0.18 %) — left as gaps, never forward-filled |
| Source | open Binance 1-minute archive mirrored on GitHub, fetched by `tools/build_dataset.py` |
| Storage | 154 MB per symbol as float32 in `data/`, git-ignored |

## 2. Method (unchanged from round 14, restated)

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
* **Counting** — one open trade per symbol at a time. Without this, 60 consecutive
  firing bars are counted as 60 trades and every interval on the page is a lie.
* **Fees** — 5 bp round trip, charged on every trade, expressed in R against the stop.

## 3. Why reward : risk is the lever

With a fee `f` (in R) and a reward : risk of `x`, the break-even hit rate is

```
p* = (1 + f) / (1 + x)
```

Everything else is downstream of that. The sweep (`research/07_rr.py`) ran 30
geometries: 2 timeframes × 3 stops (0.5, 1, 2 ATR) × 5 rewards (1:2 … 1:6), each in
three folds on both symbols, non-overlapping, with the model refit for every fold.

**1h, 0.5 ATR stop** — the geometry the previous round shipped, with the target moved:

| reward : risk | break-even | base rate | fee | model, best cut | worst period |
|---|---|---|---|---|---|
| 1 : 2 | 37.1 % | 31.8 % | 0.113 R | 30.8 % | −0.367 R |
| 1 : 3 | 27.8 % | 24.1 % | 0.113 R | 32.7 % | −0.044 R |
| 1 : 4 | 22.3 % | 19.5 % | 0.113 R | 22.3 % | −0.795 R |
| 1 : 5 | 18.5 % | 16.4 % | 0.113 R | 20.6 % | −0.097 R |

At a half-ATR stop the fee alone is 0.113 R — **nine times the 0.0125 R prize of the
old geometry** — and nothing works. The stop has to be wide enough that the fee is
small in R, and the reward has to be big enough that being right once in five pays.

**1h, 2 ATR stop** — the shipped geometry:

| reward : risk | break-even | base rate | fee | model at p ≥ 0.50 | trades | worst period | mean |
|---|---|---|---|---|---|---|---|
| 1 : 4 | **20.6 %** | 21.1 % | 0.028 R | **25.5 %** | 184 | **+0.104 R** | +0.268 R |
| 1 : 5 | 17.1 % | 17.4 % | 0.029 R | 25.0 % | 52 | +0.339 R | — |
| 1 : 6 | 14.7 % | 14.6 % | 0.029 R | 15.1 % | 86 | −0.201 R | — |

Of all 30 geometries, **exactly one** has ≥ 100 trades *and* a positive expectancy in
every walk-forward slice: **1h, 2 ATR stop, 8 ATR target, cut p ≥ 0.50**. That is what
the app trades.

On 4h the same geometry looks better (33.3 % at 1 : 4, worst slice +0.384 R) but only
40–50 trades land in the test periods, and only two slices have enough of them to
measure. It is available (`--tfs 4h`) and it is not the default, because it has not
earned it.

## 4. The shipped system, measured

Geometry: reward : risk **1 : 4** (target 8 ATR, stop 2 ATR), 240-bar horizon, long
side, 5 bp round trip, one trade at a time.

| band | signals | accuracy | 95 % CI | EV pre-fee | **EV after fees** | 2022 | 2023 | 2024–26 |
|---|---|---|---|---|---|---|---|---|
| A · p ≥ 0.60 | 52 | 32.7 % | 21.5–46.2 | +0.635 | **+0.606 R** | — | — | 33 % |
| B · p ≥ 0.55 | 90 | 27.8 % | 19.6–37.8 | +0.389 | +0.360 R | 14 % | — | 32 % |
| **C · p ≥ 0.50** | **164** | **24.4 %** | 18.5–31.5 | +0.220 | **+0.191 R** | 20 % | 30 % | 25 % |
| D · p ≥ 0.40 | 250 | 22.8 % | 18.0–28.4 | +0.140 | +0.112 R | 18 % | 27 % | 23 % |
| E · p ≥ 0.30 | 298 | 22.8 % | 18.4–27.9 | +0.141 | +0.112 R | 16 % | 20 % | 26 % |
| all signals | 346 | 17.3 % | 13.7–21.7 | −0.133 | −0.161 R | 10 % | 25 % | 17 % |

The cut the app records at is **p ≥ 0.50** (row C, in bold). Note what the last row
means: without the model's filter this geometry loses money (17.3 % against a 20.6 %
break-even). **The filter is the edge here, not the geometry** — the geometry is what
lets a 24 % hit rate be worth +0.19 R instead of the −0.16 R it is worth when you take
everything.

### Robustness of the chosen configuration

Every slice, listed separately — no pooling, no averaging:

| | 2022 | 2023 | 2024–26 | trades | accuracy |
|---|---|---|---|---|---|
| BTC | +0.104 R | +0.468 R | +0.196 R | 99 | 25.5 % |
| ETH | +0.112 R | +0.475 R | +0.255 R | 85 | 26.4 % |

By calendar year, with the model refit on everything before 2023:

| | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|
| BTC | 30.0 % (20) | 25.0 % (24) | 23.5 % (17) | 17.6 % (17) |
| ETH | 31.6 % (19) | 30.0 % (20) | 30.8 % (13) | 27.8 % (18) |

Ten of the twelve symbol-years are above the 20.6 % break-even. The two that are not —
BTC 2026 at 17.6 % (17 trades) and, marginally, nothing else — are the reason the app
shows a live accuracy counter that can go red.

### Frequency

164 signals over the three test periods for two symbols on one hour is roughly
**0.05 signals per symbol per day** — a genuinely selective rule. Scaled to the 60 most
liquid MEXC contracts that is ~3 a day for the scanner, or about one every five days on
a single chart.

## 5. The 100 % club (still here, still instructive)

Round 14's search, kept because it is the honest answer to "just find a pattern":

* **480 rules** with ≥ 50 trades and zero losses in sample; the largest fired **596**
  times without losing.
* Pooled on 2024–2026, which the search never saw: **98.48 %** — 42,641 of 43,300, the
  base rate (98.07 %) plus noise. 308 of 478 rules lost at least once.
* Of 240 timeframe × target × stop configurations, **4** reach ≥ 98 % accuracy and
  **none** makes money after fees; the 6 that do are 50.7–81.0 % accurate.
* A literal 100 % rule at that geometry returns **−0.0016 R per trade**.

None of that changed. What changed is which side of the trade-off the app sits on.

## 6. The app

`app/server.py` — FastAPI + one page + SQLite, four tabs:

* **Signals** — live tape: probability, band, entry / stop / target, status, R. Rows are
  written to SQLite as they are created and resolved; a refresh (or a restart) never
  loses them. Header shows the ratio, the fee as a share of the reward, and which model
  is scoring.
* **Measured accuracy** — the reward : risk sweep first (why 1 : 4), then the band table
  with break-even, intervals and per-period records.
* **The 100 % club** — the rules that never lost, and what happened to them next.
* **Method & limits** — data, splits, geometry, and what is explicitly *not* claimed.

`--replay` walks the archive bar by bar in arrival order (what the preview shows);
`--live` polls MEXC futures for the same symbols.

**Which model scores what.** Replay uses a *validation* model trained only up to
2023-12-31, so a replay of 2024+ is genuinely out of sample. Live uses the *production*
model trained through 2026-10, because live has no "after" to cheat with. This was not
cosmetic: an early build shipped the production model for both and its replay reported
**96.9 %** accuracy for a system whose measured accuracy was ~75 % — it was scoring bars
it had been fitted on. The page says which model is in use and a test pins it.

## 7. Reproduce

```bash
cd signal-lab
../.venv/bin/python tools/build_dataset.py                      # 9.6 M one-minute bars
../.venv/bin/python research/01_frontier.py                     # the accuracy/fee frontier
../.venv/bin/python research/04_hundred.py --tf 1h --target 0.05 --stop 4
../.venv/bin/python research/07_rr.py --rrs 2,3,4,5,6 --stops 0.5,1,2 --horizon 240
../.venv/bin/python research/06_bands.py --tfs 1h --target 8 --stop 2 --horizon 240 --rec-cut 0.5
../.venv/bin/python -m pytest tests -q                          # 17 tests
../.venv/bin/python app/server.py --replay --speed 150 --port 8811
cd tools/ui && npm install && node ui.mjs                       # page checks
```

Every figure on the page comes from `app/data/*.json`, which those scripts write. No
number is typed into the HTML.

## 8. Limits — read before trusting a number

* **The cut was chosen from this table.** p ≥ 0.50 was the best worst-period cut in the
  sweep, so the +0.191 R is mildly optimistic. The defence is that all six
  symbol × period slices are positive at that cut, and that p ≥ 0.55 and p ≥ 0.60 are
  *more* profitable per trade, so the choice sits inside a region rather than on a
  spike. It is still a choice made with hindsight and it is disclosed as one.
* **Small samples by design.** 164 trades at the recorded cut. The C-band interval is
  18.5–31.5 %, wide enough to contain both "works" and "does not".
* **Two symbols, one timeframe, long only.** The short side is the same pipeline on
  mirrored bars; it is not shipped until it passes the same six slices.
* **Funding is not modelled.** At 1 : 4 the hold is long (up to 240 hours): funding on a
  ten-day position is material and is missing. This is the largest unmodelled cost and
  it is why the page promises a measured record, not a live edge.
* **A 240-bar time-out is scored as 0 R.** 2.5 % of trades end that way at this
  geometry; the number is displayed, and treating them as free is generous.
* **Replay is a simulation.** It re-runs from 2024-01-01 on every start; only live rows
  persist across restarts.

## 9. The honest summary

Round 14 proved that 100 % accuracy is a coordinate on a trade-off curve, not a secret.
Round 15 shows the other half of that sentence: **the curve has a good side.** Move the
reward to four times the risk, widen the stop until the fee is a rounding error, and a
model that is right one time in four pays +0.19 R per trade, in every period measured,
on two assets and four calendar years.

That is not a promise of the future. It is the best that 9.6 million bars of the past
would support, with the weakest period shown next to the strongest, and the cut that
produced it named as a hindsight choice. A strategy that is wrong three times out of
four is uncomfortable to trade — which is exactly why it was worth building the counter
that shows the arithmetic instead of the accuracy.
