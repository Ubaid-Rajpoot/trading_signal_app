# Does the scanner trade the book? — page by page

The book is [`docs/reference/book/ict-institutional-smc-trading.pdf`](reference/book/ict-institutional-smc-trading.pdf)
(*Advanced ICT Institutional SMC Trading*, David Woods). This page is the honest
audit: for every concept the strategy uses, **what the code does**, **which test
proves it**, and **what it measured**. Nothing is marked "done" because the idea
sounds right.

Legend: **yes** = implemented and tested · **yes (switch off by default)** =
implemented, measured, and left off because it made the results worse on real
data · **no** = not implemented, and here is what it would need.

| Book | Concept | Status | Code | Test |
|---|---|---|---|---|
| p.42 | **Strong high / weak low** — "the High that caused Manipulation and Structure Breaking"; "the Low that failed to break structure" | **yes** | every alert carries `swept_label` = *strong high/low*, `broke_label` = *weak low/high* (`bot/engine.py::_confluence`) | `test_structure_labels_strong_and_weak_levels_and_the_break_kind`, `test_every_signal_carries_the_book_labels` |
| p.42 | **Premium / discount** — "above the BSL = premium, below the SSL = discount" | **yes (switch off by default)** | dealing range = last major low..major high; position of the **sweep** (or the entry) in that range, `bot/confluence.py` | `test_dealing_range_separates_premium_and_discount`, `test_book_gates_reject_and_explain` |
| p.49 | **False break in market structure (FBMS)** | **yes** | a sweep that then closes beyond its own extreme is dropped (`invalidated`) instead of becoming a setup (`bot/structure.py`) | `test_a_sweep_that_keeps_going_is_dropped` |
| p.53 | **SHIFT** (minor break) / **MS** (major break) | **yes** — labels; MS as the confirming break is **off** | minor pivots = `pivot_len` (5), major = `major_len` (20); every setup carries `break_kind` | `test_structure_labels_strong_and_weak_levels_and_the_break_kind` |
| p.53 | **Follow the external structure** (last major break) | **yes, on by default** | `structure_direction` gate compares the trade with `Structure.external_trend` | `test_book_gates_reject_and_explain` |
| p.53–54 | **Failure swing** — price fails to make a new LL/HH | **yes (shown, not gated)** | `failing_swings()` returns the last swing that failed to extend; printed on the card | `test_failure_swing_is_a_swing_that_fails_to_extend` |
| p.53 | **Liquidity build-up** (equal highs/lows) | **yes, on by default** | `liquidity_pools()` + `pools_ahead()`; a trade aimed into 3+ equal levels is refused | `test_liquidity_pool_counts_equal_extremes_not_every_candle` |
| p.87 | **FVG / imbalance in the displacement** | **yes (switch on, off by default)** | `fair_value_gaps()` counts the 3-candle gaps the sweep→break leg printed, and how much of the best one is filled | `test_fvg_is_a_three_candle_gap_and_nothing_else` |
| p.92–93 | **HVB** — the price line crossing the centre band *with speed*, nothing else | **yes** | `centre_of()` + `hvb_signal(..., min_points, min_speed)`; outer bands are never read by a rule | `test_hvb_never_fires_on_an_outer_band_touch`, `test_hvb_needs_speed_when_the_speed_gate_is_on` |
| p.88 | **Session high/low as liquidity** (Asia / London / NY) | **no** | would need a session calendar + per-session extremes; the sweep of a session extreme is the same `Level` machinery, so it is a small addition, not a redesign | – |
| p.88 | **Breaker block / LTF "algo candle"** | **no** | needs an LTF order-block detector (last down candle before the up move, etc.) and an LTF feed per symbol; that is a second engine, and it is not in the drawing the strategy came from | – |
| p.100–103 | **Divergence / Stoch RSI** | **no** | the TDIGM already gates momentum; adding a second oscillator would need its own measured case | – |

> **Round 17 — trend, depth, break of structure, volume.** Four further gates
> live outside the book: trade with the external trend (HH+HL / LH+LL), skip
> internal-within-internal structure, require a break of structure, and
> optionally require a high-volume bar, combinable as hard AND rules or as a
> weighted score. What they reject and what they measured — including the two
> this sample does not confirm — is in
> [`trend-rules.md`](trend-rules.md). The book's own "follow the external
> structure" row above (p.53) is the ancestor of the first of them; the round-17
> version reads the full swing sequence at three degrees rather than just the
> last major break.

## What this means in one sentence

The **setup shape** the book draws — liquidity taken, strong/weak levels, a SHIFT
that confirms, an HVB with speed, structure context and liquidity ahead — is
implemented and tested. The **lower-timeframe execution detail** (session
blocks, breaker blocks, FVG-fill→algo-candle sequence, divergence) is not.

## Measured: every gate, one at a time

Real BTCUSDT 1-minute candles, 2024-07-01 → 2025-06-29 (364 days), replayed
through the live engine (`tools/book_study.py`, five parallel workers). Each row
adds **one** book gate to the baseline (structure + HVB ≥1 pt + speed ≥1.5× +
trending market). Every signal is then walked forward on its own timeframe (stop
vs the range-extreme target, 300 bars, stop wins ties).

| gate | n | win% | per day | total R | R / trade |
|---|---|---|---|---|---|
| baseline (no book gates) | 225 | 51.5% | 0.62 | −4.65 | −0.036 |
| liquidity pool block | 187 | 58.1% | 0.51 | +0.81 | **+0.009** |
| follow external structure (p.53) | 146 | 72.1% | 0.40 | +9.62 | **+0.158** |
| premium/discount at the sweep (p.42) | 124 | 43.3% | 0.34 | −5.51 | −0.061 |
| premium/discount at the entry (p.42) | 60 | 29.8% | 0.16 | −6.79 | −0.119 |
| FVG required (p.87) | 185 | 50.5% | 0.51 | −5.96 | −0.058 |
| MS must confirm (p.53) | 0 | – | 0.00 | – | – |
| structure 0.4–6 ATR | 219 | 50.8% | 0.60 | −6.22 | −0.049 |
| **direction + pool (chosen default)** | **139** | **72.2%** | **0.38** | **+8.30** | **+0.15** |

Two findings worth keeping:

* **The confirming break is always a SHIFT, never an MS.** In 364 days of 1m
  BTC, "MS must confirm" produced **zero** setups: after liquidity is swept, the
  reversal always breaks a *minor* swing within the 60-bar window. That matches
  the book's own chart, where the reversal is labelled **shift**, not MS — so
  the MS setting stays available but off, and the direction gate uses the last
  major break instead.
* **Premium/discount hurt when used as a hard gate**, whichever way it is
  measured. The reason is visible in the rejections: after a high is swept and
  the reversal breaks a low, the **entry** is near the bottom of the dealing
  range — i.e. a short would be sold in *discount*. Gating that out removes the
  good trades too. It is therefore displayed (so you can see it) and switchable,
  but off by default.

## Held-out year

Same code, same defaults, 2023-07-01 → 2024-06-30 (366 days, one year the
defaults were not chosen on):

| filters | signals | per day | R / trade |
|---|---|---|---|
| no trend gate, no book gates | 976 | 2.67 | −0.03 |
| trending market only | 283 | 0.77 | −0.09 |
| **trending + external structure + pool (default now)** | 163 | 0.45 | −0.08 |

So the book gates cut the noise by **83%** but did **not** turn the walk-forward
into a profitable one. (Also note that on the short samples here more than half
of the signals are still *open* after 300 bars - they never reached either the
stop or the range-extreme target - so the R/trade figures rest on roughly 40-60
scored trades per year. Treat them as a direction, not a verdict.) That is the honest state: the *selection* is faithful to
the book; the *profitability* depends on the exit (stop at the sweep extreme,
target at the range extreme), which is a separate, measurable question —
`tools/scan_history.py --target next|range --min-rr` exists for exactly that.

## Reproduce

```bash
# data (public, no key)
curl -sL -o /tmp/tradinbot.tgz https://codeload.github.com/Thaum-blip/TradinBot/tar.gz/refs/heads/main
tar xzf /tmp/tradinbot.tgz -C /tmp
python tools/import_csv.py /tmp/TradinBot-main/historical_data_BTCUSDT_1m_20220701_to_20250630.csv \
  /tmp/TradinBot-main/temp_data --symbol BTCUSDT --start 2024-07-01 --end 2025-06-29 --out data/btc
python tools/import_csv.py /tmp/TradinBot-main/historical_data_BTCUSDT_1m_20220701_to_20250630.csv \
  --symbol BTCUSDT --start 2023-07-01 --end 2024-06-30 --out data/btc_oos

# the gate table above (five workers, ~4 min)
python tools/book_study.py data/btc

# the live defaults, with the walk-forward
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,5m,15m,30m,1h --target range
```

---

# The book engine, measured (round 22)

The gates above were the *old* engine: a sweep, a break, an HVB. `bot/book/` is
the book's own chain instead — liquidity map → trap → displacement that leaves an
imbalance → mitigation of that imbalance → the plan, with the draw on the next
liquidity. Everything below is that engine on **BTCUSDT, 2023-01-01 →
2024-12-31**, entry timeframes `5m,15m,30m,1h`, replayed through the live engine
(`tools/book_study.py`), fees **0.06% round trip** (maker in, taker out) charged
in the plan and tested in the walk-forward.

```
../.venv/bin/python -m tools.book_study data/btc --symbol BTCUSDT \
    --mode shift --fill 1.0 --be-at-r 2.0
```

| entry recipe | n | win / BE / loss | total R | R / trade | median planned R:R |
|---|---|---|---|---|---|
| mitigation at 50% of the gap, stop to BE at +1R *(shipped before this round)* | 219 | 28 / 80 / 110 | +12.51 | +0.057 | 4.93 |
| **full gap fill, then the reverse shift, BE at +2R** *(shipped now)* | 217 | 45 / 30 / 142 | **+42.79** | **+0.197** | 3.73 |

Why the second one is the default: it is also the *book's* recipe (p.87 — follow
the daily cycle, wait for the **full FVG fill**, then the reverse shift), so it is
more faithful **and** better on this sample. Entering at half the gap on the fill
alone is a shortcut the book does not make.

The honest split of the winning row:

| window | trades | total R | R / trade |
|---|---|---|---|
| 2023 | 94 | +46.0 | +0.49 |
| 2024 | 123 | −3.2 | −0.03 |

**All of the edge is in 2023.** Two years, one instrument, 217 trades is a thin
sample, and a year-by-year split that flips sign is the definition of "not
proven". Add to that:

* **Break-even at +2R is an approximation.** The book says *move the stop to
  break-even when price breaks structure* (p.109); the live engine cannot see a
  structural break on the entry timeframe cheaply, so it uses a fixed R
  multiple. +2R was chosen because +1R measured worse (+12.51R vs +42.79R) — on
  the same data that selected everything else, so it is a fitted parameter, not
  a discovered one. It is in `config.json` (`book.breakeven_at_r`) for exactly
  that reason.
* **One instrument.** The live scanner runs many; nothing here measures whether
  the same recipe works on anything but BTC.
* **Median planned R:R is 3.73, under the 1:4 preference.** The floor
  (`book.min_rr`) is 3.0 and the trader asked for 1:4; at 4.0 the sample loses
  about half its trades (this is the "≥1:4 preference vs signal count" trade-off
  that has been stated in every round). The card shows the real number on every
  plan — nothing is rounded up to look like it met a target.

So: **the implementation is faithful and auditable; the edge is not established.**
The engine is built so that this is checkable rather than believed — the numbers
above come out of the same code the page runs, and the same command can be
pointed at another instrument, another window, or a different exit.
