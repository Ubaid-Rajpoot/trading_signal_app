# Conditions, probability, and why the stops were being hit

Round 7 answers three things the user said, in order:

1. *"A and A+ have only a few conditions met, yet the type and quality says A+ — why?"*
2. *"For futures no signal comes at all, though MEXC has 1600 coins."*
3. *"Give me the most signals **and** the highest accuracy, otherwise I can find
   signals better myself."*

Every number below comes from one of two commands, on real recorded candles,
never from a promise.

```bash
cd smc-radar
# 1 - the conditions, the frontier, the fee (90 days of real 1m BTC)
../.venv/bin/python tools/condition_study.py --csv study-data/BTCUSDT-1m-90d.csv --timeframe 1m
# 2 - the model: fitted on those 90 days, checked on the NEXT 90 days
../.venv/bin/python tools/fit_model.py --train study-data/BTCUSDT-1m-90d.csv \
    --holdout study-data/BTCUSDT-1m-90d-holdout.csv --timeframe 1m
```

`study-data/BTCUSDT-1m-90d.csv` = 1 Jan–31 Mar 2025, `…-holdout.csv` = 1 Apr–29
Jun 2025 (129,600 real 1m candles each). Both live outside `data/` so the replay
never plays them.

---

## 1. The letter now means a measured probability

The old grade was a weighted score. The user's complaint was exact: a setup with
five or six of the book's conditions could print **A+**, so the letter stopped
meaning anything.

So we measured the obvious fix first — **does "more conditions" actually predict
a better trade?** Trade every zone's own plan (limit at the block edge, stop
beyond the level, target at the next liquidity pool), candle by candle:

| conditions met (of 10) | plans | win (target first) | expectancy |
|---|---|---|---|
| 3 | 40 | 62 % | +0.01 R |
| 4 | 461 | 55 % | −0.20 R |
| 5 | 1,839 | 62 % | −0.08 R |
| 6 | 3,231 | 63 % | −0.07 R |
| 7 | 2,877 | 60 % | −0.12 R |
| 8 | 975 | 59 % | −0.19 R |
| 9 | 113 | 69 % | −0.07 R |
| 10 | 5 | 100 % | +0.15 R |

**The count does not rank the outcome** — 57–69 % everywhere, and eight
conditions is *worse* than five. The held-out 90 days say the same (5 cond. 60 %,
8 cond. 57 %). So making the letter a count would have been a nicer-looking lie.

What *does* rank it is a small model over **which** conditions are met, the
pattern family, the side and the plan's own R:R. Fitted on the first 90 days,
then checked on the next 90 days it had never seen:

* AUC **0.735** on the held-out block;
* calibration (predicted → actually happened): 30 %→28 %, 47 %→46 %, 63 %→62 %,
  75 %→75 %, 88 %→87 %, 92 %→95 %;
* the best decile reached its target before its stop **95 %** of the time, the
  worst decile 28 %;
* stable in every month of the holdout: top-decile win 96 / 93 / 96 / 94 %.

That is what the letter is now: **A+ ≥ 85 %, A ≥ 70 %, B ≥ 55 %, C below**
(`prob_cuts` in the settings). The card still lists all ten conditions, met and
missed, because that is the book's language — but the letter is not allowed to
be a count the data says is meaningless.

The ten conditions, exactly as the code checks them (`bot/map.py`,
`CONDITIONS`):

| condition | the book | how it is checked |
|---|---|---|
| `grab` | p.42, p.45 | a later candle traded through the previous swing |
| `ms` | p.42, p.53 | a candle closed through the opposing swing (structure break) |
| `major` | p.45 | the swept level was itself a major (strong) level |
| `inducement` | p.45, p.69, p.91 | ≥ `induce_count` equal highs/lows within `induce_tol × ATR` |
| `algo` | p.67 | the pivot candle left an FVG within `disp_bars` |
| `displacement` | p.89 | the leg out of the extreme ≥ 2 × ATR |
| `block` | p.88 | a real opposite-colour order-block candle was found |
| `session` | p.88, p.91 | the extreme is a session high/low |
| `confluence` | book ch. on strong levels | another strong level at the same price |
| `trend` | user's rule | the market is trending, not ranging |

Each condition on its own (held-out block, "with" vs "without"):

| condition | win with | win without | exp with | exp without |
|---|---|---|---|---|
| `grab` | 54 % | 75 % | −0.23 | +0.26 |
| `ms` | 62 % | 59 % | +0.01 | −0.16 |
| `major` | 66 % | 60 % | −0.12 | −0.07 |
| `inducement` | 61 % | 42 % | −0.07 | −0.36 |
| `algo` | 61 % | 22 % | −0.07 | −0.58 |
| `displacement` | 66 % | 45 % | +0.01 | −0.32 |
| `block` | 60 % | 90 % | −0.08 | +3.86 |
| `session` | 60 % | 61 % | −0.07 | −0.07 |
| `confluence` | 65 % | 59 % | −0.07 | −0.08 |
| `trend` | 57 % | 61 % | +0.02 | −0.10 |

Three of these are worth saying out loud, because they contradict a natural
reading:

* **`grab` looks negative**, but that is not "liquidity grabs are bad": a strong
  high/low *is* a grab by definition, and that whole family is the weakest one
  (strong high 39 %, strong low 42 % — see below). Inside a family, the model
  gives `grab` a negative weight too, which is why the family terms are in it.
* **`algo`, `inducement`, `displacement` are the strongest positive conditions**
  — exactly the user's intuition that an algo-candle high/low is the reliable
  one.
* **`block without`** is 90 % win on a tiny handful of zones that fell back to
  the "pivot candle" rectangle; not a real group.

By pattern family (hold-out block):

| kind | plans | win | expectancy |
|---|---|---|---|
| very strong algo bull | 199 | 91 % | +0.24 R |
| very strong algo bear | 272 | 90 % | +0.25 R |
| algo bull | 2,644 | 69 % | +0.01 R |
| algo bear | 2,730 | 69 % | +0.04 R |
| rejection block bull | 374 | 59 % | −0.14 R |
| rejection block bear | 439 | 60 % | −0.10 R |
| very strong low | 75 | 71 % | −0.09 R |
| very strong high | 68 | 71 % | −0.06 R |
| strong low | 1,341 | 42 % | −0.40 R |
| strong high | 1,399 | 39 % | −0.44 R |

## 2. The reason the stops were being hit: the fee

This is the finding that matters more than any model. **The fee is charged on the
whole position, while the risk is only the stop distance.** So the cost of a
trade in R is `round-trip fee ÷ stop distance`.

Measured on 129,600 real 1m candles, the shipped plan's stops (0.10 × ATR beyond
the level) are:

| stop distance as % of price | trades | win | gross R | fee at 6 bps × 2 | net R |
|---|---|---|---|---|---|
| 0.00–0.02 % | 704 | 64 % | +1.31 | −33.7 | −32.4 |
| 0.02–0.05 % | 2,610 | 74 % | +0.17 | −3.48 | −3.31 |
| 0.05–0.10 % | 5,574 | 67 % | −0.04 | −1.68 | −1.72 |
| 0.10–0.20 % | 6,495 | 59 % | −0.20 | −0.88 | −1.08 |
| 0.20 % + | 4,680 | 48 % | −0.35 | −0.40 | −0.75 |

The median stop is **0.11 % of price**; a MEXC futures round trip (0.02 % maker
in, 0.06 % taker stop / 0.02 % maker out) is **0.08 %**. The fee is
**0.7–1.8 × the entire risk**. No pattern, no model and no win rate survives
that — and it is exactly why "the stops get hit and it still loses".

So every zone now carries:

* `plan.risk_pct` — the stop distance as a % of price;
* `fee_r` — the round-trip cost in R for that plan (`(maker+taker) ÷ risk`);
* `net_r` — `prob × R:R − (1 − prob) − fee_r`;
* `tradable` — true only when `fee_r ≤ 0.30 R` **and** `net_r > 0`.

The page prints all four on the card and only marks **TRADE-READY** when the
edge survives the fee.

The consequence, stated plainly: **on 1m, hardly anything is tradeable.** On this
data only ~1 % of 1m plans clear the fee. From 5m upwards the same signals do:

| timeframe | plans | trade-ready | all: win | all: net R | trade-ready only: win | net R |
|---|---|---|---|---|---|---|
| 1m | 9,498 | 127 (1.3 %) | 61 % | −1.29 | 58 % | −0.05 |
| 5m | 1,483 | 87 (5.9 %) | 65 % | −0.40 | 71 % | +0.06 |
| 15m | 374 | 41 (11 %) | 60 % | −0.31 | 80 % | +0.23 |
| 30m | 188 | 33 (18 %) | 61 % | −0.17 | 94 % | +0.58 |

Read that table honestly, because it is the whole story:

* On **1m** the fee filter lifts a −1.29 R system to roughly **break-even**
  (−0.05 R). It does not make 1m profitable; it stops the bleeding.
* From **15m** up the same rules, same model, are genuinely positive
  (+0.23 R at 15m, +0.58 R at 30m) on a handful of trades a day per instrument.
* Ranking by measured chance alone is **not** enough: the top decile of 1m is
  94 % accurate and still **−0.82 R**, because many of those plans have a
  reward smaller than the fee (94 % × 0.2 R is a loss). Accuracy and expectancy
  are two different numbers, and the card shows both.

## 3. "Most signals with the highest accuracy"

Those two pull against each other, and the app now shows the trade-off instead of
pretending it away:

* **Breadth is where signals come from.** The scan takes any number of spot
  pairs *and* futures contracts (up to `max_symbols`, 2000 by default), each on
  five timeframes. On one instrument and one timeframe the honest number is a
  few trades a day; on 1,600 contracts it is thousands of setups a day, and the
  ranking picks the best of them.
* **Accuracy is where the ranking comes from.** The list is sorted by measured
  probability, `A+` is the ≥ 85 % band, and the top decile measured 95 % on data
  it had never seen.
* **Both together** are the `trade-ready` filter, which is now the default view.

## The model file

`bot/signal_model.json` is written by `tools/fit_model.py` and read at runtime by
`bot/model.py` (pure Python, no numpy in the app). It carries its own provenance:

```json
"trained_on":  {"file": "BTCUSDT-1m-90d.csv",          "rows": 9498},
"validated_on":{"file": "BTCUSDT-1m-90d-holdout.csv", "rows": 10565},
"target": "P(the plan's target is reached before its stop)"
```

Features (all known the moment the zone forms — no future candle):

```
bias, side=high, grab, ms, major, inducement, algo, displacement,
block, session, confluence, trend, log(1+rr), family=strong, family=algo, family=vs_algo
```

Weights are in the file, so a zone's probability can be recomputed by hand. If
the file is missing the app still runs: every `prob` is `null`, the grade falls
back to `C`, and the page says the model is missing rather than inventing one.

## Honest limits

* **BTC only.** The model is fitted on BTCUSDT. It transferred across timeframes
  (5m AUC 0.730, 15m 0.756, 1h 0.766) but it has **not** been validated on other
  coins; re-run `tools/fit_model.py` on the instrument you care about before
  trusting the numbers there.
* **No funding, no slippage, no partial fills.** Fees are modelled (maker in,
  maker out, taker stop), nothing else. A stop that gaps through costs more than
  −1 R.
* **A target may not fill.** A limit at the pool is assumed to fill when price
  touches it. In a fast reversal that is optimistic.
* **The frontier is thin at the top.** A+ is 10 % of plans; on one instrument and
  one timeframe that is a handful of trades a day, and the confidence interval on
  a 95 % win rate measured on 1,058 trades is roughly ±1 %.
* **Nothing here is a promise.** It is a measurement of what these rules did on
  this data, with the command that reproduces it printed above.
