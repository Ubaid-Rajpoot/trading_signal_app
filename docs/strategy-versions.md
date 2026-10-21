# Strategy versions

The trader's system is published in **versions**. A version is never edited once
it has been used to judge anything — new ideas become a new version. That keeps
old results reproducible and lets you compare the versions side by side in the
app (the strategy dropdown lists them with their version number).

| Version | id | Status | What it does |
|---|---|---|---|
| **v1.0** | `tdi_shift_v1` | **frozen** — do not edit | HVB + same-candle SHIFT/MS, nothing else |
| **v2.0** | `tdi_shift_v2` | frozen | v1 + structure / trend / range / liquidity filters |
| **v3.0** | `tdi_shift_v3` | current default | v2 + HVB now requires a **volume spike** |

`tdi_shift` (the old id) now resolves to the **latest** version, so old links and
saved settings keep working. Pinned ids never move: `tdi_shift_v1` will always be
v1.

A git tag marks the exact commit where v1 was frozen:

```bash
git tag -l                 # strategy-v1
git show strategy-v1       # the last commit before v2 existed
```

## v1.0 — the original build

One candle had to do two things:

1. **HVB** — the purple RSI line crosses the Market Base Line *with speed*
   (travel ≥ 4 RSI points in 2 bars, landing ≥ 1 point past the band).
2. **SHIFT / MS** — price closes through a swing level (minor = SHIFT,
   major = MS).

Exit when the purple line crosses back. No stop loss, no take profit.

The problem you pointed out: **it fires anywhere**, including inside a range.
That is what v2 fixes.

## v3.0 — HVB means HIGH VOLUME break

Your correction:

> "HVB ka matlab hai ke centre wala band break ho high volume ke saath."

HVB is *High Volume Breakout* (book p.93). v1 and v2 measured "aggression" only
in RSI points, which is why HVB was so rare — one per ~500 bars on smooth data.
In v3 the volume does the work:

| Condition | Default | Note |
|---|---|---|
| purple line crosses the Market Base Line | required | unchanged |
| **volume spike** | ≥ **1.5×** the 20-bar average | this is what makes it an HVB |
| speed floor | ≥ 2 RSI points in 2 bars | relaxed from 4.0, since volume now carries the weight |
| finish past the band | ≥ 1 RSI point | unchanged |

Set `hvb_step: 0` to ignore speed entirely, or `hvb_use_volume: false` to go back
to v2's RSI-only definition. Volume is measured as
`volume / SMA(volume, 20)` per bar, lag-free.

All v2 structure gates are unchanged, and v3 is a strict subset of v2 — a test
asserts it only adds the volume requirement.

> **Why HVB looked missing on the demo feed:** synthetic candles had random volume
> and smooth prices, so no bar could be a genuine volume breakout. The demo
> generator now expands volume on impulsive candles (like a real tape), which is
> why HVB counts went up even offline. Real candles are the only honest test.

## v2.0 — inside structure, with the trend, never in a range

Every v1 condition still has to be true, *and*:

### 1. Structure scope

The setup must be **inside external or internal structure**.

* *External structure* = the direction of the last **major** break. In your
  drawings that is the impulse leg that broke the previous major low (downtrend)
  or high (uptrend).
* *Internal structure* = the compact correction built **after** that break — the
  minor high and minor low you circled. The entry is the break of that small
  structure in the direction of the leg.

So the filter requires: both minor pivots are **newer than the last major
break**, and the structure is compact — between `min_structure_atr` and
`max_structure_atr` ATR tall. A wide, overlapping mess is not a correction, it is
a range, and it is rejected.

### 2. Direction

Longs only while the external trend is up, shorts only while it is down. That is
exactly your charts: after a down leg, take the short continuation, not the
counter-trend long.

The first trade after a reversal is still allowed — the moment the major break
happens the external trend flips, so the new leg can be traded from its start.

### 3. Not ranging

A market is only tradeable when **all** of these hold:

| Check | Default | Why |
|---|---|---|
| fresh major break | ≤ 120 bars ago | a stale break means the leg is finished |
| ADX | ≥ 18 | no directional energy |
| efficiency ratio (net ÷ total travel, 40 bars) | ≥ 0.20 | a *wide* range also breaks major swings, so "last break" alone is not enough — this is the real trend-vs-chop measure |

If any fails → `RANGING` → every setup is blocked.

### 4. No liquidity build-up

3 or more swing highs (or lows) within 0.25 ATR of each other inside 30 bars =
a pool of resting liquidity. Price tends to sweep those instead of continuing,
so a setup aimed into one is blocked. This is the "build-up" case from the PDF.

### What you see in the app

* **Radar** — a setup that the rules forbid shows as **BLOCKED**, with the reason
  in the tooltip and a counter above the grid.
* **Chart panel** — a *Market* block: regime (trending/ranging), ADX, efficiency,
  bars since the break, internal structure height, and whether liquidity is
  above/below.
* **Win rate** — every row shows `taken / raw` (how many v1 setups v2 kept) and
  the blocked reasons are listed under the table.

### Tuning

All of it is in the strategy params (Settings → the strategy's parameters):

```
require_trend, reject_ranging, require_structure, reject_liquidity   (on/off)
adx_len, adx_min, er_len, er_min, max_bos_age
min_structure_atr, max_structure_atr
liq_lookback, liq_tolerance_atr, liq_touches
```

Turn `reject_ranging` off and v2 behaves like v1 plus the direction and
structure checks; turn everything off and it **is** v1 (by construction, v2 only
removes signals — a test asserts this).

## Comparing versions

```bash
python scripts/winrate.py --days 30 --strategy tdi_shift_v1
python scripts/winrate.py --days 30 --strategy tdi_shift_v2
```

Use `--real-only` on a machine with internet access so the numbers come from
Binance rather than the demo feed.

## v4 — 4.0 (current)

The trader's corrections, after he pointed out that the earlier versions were not
his system:

* **Volume is out.** HVB = the purple line breaking the centre band aggressively
  (≥ `hvb_step` RSI points in 2 bars). No volume multiple is required. The
  toggle `hvb_use_volume` still exists and defaults to off.
* **Location is opened up.** A setup may form at the big trend's structure
  *or* inside internal structure. `require_structure` defaults to off.
* **Exit at the next liquidity.** The trade runs to the nearest confirmed swing
  high (long) / swing low (short). The old "purple line crosses back" exit is off
  unless `exit_on_tdi_cross` is switched on.
* **A clock, not a stop.** `max_bars` (200) closes a trade that never reaches its
  level. `invalidate_on_structure` (off) exits when price closes back through the
  broken swing.

Trend and range filters stay: the trade must agree with the external trend, and a
ranging market still produces nothing.

Measured on real BTCUSDT, Jan–Jun 2025: `docs/reports/v4-real-btcusdt.md`.

## v5 — 5.0 (current)

The trader's drawing, encoded:

* **Box**: the high/low of the last `range_len` (60) bars.
* **Setup**: v4's rule (HVB + same-candle structure break, with the trend).
* **Entry**: the pullback into the premium of the box (`premium_level` 0.9) within
  `retest_window` bars of the setup — not the break candle itself.
* **Stop**: beyond the sweep high / box high (+0.15 ATR). This is the red box in
  the drawing.
* **Target**: the far side of the box, i.e. the previous low/high — the dashed
  line. Typically 5.5 : 1.
* **Filter**: `min_rr` 0.5 (skip trades whose target is closer than half the risk).

Measured on real BTCUSDT: 1h profitable (+15.4%, PF 3.9), 5m marginal, 15m/30m
losing. Full write-up: `docs/reports/v5-real-btcusdt.md`.
