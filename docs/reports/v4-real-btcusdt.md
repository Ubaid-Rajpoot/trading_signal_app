# v4 on real BTCUSDT — what the data actually says

**Data:** Binance spot klines, real (not synthetic), 1-minute, 2025-01-01 → 2025-06-29
(180 days, 259,200 bars), resampled to each timeframe by aggregation.
**Source reported by the API:** `dataset`. Reproduce with:

```bash
python scripts/winrate.py --symbol BTCUSDT --days 180 \
  --timeframes 1m,5m,15m,30m,1h --strategy tdi_shift_v4 --real-only
```

## What changed in v4 (your corrections)

| Before (v1–v3) | v4 |
|---|---|
| HVB required a 1.5× volume spike | **volume test removed** — HVB = aggressive centre-band break (≥2 RSI points in 2 bars) |
| Setup had to sit *inside* internal structure | **external structure accepted too** — the break can be at the big trend's structure or inside the internal one |
| Exit = purple line crosses back | **exit at the next swing high/low (liquidity)** |
| — | a `max_bars` clock (200) because there is no stop loss |
| — | a swing level closer than 0.75 ATR is ignored — a target smaller than the round-trip cost is not a target |

Everything else is unchanged: trend direction must agree, ranging markets are still
rejected, no trading into resting liquidity, no stop loss.

## Results — 6 months, fees 5 bps + 2 bps slippage per side

| TF | Trades | Win % | Profit factor | Return | 1st half | 2nd half |
|---|---|---|---|---|---|---|
| 1m | 126 | 54.8 | **0.39** | **−15.71%** | n=56, 51.8%, PF 0.25 | n=70, 57.1%, PF 0.66 |
| 5m | 24 | 95.8 | 4.78 | +4.71% | n=13, 100%, PF ∞ | n=11, 90.9%, PF 1.81 |
| 15m | 10 | 90.0 | 4.42 | +5.58% | n=8, 87.5%, PF 3.89 | n=2, 100% |
| 30m | 8 | 100.0 | ∞ | +6.91% | n=5, 100% | n=3, 100% |
| 1h | 5 | 100.0 | ∞ | +7.52% | n=4, 75.0%, PF 4.10 | n=1, 100% |

**1m is a loser.** The target (next swing) is small on a 1-minute chart, the costs
are the same, and the losers run to `max_bars`, so the average loss (−0.40%) is
three times the average win (0.13%). Do not trade this on 1m.

## The control test that matters

A high win rate can be a side effect of the exit rule, not of the entry. So the
same exit was given **random entries** (same count, same timeframe):

| TF | v4 entries | random entries, same exit |
|---|---|---|
| 5m | n=24, 95.8% win, PF 4.95, +4.97% | n=24, 62.5% win, PF 1.05, **−0.94%** |
| 15m | n=28, 89.3% win, PF 3.81, +11.23% | n=26, 53.8% win, PF 0.75, **−4.21%** |

The exit alone does not make money. The combination — HVB on the same candle as
the structure break, with the trend, out of a range, then out at the next
liquidity — is what carries the result.

## Honest caveats

* **Sample size.** 5m/15m/30m/1h give 5–24 trades per timeframe over 6 months.
  That is enough to see a direction, not enough to promise a win rate.
* **One symbol, one six-month window.** This sandbox has BTCUSDT Jan–Jun 2025 only.
  Import more with `python scripts/fetch_real_data.py` and re-run the command above.
* **No stop loss.** A trade that goes against the setup runs to `max_bars` (or the
  structural invalidation, if you switch it on). That is where the big losses come
  from. `invalidate_on_structure: true` exits when price closes back through the
  broken swing — measure it before deciding.
* **Target before entry cost.** With a tight swing target the win rate is high but
  each win is small; a few large losses can erase many wins. Watch profit factor,
  not just win rate.
