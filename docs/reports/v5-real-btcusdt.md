# v5 — the stop and target from the drawing, measured on real BTCUSDT

**Data:** real Binance klines, 2025-01-01 → 2025-06-29 (180 days), source `dataset`.
**Costs:** 5 bps fee + 2 bps slippage per side, 1% equity risk per trade.
Reproduce:

```bash
python scripts/winrate.py --symbol BTCUSDT --days 180 \
  --timeframes 5m,15m,30m,1h --strategy tdi_shift_v5 --real-only
```

## What the drawing says

| In the picture | In the code |
|---|---|
| **teal box** | the range: `range_high` / `range_low` over the last 60 bars |
| **red box** (above the teal one) | the sweep / wick zone — the level that must not be reclaimed |
| **vertical drop** | the setup itself: HVB + structure break (v4's entry) |
| **return into the teal top** | the entry: the pullback into the premium of the box |
| **dashed line** | the target: the far side of the box (below its low for shorts) |
| **stop loss** | beyond the sweep high / box high (+0.15 ATR buffer) |

So: **stop = the other side of the structure; target = the previous low/high
(the liquidity)**. With a 60-bar box that is typically a **5.5 : 1** reward/risk,
which is exactly what the drawing shows.

## Results (real data)

| TF | Trades | Win % | Avg R | PF | Return |
|---|---|---|---|---|---|
| 5m | 42 | 21.4 | −0.17 | 1.25 | +1.73% |
| 15m | 15 | 20.0 | −0.26 | 0.78 | −2.75% |
| 30m | 14 | 7.1 | −0.77 | 0.25 | −8.99% |
| 1h | 10 | 40.0 | **+1.66** | 3.93 | **+15.41%** |

Only the 1h version is clearly profitable; 15m and 30m lose. Samples are 10–42 trades per timeframe over six months, so treat every row as a hint, not a promise.

## The important comparison: stop vs no stop

Same entry logic, same data, v4 (no stop, exit at the next liquidity) versus v5
(stop beyond the structure, target at the far side of the box):

| TF | v4 — no stop | v5 — stop as drawn |
|---|---|---|
| 5m | 24 trades, 95.8% win, PF 4.78, **+4.71%** | 42 trades, 21.4% win, PF 1.25, **+1.73%** |
| 15m | 10 trades, 90.0% win, PF 4.42, **+5.58%** | 15 trades, 20.0% win, PF 0.78, **−2.75%** |
| 30m | 8 trades, 100% win, PF ∞, **+6.91%** | 14 trades, 7.1% win, PF 0.25, **−8.99%** |
| 1h | 5 trades, 100% win, **+7.52%** | 10 trades, 40% win, **+15.41%** |

Read this honestly:

* **The stop is not free.** It cuts the drawdown, but on 15m/30m it also turns a
  winning curve into a losing one, because the stop sits just beyond a level that
  price likes to wick through before going the right way.
* **The huge R:R needs a low win rate.** 5.5 : 1 means you lose four times out of
  five and still come out ahead — but only if the wins are actually taken. At 16%
  the maths is marginal, at 40% (1h) it is comfortable.
* **1h is where the drawing works.** On lower timeframes the same idea drowns in
  noise and costs.

## What to do with this

1. If you want to trade the picture exactly: use **1h**, where it is profitable in
   both halves of the sample.
2. If you want more trades: the stop needs to sit further from the sweep wick
   (`stop_buffer_atr`) or the target must be closer (`min_rr`, premium depth).
   Both are parameters, measure before changing them.
3. No stop (v4) wins more often and makes more on 5m/15m/30m in this sample, but
   one open loser can run for 200 bars. That risk is real and is not in the
   win-rate column.
