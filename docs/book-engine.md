# The book engine — how the scanner trades now

> Source: *ICT / Institutional SMC Trading*, David Woods, 2022 —
> `docs/reference/book/ict-institutional-smc-trading.pdf`. Page numbers below
> refer to it. Code: `tdigm-live/bot/book/`. Measurement:
> `tdigm-live/tools/book_study.py`. This document is the specification the code
> is written against; if the two disagree, the code is wrong.

The previous scanner (TDI + structure break + HVB, one timeframe at a time) is
still selectable as `strategy: "sweep"`, but it is no longer the default. This
is what replaced it and why.

---

## 1. The shape of the change

The old engine asked one question per timeframe: *did this candle break
structure with momentum?* The book asks a sequence, in order, and only trades
the last step:

```
1  where is the liquidity, and which pool is the draw?        p.5-19
2  which side of the dealing range is price on?               p.49-55
3  has a pool just been raided and rejected?   (trap)          p.11-13
4  did that leave a displacement with an imbalance? (AC)       p.67-93
5  has the imbalance been mitigated, and did the draw hold?    p.87
6  is the stop behind the high-volume imbalance?               p.106
7  is the draw worth at least the minimum R:R, after fees?     p.5, p.109
```

Everything is a module, everything is testable in isolation, and the same
`BookRunner` is used by the live server and the back-test:

| module | job |
|---|---|
| `bot/book/swings.py` | pivots at three degrees, breaks (BOS/MS), sweeps, strong/weak highs |
| `bot/book/liquidity.py` | PDH/PDL, PWH/PWL, PMH/PML, session extremes, "which pool is the draw" |
| `bot/book/imbalance.py` | fair value gaps, high-volume imbalances, algo/vector candles |
| `bot/book/sessions.py` | Asia/London/NY clock, New York midnight open, 90-minute cycle |
| `bot/book/narrative.py` | the state machine: trap → AC → mitigation → plan |
| `bot/book/runner.py` | 1m candles in, plans out; HTF context, per-timeframe narratives |

Causality is built in: a pivot exists only after its right side closes, a gap
only after the candle *after* the displacement closes, PDH only after the day
closes, and the higher timeframe is only consulted when its candle completes.

---

## 2. The rules, in the book's words

**Liquidity grading (p.5).** *Major liquidity, major move.*

| grade | pools | role |
|---|---|---|
| major | PDH/PDL, PWH/PWL, PMH/PML, HTF swings | the draw |
| medium | Asia/London/NY extremes, 15m-1h swings | the trap, second-choice draw |
| minor | 1m-5m swings | inducement only |

A pool is struck off the moment price trades through it: liquidity that is gone
cannot be a draw. Every pool carries `made_ms` (when it became knowable) and
`swept_ms` (when it was taken).

**Trap, not break (p.11-13).** A wick through a pool that closes back on the
original side is manipulation. A close *through* the pool is acceptance, and the
idea dies on the spot (`the pool was accepted, not raided`). "Every strong high
leaves a weak low" (p.42) is encoded as: a pivot that was swept *and* followed by
a break in the other direction is marked `strong`.

**Algo candle (p.67-93).** The displacement that leaves the imbalance. It needs
a body of at least `min_body_atr` × ATR (1.0) and volume of at least
`vol_mult` × the 20-bar mean (1.3), and it must leave a three-candle gap. When
that gap is on high volume it is an **HVI** (p.81) and the stop goes behind it
(p.106). The *vector candle* variant (body engulfs the previous candle, p.71) is
recognised and reported.

**Entry (p.87).** *Follow the daily cycle → full/pacing FVG fill → mitigate the
LTF algo candle → reverse shift.* Two modes ship:

* `mitigation` (default, measured best): enter when the gap is `fill_frac`
  (50%) mitigated — a limit into the imbalance, stop behind the HVI.
* `shift`: wait for the mitigation *and* a break of structure in the trade's
  direction, then enter on that close. Fewer, later, worse measured.

**Stop (p.106).** Behind the high-volume imbalance (or the AC high/low if that
is further), plus `stop_buffer_atr` (0.1) ATR. Never tighter than
`min_stop_atr` (0.4) ATR.

**Draw (p.5).** The nearest untouched pool in the trade's direction: medium for
intraday entries (5m/15m/30m), major for 1h and above (`target_min_grade:
"auto"`). If nothing major is left, a medium pool is the fallback.

**Fees are a rule, not a footnote.** A plan whose round-trip cost
(`fee_pct` 0.06%) is more than `max_fee_r` (0.20) of its risk is refused. On BTC
that alone rejects ~30% of otherwise valid setups, because a 0.03% stop pays 2R
in rent.

**Expiry.** A trap with no displacement inside `arm_bars` (30) dies; an armed
setup with no entry inside `entry_max_bars` (40) dies. Nothing stays "pending"
forever.

---

## 3. What a card shows

The chain, in the book's order, with the numbers that produced it:

```
swept LONDON_LOW 27,483 (medium) → bullish AC 2.4×ATR, 2.7× vol
FVG 27,417.2–27,501.4 → 63% mitigated → draw 27,801 (1:3.9)
entry 27,459 · stop 27,347 (behind the HVI) · fee 0.19R · ny session · 4h bias bullish
```

The chart draws the liquidity map (dashed, struck through once taken), the
imbalance the trade came from, the entry/stop/target, and the structure break.
Every rejection is counted and visible in `/api/health` (`book.drops`), so
"why are there no signals" always has an answer.

---

## 4. Measurement

Same code, real BTC 1m CSVs (2023-01-01 → 2024-12-31), four entry timeframes
(5m/15m/30m/1h), real fees, stop-before-target inside a candle, forward walk
starting on the candle *after* the signal:

```
python tools/book_study.py data/btc --start 2023-01-01 --end 2025-01-01
```

```
funnel: 1,052,560 minutes → 3,285 traps → 1,519 displacements → 274 plans
dropped: draw closer than min R:R ×9,271 · accepted, not raided ×5,584
         against the HTF direction ×4,170 · the fees would eat the edge ×4,111
         stop on the wrong side ×3,122 · no displacement ×1,755 · expired ×1,338

274 plans · 23.0% win · +38.63R · +0.141R per trade · median planned R:R 3.7
2023: 110 plans, 28.2% win, +50.06R      2024: 165 plans, 19.4% win, -11.93R
```

The half-year disagreement is the important number. **+0.14R per trade over 274
trades on one instrument is not an edge** — it is a hint. What *is* established
is that the pipeline is deterministic, causal, fee-aware and explainable, and
that its loss modes are visible.

Settings that were measured and rejected (same window, same fees):

| variant | plans | win% | total R | R/trade |
|---|---|---|---|---|
| **mitigation, min R:R 3 (shipped)** | 274 | 23.0 | **+38.63** | +0.141 |
| mitigation, min R:R 4 | 172 | 15.7 | -0.26 | -0.002 |
| mitigation, min R:R 2.5 | 330 | 24.5 | +36.66 | +0.111 |
| shift entry, min R:R 4 | 306 | 13.7 | +20.84 | +0.068 |
| premium/discount required | 97 | 18.6 | -11.76 | -0.121 |
| major pools only | 326 | 25.2 | +41.68 | +0.128 |
| 1h/4h entries only, major draw | 160 | 20.8 | -1.79 | -0.011 |
| scale 50% at +1R, stop to BE | 306 | 6.2 | -88.27 | -0.288 |

*(all with the fee gate on; the "scale out" row is why the shipped build does
not take partials — it removes the right tail that pays for everything else)*

### The honest summary

* Works: the strategy is implemented as specified, one code path from live to
  back-test, and every number on the page is derived, not decorated.
* Not proven: the edge. 2023 carries the result; 2024 is negative. Treat this
  as a structured hypothesis with a measurement harness, not as a money machine.
* Unverified here: live exchange behaviour (this sandbox cannot reach
  MEXC/Binance), 30-symbol parallel scanning under a real feed, and any
  out-of-sample year after 2024 (`data/btc_oos` is not in this checkout).

### Where to push next

1. **Sessions.** The book's daily cycle is not in the defaults yet
   (`sessions: []`). London/NY-only is one flag away and is the obvious next
   test, not a guess to hard-code now.
2. **Inducement as a gate, not a label.** It is currently scored (+1) but not
   required.
3. **Higher-timeframe alignment of the draw.** Using 4h structure for the draw
   on 5m entries was tested only indirectly (`--no-htf`, `premium`); a dedicated
   `htf` sweep is cheap with the harness.
4. **A second instrument.** Everything above is BTCUSDT. Before any of it is
   believed, the same tables have to be produced for ETH/SOL — a one-line
   `--symbol` change.
