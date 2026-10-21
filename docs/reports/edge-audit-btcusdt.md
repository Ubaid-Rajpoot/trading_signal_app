# Edge audit — BTCUSDT

Every variant walks the same real candles once. `pf` is gross win / gross loss,
`exp` is the average return per trade after 14 bps round-trip costs.
A rule with an edge stays above pf 1 in **both** halves.

| TF | entry | exit | trades | win % | PF | exp % | h1 PF | h2 PF |
|---|---|---|---|---|---|---|---|---|
| 5m | tdi_break | tdi_cross | 120 | 25.8 | 0.89 | -0.039 | 1.28 | 0.47 |
| 5m | tdi_break | struct_inval | 121 | 30.6 | 0.9 | -0.043 | 1.09 | 0.59 |
| 5m | struct_trend | tdi_cross | 1389 | 20.7 | 0.5 | -0.138 | 0.56 | 0.43 |
| 5m | struct_trend | struct_inval | 1061 | 24.6 | 0.6 | -0.135 | 0.67 | 0.52 |
| 5m | struct_vol | tdi_cross | 763 | 21.6 | 0.55 | -0.131 | 0.64 | 0.46 |
| 5m | struct_vol | struct_inval | 638 | 25.7 | 0.65 | -0.122 | 0.77 | 0.51 |
| 5m | struct_tdi | tdi_cross | 1271 | 19.7 | 0.51 | -0.132 | 0.56 | 0.46 |
| 5m | struct_tdi | struct_inval | 933 | 25.1 | 0.63 | -0.121 | 0.7 | 0.56 |
| 15m | tdi_break | tdi_cross | 59 | 25.4 | 0.76 | -0.125 | 1.03 | 0.37 |
| 15m | tdi_break | struct_inval | 58 | 31.0 | 0.59 | -0.291 | 0.69 | 0.42 |
| 15m | struct_trend | tdi_cross | 443 | 27.3 | 0.68 | -0.125 | 0.59 | 0.81 |
| 15m | struct_trend | struct_inval | 343 | 31.8 | 0.75 | -0.124 | 0.74 | 0.77 |
| 15m | struct_vol | tdi_cross | 283 | 25.1 | 0.67 | -0.14 | 0.7 | 0.64 |
| 15m | struct_vol | struct_inval | 234 | 31.6 | 0.73 | -0.149 | 0.83 | 0.63 |
| 15m | struct_tdi | tdi_cross | 405 | 24.4 | 0.64 | -0.142 | 0.54 | 0.78 |
| 15m | struct_tdi | struct_inval | 302 | 33.4 | 0.83 | -0.082 | 0.86 | 0.79 |
