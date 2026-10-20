# Win-rate study — BTCUSDT

- Window: last **30 days**
- Strategy: `tdi_shift` with `{"rsi_len": 50, "band_len": 50, "fast_len": 1, "slow_len": 9, "band_mult": 1.6185, "hvb_step": 4.0, "hvb_step_bars": 2, "hvb_margin": 1.0, "swing_minor": 3, "swing_major": 15, "allow_shift": true, "allow_ms": true, "confluence_window": 0, "atr_len": 14, "long_only": false}`
- Generated: 2026-10-20 17:13 UTC
- Data source: **demo**

| TF | Bars | Trades | Wins | Losses | Win % | Return % | PF | Max DD % |
|---|---|---|---|---|---|---|---|---|
| 1m | 43119 | 57 | 18 | 39 | 31.58 | -6.86 | 0.49 | -7.06 |
| 3m | 14319 | 13 | 2 | 11 | 15.38 | -5.71 | 0.1 | -6.05 |
| 5m | 8559 | 5 | 2 | 3 | 40.0 | 0.06 | 1.4 | -0.95 |
| 15m | 2799 | 3 | 1 | 2 | 33.33 | -1.63 | 0.21 | -1.63 |
| 30m | 1359 | 0 | 0 | 0 | — | 0.0 | — | 0.0 |

> DEMO DATA - the exchange was unreachable, so these candles are synthetic. Win rate here tests the code, not the market. Run the same report with internet access to get real numbers.
