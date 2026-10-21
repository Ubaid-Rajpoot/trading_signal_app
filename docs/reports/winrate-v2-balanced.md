# Win-rate study — BTCUSDT

- Window: last **30 days**
- Strategy: `tdi_shift` with `{"rsi_len": 50, "band_len": 50, "fast_len": 1, "slow_len": 9, "band_mult": 1.6185, "hvb_step": 4.0, "hvb_step_bars": 2, "hvb_margin": 1.0, "swing_minor": 3, "swing_major": 15, "allow_shift": true, "allow_ms": true, "confluence_window": 0, "atr_len": 14, "long_only": false, "require_trend": true, "reject_ranging": true, "require_structure": true, "reject_liquidity": true, "filter_mode": "balanced", "adx_len": 14, "adx_min": 15.0, "er_len": 40, "er_min": 0.12, "max_bos_age": 200, "min_structure_atr": 0.4, "max_structure_atr": 8.0, "liq_lookback": 30, "liq_tolerance_atr": 0.25, "liq_touches": 4}`
- Generated: 2026-10-21 06:51 UTC
- Data source: **demo**

| TF | Bars | Trades | Wins | Losses | Win % | Return % | PF | Max DD % |
|---|---|---|---|---|---|---|---|---|
| 1m | 42969 | 4 | 3 | 1 | 75.0 | 0.38 | 4.51 | -0.21 |
| 3m | 14169 | 1 | 0 | 1 | 0.0 | -0.35 | 0.0 | -0.35 |
| 5m | 8409 | 0 | 0 | 0 | — | 0.0 | — | 0.0 |
| 15m | 2649 | 0 | 0 | 0 | — | 0.0 | — | 0.0 |
| 30m | 1209 | 0 | 0 | 0 | — | 0.0 | — | 0.0 |

> DEMO DATA - the exchange was unreachable, so these candles are synthetic. Win rate here tests the code, not the market. Run the same report with internet access to get real numbers.
