# Historical regression fixtures

`BTCUSDT-3m-chain8-2024-12.csv.gz` contains 3,010 unmodified BTCUSDT one-minute OHLCV rows ending shortly after the 2024-12-11 13:32:59 UTC signal.

Source: public `BTCUSDT-1m-2025-09-01.csv` data from `Thaum-blip/TradinBot`, repository commit `f085c6668517c9384229a4b1300767296092e323`, Git blob `693f40455d4bb90d3a2497a400e8aaa6bc5bf3f3`. Despite that upstream filename, the source ends on 2024-12-31. The fixture is intentionally small and is replayed through the real production engine; it contains no expected alert or synthetic signal row.
