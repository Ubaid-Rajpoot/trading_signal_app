# Reference material

The trader's own charts and the book the strategy comes from. Kept here so the
code and the visual rules stay side by side. Nothing in this folder is executed
by the app.

## Charts

| File | What it shows |
|---|---|
| [`charts/eurusd-h1-shift-weak-low.png`](charts/eurusd-h1-shift-weak-low.png) | EURUSD 1h — a **SHIFT** down through the marked level, a **weak low**, and the TDI crossing its base line (orange marker). |
| [`charts/eurusd-h6-strong-high-hvb.png`](charts/eurusd-h6-strong-high-hvb.png) | EURUSD 6h — **STRONG HIGH** rejection zone after a SHIFT, with the **HVB** (high volume breakout) flagged below. |
| [`charts/tdi-hvb-closeup.png`](charts/tdi-hvb-closeup.png) | Close-up of the TDI: purple line, base line, volatility bands, and the point where the purple line breaks the base line *with speed*. |
| [`charts/btc-1m-tdgm-ubaid.png`](charts/btc-1m-tdgm-ubaid.png) | BTC 1m — the trader's own `TDIGM_ubaid (50, 50, 1, 9)` settings, with the swing levels marked in orange. |
| [`charts/btc-3m-tdgm-ubaid-01.png`](charts/btc-3m-tdgm-ubaid-01.png) | BTC 3m — same settings, level retest. |
| [`charts/btc-3m-tdgm-ubaid-02.png`](charts/btc-3m-tdgm-ubaid-02.png) | BTC 3m — same settings, HVB marker on the base-line cross. |
| [`charts/btc-3m-wick-then-body-break.png`](charts/btc-3m-wick-then-body-break.png) | The break rule: *"broken by wick and then by next candle body and still working"* — a body close counts, a wick poke does not. |
| [`charts/valid-setup-downtrend-examples.png`](charts/valid-setup-downtrend-examples.png) | **v2**: two downtrend examples of a *valid* setup — impulse leg, compact correction (orange levels), break, continuation. |
| [`charts/setup-inside-structure-zone.png`](charts/setup-inside-structure-zone.png) | The same two examples with the structure zone circled: the setup forms **inside** that structure, not in open space. |

## Book

[`book/ict-institutional-smc-trading.pdf`](book/ict-institutional-smc-trading.pdf)
— *Advanced ICT Institutional SMC Trading Book* (David Woods). The pages used to
define this strategy are:

- **p.42** — strong high / weak high, strong low / weak low
- **p.49–50** — false break in market structure (FBMS) / fake momentum shift
- **p.53–54** — failure swing (price fails to make a new LL / HH)
- **p.87–88** — confluence: FVG fill → LTF algo candle → **−MS**
- **p.93** — high volume breakout (HVB)
- **p.100–103** — divergence and Stoch RSI

## How these map into code

| Concept | Where |
|---|---|
| TDI maths (`TDIGM`) | `backend/app/core/indicators.py` → `tdi()` |
| HVB (purple line breaks the base line with speed) | `backend/app/core/strategies/tdi_shift.py` |
| SHIFT (minor swing) / MS (major swing) | `backend/app/core/swings.py` |
| Trend / range / liquidity filters (v2) | `backend/app/core/market_structure.py` |
| The rule in plain words | [`../strategy-spec.md`](../strategy-spec.md) |
| Version history | [`../strategy-versions.md`](../strategy-versions.md) |
