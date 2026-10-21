# Win-rate study — BTCUSDT

- Window: last **30 days**
- Strategy: `tdi_shift_v3` with `{"rsi_len": 50, "band_len": 50, "fast_len": 1, "slow_len": 9, "band_mult": 1.6185, "hvb_step": 2.0, "hvb_step_bars": 2, "hvb_margin": 1.0, "swing_minor": 3, "swing_major": 15, "allow_shift": true, "allow_ms": true, "confluence_window": 0, "atr_len": 14, "long_only": false, "require_trend": true, "reject_ranging": true, "require_structure": true, "reject_liquidity": true, "filter_mode": "strict", "adx_len": 14, "adx_min": 18.0, "er_len": 40, "er_min": 0.2, "max_bos_age": 120, "min_structure_atr": 0.4, "max_structure_atr": 6.0, "liq_lookback": 30, "liq_tolerance_atr": 0.25, "liq_touches": 3, "hvb_volume_mult": 1.5, "hvb_volume_len": 20, "hvb_use_volume": true}`
- Generated: 2026-10-21 07:13 UTC
- Data source: **demo**

| TF | Bars | Trades | Wins | Losses | Win % | Return % | PF | Max DD % |
|---|---|---|---|---|---|---|---|---|
| 1m | 43049 | 6 | 3 | 3 | 50.0 | 0.52 | 2.11 | -0.69 |
| 3m | 14249 | 7 | 2 | 5 | 28.57 | -0.23 | 1.17 | -0.81 |
| 5m | 8489 | 2 | 1 | 1 | 50.0 | 0.57 | 3.28 | -0.39 |
| 15m | 2729 | 1 | 0 | 1 | 0.0 | -0.18 | 0.0 | -0.18 |
| 30m | 1289 | 0 | 0 | 0 | — | 0.0 | — | 0.0 |

> DEMO DATA - the exchange was unreachable, so these candles are synthetic. Win rate here tests the code, not the market. Run the same report with internet access to get real numbers.
