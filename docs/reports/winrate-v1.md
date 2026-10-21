# Win-rate study — BTCUSDT

- Window: last **30 days**
- Strategy: `tdi_shift_v1` with `{"rsi_len": 50, "band_len": 50, "fast_len": 1, "slow_len": 9, "band_mult": 1.6185, "hvb_step": 4.0, "hvb_step_bars": 2, "hvb_margin": 1.0, "swing_minor": 3, "swing_major": 15, "allow_shift": true, "allow_ms": true, "confluence_window": 0, "atr_len": 14, "long_only": false}`
- Generated: 2026-10-21 06:51 UTC
- Data source: **demo**

| TF | Bars | Trades | Wins | Losses | Win % | Return % | PF | Max DD % |
|---|---|---|---|---|---|---|---|---|
| 1m | 43119 | 55 | 18 | 37 | 32.73 | -6.23 | 0.53 | -6.93 |
| 3m | 14319 | 12 | 1 | 11 | 8.33 | -6.05 | 0.02 | -6.05 |
| 5m | 8559 | 6 | 2 | 4 | 33.33 | -1.17 | 0.55 | -2.16 |
| 15m | 2799 | 3 | 1 | 2 | 33.33 | -1.63 | 0.21 | -1.63 |
| 30m | 1359 | 0 | 0 | 0 | — | 0.0 | — | 0.0 |

> DEMO DATA - the exchange was unreachable, so these candles are synthetic. Win rate here tests the code, not the market. Run the same report with internet access to get real numbers.
