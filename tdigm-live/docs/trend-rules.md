# Trend, structure and confirmation — the round-17 gates

The TDIGM setup (liquidity swept → opposite break → HVB) was being fired in
ranging markets, and a lot of those trades lost. Round 17 adds four things and
makes each of them a switch:

1. the setup must form **inside a trend** — higher highs *and* higher lows for a
   long, lower highs *and* lower lows for a short;
2. it may form inside the **internal structure of a bigger trend**, but only
   **one level deep** — internal within internal is skipped;
3. a **break of structure** (BOS) in the trade's direction must have happened;
4. a **high-volume bar** can be demanded as well — and (3) and (4) can be
   combined, either as hard AND rules or as a weighted score.

This page is the honest record: what each rule computes, what it rejects, and
where old measurements are too weak or too stale to support a claim.

> **Current-policy boundary (lifecycle v7 / chain v6).** The historical tables
> below predate exactly-one-sweep structure, the direct paired-endpoint-to-sweep
> approach, four-value moving-MBL HVB proof, breaker-local stops, mitigation-first
> activation, and the variable confirmation-leg H/L exit. They are retained as labelled
> research, not presented as current results. The live
> policy now requires repeated major/macro progression (at least two high and two
> low steps, with 100% remembered recent-step consistency) plus a clear local leg
> or range release. A mixed left-side range passes only after a fresh major/macro
> body close beyond the furthest remembered range edge. Inside an established
> outer trend, one clean local `LH+LL` / `HL+HH` step is enough to identify the
> specific directional leg; the same one-step flicker cannot stand alone inside
> mixed outer structure. ADX ≥ 22 and ER ≥ 0.30 remain additional gates for an
> established trend and cannot certify trend by themselves. They do not veto the
> fresh major/macro full-range release: those lagging statistics commonly still
> say `range` on the exact body close that establishes the new direction.

Legend: **on** = shipped default · **off** = implemented, switchable, left off.

---

## 1. What "trending" means in the code

`bot/trend.py::TrendMap` reads the same pivot machinery the setup already uses,
at three degrees:

| degree | width | what it is |
|---|---|---|
| minor | `pivot_len` = 5 | the swings the setup itself is built from (the **internal** structure) |
| major | `major_len` = 20 | the first **external** structure |
| macro | `macro_len` = 80 | the fallback: used only when no major pivot exists yet |

From those pivots it keeps:

* `direction` / `clean` — `+1` when the latest highs and lows both rose, `-1`
  when both fell, and no direction when mixed;
* `trend_steps` — the smaller of the consecutive high-side and low-side runs, so
  one side cannot hide repeated oscillation on the other;
* `directional_ratio` — how many of all remembered steps (up to seven per side
  with the eight-pivot memory) agree with that direction; the shipped gate
  requires 100%, so one remembered mixed
  step prevents a range from masquerading as an established trend;
* `range_like` — true when direction is mixed, either side has no continuing
  step, or recent agreement is below the structural floor;
* `external.source` — `major` or `macro`, printed on the card so you can see how
  much history the label rests on;
* `bos_direction` / `bos_age` — the direction and age of the last body close
  through a swing;
* `bos_range_break` / `bos_range_edge` — whether that close passed the furthest
  remembered same-side boundary, rather than merely breaking a newer pivot still
  inside an older range;
* `depth(side, kind, index)` — how many degrees of structure the setup sits
  inside: **1** = the break that created it is itself the external break,
  **2** = the first internal break inside an external trend, **3+** = internal
  inside internal.

The card shows all of it: `external bullish (HH+HL) · last break of structure up,
42 bars ago · depth 2 internal · volume 1.81× · score 85`.

## 2. The gates, and what they reject

| rule | config key | default | rejection text on the page |
|---|---|---|---|
| evaluate structural context | `trend_context` / `clear_structure` | **on / on** | `ranging/unclear structure to the left` |
| repeated high-side and low-side progression | `min_trend_steps` | **2** | included in the clear-structure reason |
| remembered recent structural consistency | `min_directional_ratio` | **1.00** | any mixed step blocks the established-trend path |
| specific local leg inside established structure | internal `trend_steps` | **at least 1** | one clean local step is accepted only when major/macro context is already clear |
| fresh full-range shift may establish a new direction | `fresh_shift_max_bars` | **20** | stale/internal-only breaks do not clear the context gate |
| a break of structure must have happened in the trade's direction | `require_bos` | **on** | `no recent structure break in the trade direction` |
| …and it must be recent | `bos_max_age` | **240 bars** | `break of structure is stale (312 bars)` |
| how deep inside the structure a setup may sit | `max_internal_depth` | **2** | `internal within internal` |
| lagging range regime for established trends | `regime_adx_min` / `regime_er_min` | **22 / 0.30** | ADX/ER rejection; bypassed only by the fresh major/macro full-range shift above |
| high-volume signal candle (≥ N× average) | `volume_break` / `volume_mult` / `volume_period` | **off** / 1.5× / 20 | `volume 0.83× < 1.5×` |
| how the switched-on conditions combine | `confirm_mode`, `confirm_min_score` | `all` / 60 | `confluence score 55 < 60` |

`confirm_mode: "all"` is a hard AND: every optional switch that is on must hold.
In `"score"` mode the remaining evidence contributes a weight
(`bot/filters.py::SCORE_WEIGHTS` — trend agreement, clean sequence, BOS, HVB,
volume, depth ≤ 2, regime, pool block) and the sum must reach the threshold.
**Score mode never replaces HVB:** the confirming candle must still prove the
moving-MBL cross before its other confluences are scored. The score and its parts
are on every alert (`score`, `score_parts`) and on the card.

Optional switches live in ⚙ **Trend & structure** and **Confirm the break** in
the page and are written to `config.json`; HVB itself and `hvb_window: 0` are
fixed sweep-strategy invariants.

## 3. Legacy replay: what the earlier policy rejected

These counts are **not lifecycle-v4 counts** and cannot be used as current signal
frequency or performance. One earlier replay of BTCUSDT 1-minute candles, 2024-07-01 → 2025-06-29 (911 days),
on 5m / 15m / 30m / 1h, with the shipped defaults
(`python tools/scan_history.py data/btc --timeframes 5m,15m,30m,1h`):

| event | count |
|---|---|
| sweeps found | 28 824 |
| breaks after a sweep | 26 375 |
| HVB on the same candle | 8 685 |
| **complete setups published** | **68** |

Of the 23 020 candidates that did *not* become a signal, the most common single
reason each was dropped:

| reason | count |
|---|---|
| no HVB on the signal candle | 5 279 |
| internal within internal (depth ≥ 3) | 3 301 |
| break of structure the other way | 2 682 |
| ranging market (ADX < 20) | 2 675 |
| against the external trend | 2 663 |
| against the external structure | 2 363 |

That is **~0.07 signals per day per symbol** on four timeframes. The point of
round 17 was selectivity, and this is what selectivity looks like.

## 4. Legacy measurement (2R research, not the current lifecycle)

Same old data and replay, with a synthetic 2R fixed target (`--score-target fixed --score-r 2`),
signals walked forward on their own timeframe. 8 signals are unscored because
price never reached either level inside the horizon; they count in *signals*,
not in *win%* or *R*.

| # | configuration | signals | scored | win% | R / trade |
|---|---|---|---|---|---|
| A | baseline (pre-round-17 defaults) | 144 | 128 | 39.8% | +0.20 |
| B | trend context + HH/HL only | 88 | 78 | 37.2% | +0.12 |
| C | break of structure only | 125 | 111 | 41.3% | +0.24 |
| D | depth ≤ 2 only | 114 | 101 | 39.6% | +0.19 |
| **E** | **all three (shipped default)** | **68** | 61 | **41.7%** | **+0.25** |
| F | E + high-volume bar | 62 | 55 | 40.0% | +0.20 |
| G | score mode, threshold 60 | 385 | 331 | 36.0% | +0.08 |
| H | score mode, threshold 75 | 342 | 291 | 36.4% | +0.09 |
| I | E without the ADX/ER regime gate | 161 | 146 | 34.2% | +0.03 |

Read at face value: the set of gates cuts signals by **53%** (−76) and moves
R/trade from +0.20 to +0.25. Almost none of that comes from the trend gate on
its own — **B is worse than A** — and the score modes (G, H) are clearly worse
than hard AND rules (E).

### Component study on the same 144 baseline alerts

Each baseline signal was bucketed by whether it would have passed an individual
gate, then walked forward identically:

| bucket | n | win% | R / trade |
|---|---|---|---|
| external trend agrees **and** clean HH+HL | 88 | 37.2% | +0.12 |
| external trend opposite (counter-trend) | 37 | 40.6% | +0.22 |
| no external trend at all (range) | 19 | 50.0% | +0.50 |
| depth ≤ 2 | 114 | 39.6% | +0.19 |
| depth ≥ 3 | 37 | 40.7% | +0.22 |
| BOS agrees with the trade | 116 | 41.4% | +0.24 |
| BOS the other way | 34 | 29.4% | **−0.12** |
| all three gates pass | 68 | 41.0% | +0.23 |
| all three would skip | 36 | 38.8% | +0.16 |

**Two of the three gates are not confirmed by this data.**

* **Break of structure is the one with signal.** When the last break points the
  same way as the trade: 41.4% / +0.24. When it points the other way: 29.4% /
  **−0.12**. That spread is the largest and the only one that changes the sign of
  the expectancy.
* **The trend gate is not confirmed here.** Trend-agreeing setups did *worse*
  (+0.12) than counter-trend (+0.22) and than no-trend (+0.50) — though the
  last bucket is 19 trades and too small to mean much. Note also that every
  signal in this dataset formed *inside* some trend and *all* of them sat at
  depth 2, so the "formed in trend / depth 2" buckets cannot discriminate: the
  setup, as written, is already a trend-continuation pattern. The gate mostly
  removes cases where the external degree disagreed, and those cases did fine in
  this sample.
* **The depth gate is neutral** (+0.19 vs +0.22). It removes 30 signals and
  changes nothing measurable. It stays on because the requirement is explicit —
  internal within internal must be skipped — not because the numbers ask for it.
* **High-volume bar** (F) shaved 6 signals and lost 0.05 R. Off by default,
  switchable.

### Why the trend gate stays on anyway

It is the user's explicit requirement, and this sample cannot refute it — see
the caveats below. It is on, it is measured, and the card always shows the
evidence (`external bullish (HH+HL)`, `depth 2`) so the decision is visible
rather than buried. If a future run on more symbols and more years shows the
same pattern with enough trades to clear the noise floor, the honest move is to
turn `trend_context` off and keep `require_bos`.

## 5. The caveats you should read before believing any of the above

* **This is one symbol and one year.** BTCUSDT only, 2024-07 → 2025-06. A
  trending year for BTC. Nothing here is a claim about ETH, about alts, or about
  2022.
* **The differences are inside the noise.** With ~120 scored trades and a 2R
  target, the standard deviation of R per trade is ≈ 2.83, so the standard error
  of R/trade is ≈ 0.17–0.26 per row. The A→E difference (0.05 R) is a fifth of
  one standard error. **No comparison in this document reaches 2σ.** Treat the
  table as "nothing got worse", not as "this is the winning config".
* **BOS-other-way is the one exception worth naming**: −0.12 vs +0.24 is
  directionally consistent across the buckets and is the reason `require_bos` is
  on.
* **The old structure-target study is not current scoring.** In that study the
  next opposite range extreme left 34 of 37 trades unscorable and median R:R was
  0.28, so the table used synthetic 2R. The live lifecycle now extends the
  favorable sweep→HVB/BOS leg H/L while pending, waits for actual
  order-block/breaker mitigation, and freezes the previous extreme as its
  variable target.
* **Eight configs, chosen after looking at the data.** This is exploratory
  fitting on one dataset, not a pre-registered test. Out-of-sample confirmation
  would need `data/btc_oos` (2023-07 → 2024-06), which is not in this workspace.
* **Timeframe pooling.** All four timeframes are pooled in the headline rows;
  per-timeframe counts are 25–48 each, which is far too few to compare.

## 6. Re-run the study method

The current detector intentionally will not reproduce the superseded counts: it
rejects multi-sweep fixtures and uses the stricter defaults. These commands show
the study workflow on a compatible local dataset and produce current-policy
output.

```bash
cd tdigm-live
# the current shipped policy
python tools/scan_history.py data/btc --timeframes 5m,15m,30m,1h \
    --target range --score-target fixed --score-r 2

# row A: switch the round-17 gates off
python tools/scan_history.py data/btc --timeframes 5m,15m,30m,1h \
    --target range --score-target fixed --score-r 2 \
    --no-trend-context --no-hh-hl --no-clear-structure --no-bos --max-depth 9

# row F: add the high-volume requirement;  row G: score mode
python tools/scan_history.py data/btc --timeframes 5m,15m,30m,1h \
    --target range --score-target fixed --score-r 2 --volume-break
python tools/scan_history.py data/btc --timeframes 5m,15m,30m,1h \
    --target range --score-target fixed --score-r 2 --confirm score --confirm-min 60
```

The walk-forward prints a `depth N` and a `volume <bucket>` breakdown under the
headline table, plus the rejection counters quoted in §3.
