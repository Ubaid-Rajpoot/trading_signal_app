# The trade plan on every zone — measured on 90 days of real 1m BTC

Round 6 attaches an **execution plan** to every zone and separates **futures**
from **spot**. This page records what the plan is, what it was measured to do,
and — plainly — what it cannot promise.

Everything below is produced by one command on one file of real candles:

```bash
cd smc-radar
../.venv/bin/python tools/plan_study.py --csv study-data/BTCUSDT-1m-90d.csv --timeframe 1m --horizon 240
```

```
study-data/BTCUSDT-1m-90d.csv · 1m · 129,600 candles · 9541 plans
horizon 240 bars to fill, then 240 bars to resolve
```

`study-data/BTCUSDT-1m-90d.csv` is 90 days of real 1-minute BTCUSDT candles
(129,600 bars, 6 Jan – 5 Apr 2025). It deliberately lives **outside `data/`**
so the replay never plays it and the study never plays the replay file.

---

## 1. What the plan is

The plan is not a new pattern and not a new strategy: it is the trade the book's
own objects imply, written down as three prices.

| Field | Where it comes from | Why |
|---|---|---|
| **entry** | the **near edge of the block** — `bottom` for a short from a high, `top` for a long from a low | the limit order fills the moment price comes back into the block, which is exactly what *mitigation* means in the book (p.48) |
| **stop** | `plan_stop_atr` (0.10) × ATR **beyond the exact high/low** | a wick through the level is the invalidation the book describes, so the stop sits just outside it — not at an arbitrary distance |
| **target** | `next_liquidity()`: the nearest strong/very strong level, swing or session extreme **in front of** the trade | the user's own exit rule: *target = previous low/high*, the next pool of liquidity. It is never a fixed R multiple and never an invented level |
| **R:R** | `abs(entry - target) / abs(stop - entry)` | printed on every card so you can see *before* the trade whether it pays for its risk |

Code: `bot/map.py` — `plan_for()` (lines around 697) and `next_liquidity()`
(around 642). Both are pure functions of what the map already knows; they add
no rule to detection.

A plan is **tracked live**: `waiting` → `filled` (price came back into the
block) → `target hit` / `stopped out`, with the time and price of each. If a
single candle touches both the stop and the target, the stop wins — the same
conservative rule the study uses.

A zone with **no pool in front of it** (no opposite-side level, swing or session
extreme within `plan_pool_bars` = 120 bars, or the nearest one is closer than
0.25 × ATR) carries **no target and no R:R**. The card says *"no pool in
front"*. Those plans are excluded from the study below, because a trade with no
exit cannot be measured as written.

## 2. How it was measured (no look-ahead)

`tools/plan_study.py`, candle by candle:

1. replay the 90 days through the real `MarketMap`; keep every zone that got a
   plan with a target (9,541 of them);
2. from the candle **after** the zone formed, wait for price to reach the entry
   — up to 240 bars;
3. once filled, the **first** of stop / target to be touched decides the trade
   (again up to 240 bars);
4. a candle that touches both counts as a **loss**;
5. no fill within the horizon = `no fill`;
6. trades still open at the end are marked to the close in R.

There are **no fees, no funding and no slippage** in these numbers. On 1m with
0.1 ATR stops, fees are not small; that is a reason to prefer the higher-R:R
subset, not a reason to trust the headline.

## 3. What it measured

### 3.1 Every plan

| group | plans | filled | win | expectancy |
|---|---|---|---|---|
| all plans | 9,541 | 100 % | **61 %** | **−0.11 R** |

**A 61 % win rate that loses money.** The filler is the `< 1.5` R:R bucket: most
targets are the *nearest* pool, and the nearest pool is often closer than the
risk. This is the honest answer to "signals fail and my stops get hit": the
pattern is not the problem, the **distance to the target** is.

### 3.2 By R:R — this is the important table

| R:R | plans | filled | win | expectancy |
|---|---|---|---|---|
| < 1.5 | 9,105 | 100 % | 62 % | −0.15 R |
| 1.5 – 2 | 195 | 97 % | 58 % | +0.56 R |
| 2 – 3 | 117 | 97 % | 48 % | +0.64 R |
| ≥ 3 | 124 | 92 % | 33 % | **+1.36 R** |

Win rate **falls** as R:R rises and expectancy **rises**. That is the whole
point: you do not need to win often, you need the winners to pay for the
losers. Every extra point of R:R buys expectancy at a cost of hit rate.

### 3.3 By grade and by pattern

| grade | plans | filled | win | exp R |
|---|---|---|---|---|
| A+ | 332 | 99 % | **94 %** | +0.28 |
| A | 5,332 | 100 % | 70 % | −0.02 |
| B | 3,425 | 99 % | 45 % | −0.33 |
| C | 452 | 100 % | 59 % | +0.29 |

| kind | plans | filled | win | exp R |
|---|---|---|---|---|
| very strong algo bull | 199 | 99 % | 91 % | +0.24 |
| very strong algo bear | 272 | 100 % | 90 % | +0.25 |
| algo bull | 2,644 | 99 % | 69 % | +0.01 |
| algo bear | 2,730 | 99 % | 69 % | +0.04 |
| rejection block bull | 374 | 99 % | 59 % | −0.14 |
| rejection block bear | 439 | 100 % | 60 % | −0.10 |
| very strong low | 75 | 100 % | 71 % | −0.09 |
| very strong high | 68 | 100 % | 71 % | −0.06 |
| strong low | 1,341 | 100 % | 42 % | −0.40 |
| strong high | 1,399 | 100 % | 39 % | −0.44 |

Trend alignment at formation (8,490 not aligned / 1,051 aligned): 61 % vs
64 % win, −0.11 R vs −0.05 R. **No edge worth claiming** — the same conclusion
round 5 reached for the same filter.

### 3.4 The filters the page actually offers

| filter (as in the app) | plans | filled | win | exp R |
|---|---|---|---|---|
| **sniper — R:R ≥ 1.5** | 436 | 96 % | 48 % | **+0.80** |
| **sniper — R:R ≥ 1.5 + algo candle** | 310 | 94 % | 51 % | **+0.96** |
| sniper — R:R ≥ 1.5 + A+/A | 194 | 97 % | 50 % | +0.51 |
| A+/A only | 5,664 | 100 % | 71 % | −0.00 |
| A+/A and R:R ≥ 2 | 84 | 95 % | 36 % | +0.35 |
| A+/A, R:R ≥ 2, block ≥ 1 ATR | 83 | 95 % | 37 % | +0.37 |
| … and trend aligned | 7 | 86 % | 50 % | +0.77 |

The sniper filters exist because of this table: they are the only subset with a
clearly positive expectancy on this sample, and they are one click away from
"everything". The default stays **A+ and A** (unchanged from round 5).

## 4. About "accuracy approaching 100 %"

Measured, on this data, with these rules:

* **94 % win** is real — for **A+ zones only**, 332 plans, and it comes with
  **+0.28 R**: the average A+ trade risks more than it makes. Those targets are
  close, so a 94 % hit rate and a mediocre payoff are the same trade.
* **The best combination that is both frequent and profitable** is
  R:R ≥ 1.5 + an algo candle: **310 trades over 90 days (~3.4/day), 51 % win,
  +0.96 R per trade**.
* **100 % is not reachable.** Not because the rules are strict, but because a
  stop placed anywhere outside the noise gets hit sometimes; the 9,541 trades
  include 100 %-filled plans whose first touch was the stop. Anyone selling you
  100 % is describing a backtest with look-ahead, a martingale, or no stops.
  The honest number is **expectancy**, and this app now shows it next to every
  filter.

What was done about "my stops get hit": the stop was moved from an arbitrary
distance to **0.10 ATR beyond the exact high/low** (the book's invalidation),
the entry to the **block edge** (so the fill is a real mitigation, not a chase),
and the target to the **liquidity pool** (so the payoff is measured, not
assumed). The result is the table above: the same patterns, now sorted by
whether the trade pays for its risk.

## 5. Futures vs spot

Two markets, two order books, two sets of candles — never mixed into one list:

* **spot** — `BTCUSDT`, MEXC spot kline endpoints and the spot websocket;
* **futures** — `BTC_USDT`, MEXC **contract** endpoints
  (`contract.mexc.com/api/v1/contract/kline/<SYM>`) and the contract websocket
  (`wss://contract.mexc.com/edge`).

`bot/feed.py` tags every symbol with its market, `CompositeFeed` keeps one feed
per market and merges the queue, `bot/engine.py` stamps `market` on every zone
and counts both sides (`market_counts`), and `bot/server.py` gives
`/api/zones?market=`, `/api/universe?market=` and two persisted lists
(`symbols`, `futures_symbols` in `config.json`). The page shows **two
sections**, futures first, each with its own count, its own KPI card and a
`FUT` / `SPOT` badge on every card.

The replay plays both files **merged on time**, so the two sections advance
together; `tools/ui/quality.mjs` fails if one of them goes quiet.

**Honest limit:** MEXC is unreachable from the build sandbox, so the futures
path was verified against the documented request/response shapes and with the
replay file `data/fut/BTC_USDT-1m-2025-01.csv` — which is real *spot* BTC
candles copied under a contract name. It proves the plumbing (two markets, two
sections, two lists, merged stream), **not** that contract candles differ from
spot ones. On your machine, `python run.py check` reports both markets.

## 6. What would change these numbers

* fees and funding — on 1m, a 0.1 ATR stop is small relative to taker fees;
* other instruments — this is BTC only;
* a wider `plan_pool_bars` — a further pool means a bigger target and a lower
  hit rate;
* another timeframe — the plan is per-timeframe; the study is 1m.

Re-run the command above after any change to the rules. Every number on this
page is printed by that tool; none is typed in by hand.
