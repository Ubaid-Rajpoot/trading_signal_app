# Strategy Specification — TDIGM + Structural Shift (HVB)

> **Status: CONFIRMED** (revised after your answers). Implemented in
> `backend/app/core/strategies/tdi_shift.py`, selectable in the app as
> **"TDI + Shift (HVB)"**. It runs side by side with the earlier strategies.

---

## 1. The indicator — TDI (TradingView `TDIGM`)

| Input | Your value | TradingView default |
|---|---|---|
| RSI period | **50** | 13 |
| Band length | **50** | 34 |
| Fast MA on RSI | **1** | 2 |
| Slow MA on RSI | **9** | 7 |
| Band deviation | 1.6185 | 1.6185 |

One RSI drives everything:

```
r        = RSI(close, 50)
mbl      = SMA(r, bandLength)                 # Market Base Line  = the MIDDLE band
offset   = 1.6185 * StDev(r, bandLength)      # population StDev (Pine's stdev)
upper    = mbl + offset                       # blue volatility bands
lower    = mbl - offset
purple   = SMA(r, fastLength) = r             # fast MA = 1, so the purple line IS the RSI
slowMa   = SMA(r, slowLength)                 # SMA(r, 9)
```

Cross-checked against your own legend: `TDIGM_ubaid (50, 50, 1, 9)` printed
**55.0 / 46.5 / 50.8 / 52.2** = upper, lower, MBL, RSI. The implementation
reproduces exactly these four numbers from the same formulas.

---

## 2. HVB — "High Volume Breakout" (your definition, confirmed)

> *"Jab purple line jo move karti hai center me, wo tezi ke sath break kare
> center wale band ko, tab HVB kehlata hai."*

So HVB is **momentum speed**, not traded volume:

```
HVB up   =  purple crosses ABOVE mbl
            AND |Δpurple over N bars| >= hvb_step      (tezi / speed)
            AND purple - mbl        >= hvb_margin      (decisive close-through)

HVB down =  the mirror image below mbl.
```

A slow, flat crawl through the base line is **not** an HVB. Defaults:
`hvb_step = 4.0` RSI points over `hvb_step_bars = 2` bars, `hvb_margin = 1.0`.
All three are adjustable in the UI.

---

## 3. Structure — SHIFT and MS (confirmed)

| Event | Meaning | Code |
|---|---|---|
| **SHIFT** | break of a **minor** swing (local structure turns) | `swing_minor` fractal, default 3 bars |
| **MS** | break of a **major** swing (market structure / trend change) | `swing_major` fractal, default 15 bars |

A swing is a fractal pivot — the extreme of the surrounding `2k+1` bars — and it
becomes usable only `k` bars later, when the right side is complete. A break
fires when price **closes** beyond the level while the previous close was still
inside it (body break, not a wick poke — matching your own annotation
*"broken by wick and then by next candle body and still working"*).

---

## 4. Entry rule (confirmed: same candle)

```
LONG  =  HVB up   AND  ( SHIFT up  OR  MS up )     on the SAME candle
SHORT =  HVB down AND  ( SHIFT down OR MS down )   on the SAME candle
```

`accepted breaks` is switchable: SHIFT only, MS only, or both (default both).
`confluence_window = 0` enforces the strict same-candle rule; raising it to 1–3
lets the structure break trail the HVB if you ever find the strict rule too rare.

**Exit:** the purple line crossing back through the base line, or the stop /
target being hit.

**Stop:** beyond the swing that the break left behind (minor swing first, major
as fallback) plus an ATR buffer. **Targets:** 1R / `target_r`R / 1.6×`target_r`R.

---

## 5. How it is verified

- `test_tdi_shift.py` — TDI maths, causality, pivot confirmation delay, the
  same-candle confluence, and that a slow crawl never counts as an HVB.
- `test_no_lookahead.py` — truncates future candles and asserts past signals do
  not change, for **every** strategy including this one.
- The chart draws the swing levels on price and a live **TDI sub-pane**
  underneath (purple RSI, base line, blue bands, yellow HVB markers).

## 6. Signal frequency (measured, honest numbers)

On the synthetic demo feed (smoother than real crypto), TDI(50) crosses its
base line roughly **38 times per 1,000 bars**, of which ~30% are fast enough to
be HVBs. Requiring a swing break on that exact candle makes setups rare —
roughly **1 per 1,000 bars**. Real exchange data oscillates more, so expect more.

If that is too strict for your taste, the levers (all in the UI) are:
`hvb_step` (lower = more HVBs), `confluence_window` (1–3), and `swing_minor`.

## 7. Still open

| # | Question |
|---|---|
| 1 | Which timeframe do you actually trade this on? (screenshots: 1m, 3m, 6h) |
| 2 | Longs + shorts, or long only on spot? |
| 3 | Any session / news filter (London / NY open)? |
| 4 | Do you want the **strong / weak high-low** and **failure swing** labels from the book on the chart too? |
