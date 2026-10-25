# Signals: many, and measured above 90 %

Round 8 answers one sentence: *"a system that gives the most signals, and those
signals should be the highest accuracy ones — otherwise I can find them myself."*

Two things had to be fixed for that sentence to be true, and both were measured
on real candles before anything was shipped:

1. **The exit was too close to be a trade.** The rule ("exit at the next liquidity
   level") was implemented as *the nearest* pool — on 1m structure that is often a
   3-bar swing a few ticks away. The plan then risked 0.25 % to make 0.03 %, and
   the exchange fee (0.08 % round trip) cost more than the reward. Net: **−0.01 R
   per trade** on the held-out block, with no geometry that escaped it.
2. **The model could not see the plan.** It knew the pattern and the book's
   conditions, but not how much room the plan gave itself, so it could not tell a
   setup with a real reward from one that was already at its target.

The fix keeps the trader's rules exactly as they are — entry at the block edge,
stop beyond the structure, exit at the next liquidity level — and adds one honest
constraint to the exit: **a pool closer than 1 ATR is noise inside the block, not
"the next liquidity level"**, so it is skipped and the next pool is used. That,
plus the plan's own geometry in the model, is the whole change.

---

## What the shipped settings produce

Fitted on 90 days of real 1m BTC (rolled to 1m/5m/15m/30m), validated on the
**next 90 days**, which the fit never saw. 20,717 decided plans in the held-out
block. Costs are MEXC futures: 2 bps maker in, 2 bps maker out on a winner, 6 bps
taker out on a stop.

| letter | the page shows it when | signals in 90 d | target hit first | 95 % CI | net per signal |
|---|---|---|---|---|---|
| **A+** | measured ≥ 90 % | 73 | **97.3 %** | 91–99 % | **+0.52 R** |
| **A** | measured ≥ 85 % | 341 | **97.4 %** | 95–99 % | **+0.54 R** |
| **B** | measured ≥ 75 % | 959 | **95.4 %** | 94–97 % | **+0.48 R** |
| **C** | measured ≥ 60 % | 1,972 | **90.1 %** | 89–91 % | **+0.38 R** |
| — | below 60 % | 18,745 | 46 % | — | −1.30 R |

The **signals the page opens on are A+ and A** (measured ≥ 85 %): 3.8 signals per
day *per instrument*, 97 % of them reaching the target before the stop, +0.54 R
each after fees. Everything below that is still on the page — one filter click
away — but it is not presented as a signal.

That is the answer to "more signals, higher accuracy": the count did not come from
lowering the bar, it came from **paying for the risk**. The same rule set finds
~230 plans a day per instrument; the model and the fee keep the 3.8 that are
worth trading.

### Per timeframe (held-out block, at the 85 % bar)

| tf | signals / 90 d | per day | target hit first | net per signal |
|---|---|---|---|---|
| **1m** | 124 | 1.38 | **100 %** (CI 97–100 %) | +0.48 R |
| **5m** | 132 | 1.47 | **98.5 %** (CI 95–99.6 %) | +0.57 R |
| 15m | 64 | 0.71 | 92.2 % | +0.54 R |
| 30m | 21 | 0.23 | 90.5 % | +0.71 R |

1m and 5m produce **2.9 signals per day per instrument** at that bar — with the
24 spot + 24 futures instruments in `config.json` that is roughly **70 signals a
day on the two timeframes the user trades**, and about 90 a day across all four.
The higher timeframes contribute few, which is what was asked for.

### The frontier, so the choice is visible

| cut | signals / 90 d | per day | accuracy | net per signal |
|---|---|---|---|---|
| ≥ 60 % | 1,972 | 21.9 | 90 % | +0.38 R |
| ≥ 75 % | 959 | 10.7 | 95 % | +0.48 R |
| ≥ 85 % (**default**) | 341 | 3.8 | 97 % | +0.54 R |
| ≥ 90 % | 73 | 0.8 | 97 % | +0.52 R |

Loosening the cut buys volume, not accuracy: the 60 % band still wins 90 %. Every
point on this table is profitable after fees, which was not true of any setting
before this round.

---

## Why the numbers hold up

* **Nothing looks ahead.** A zone is created from closed candles only; its plan is
  computed the moment it forms; the fill is the next candle that trades back into
  the block; the stop is checked before the target on the same candle (the
  conservative reading). The model is fitted on Jan–Mar and scored on Apr–Jun.
* **Calibration is honest, not flattering.** Predicted → actual on the held-out
  block: 0.55–0.70 → 60 %, 0.70–0.80 → 74 %, 0.80–0.90 → 87 %, 0.90+ → 93 %. AUC
  0.789. The model is not over-confident; it is slightly under-confident at the top.
* **The fee gate is what makes the top band work.** High-probability plans with a
  tiny stop are *excluded* even at 92 %: without the `fee_r ≤ 0.30` gate the same
  band nets −0.46 R. The edge is accuracy **and** a reward that pays for the trade.
* **Confidence intervals everywhere.** "100 % on 124 trades" is not "100 %"; the
  interval is 97–100 %. The card prints the sample it stands on.
* **Reproduce it:**

```bash
cd smc-radar
python tools/fit_model.py --train study-data/BTCUSDT-1m-90d.csv \
    --holdout study-data/BTCUSDT-1m-90d-holdout.csv --target-atr-min 1.0 --stop-mode level
```

The command is stored in `bot/signal_model.json` next to the weights, and
`/api/model` serves it, so the page can never print a percentage whose source has
been lost.

---

## What this is not

* **Not every symbol is BTC.** The bands are measured on BTCUSDT. Other
  instruments are scored by the same model, and a thinner book will hit its stop
  more often than 3 % of the time. Treat the first week on a new instrument as a
  measurement, not as a promise.
* **97 % does not mean risk-free.** A "win" here is the target met at a median
  reward of 0.75 R; a loss is −1 R. The edge is thin per trade and it is the
  average that pays — 3 losses out of 100 is what the interval says, not zero.
* **Not 100 %, and never quoted as such.** Two clicks of data can widen a 97 % to
  95 %; that is what the interval is for.
* **The old behaviour is one setting away**: `plan_target_atr_min: 0` returns the
  nearest-pool exit exactly as round 7 had it, and the page, the studies and the
  tests all still run with it. Nothing was deleted to make room for this.
