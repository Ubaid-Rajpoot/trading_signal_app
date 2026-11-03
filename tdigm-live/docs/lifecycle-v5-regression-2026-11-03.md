# Lifecycle-v5 deterministic regression — 2026-11-03

This is a reproducibility and lifecycle check, **not a profitability claim for
the MEXC futures universe**. It runs the shipped sweep/HVB/structure policy on
one public BTCUSDT candle file because no production signal archive is present
in this checkout and the development sandbox cannot reach MEXC.

## Fixed input

- Source repository: `Thaum-blip/TradinBot`, commit
  `f085c6668517c9384229a4b1300767296092e323`.
- Source file: `historical_data_BTCUSDT_1m_20220701_to_20250630.csv`.
- SHA-256: `a7c45107b3095a75f1a65f675e01cf1e8982a031587ef7f2ca2f7b3b9cf7a0fe`.
- Despite its filename, the file itself contains 1,317,520 closed one-minute bars
  from 2022-07-01 00:00:00 through 2024-12-31 23:59:00.
- Timeframes: all configured frames — `1m,3m,5m,15m,30m,1h,2h,4h,1d`.
- Policy: strict structural context and regime gates, exact one-level same-leg
  sweep, same-candle directed HVB/body break, order-block or eligible-breaker
  mitigation, and lifecycle v5 target model `hvb_bos_leg_extreme_v1`.

## Result

| measure | result |
|---|---:|
| setups retained | 109 |
| mitigated / decided | 108 / 108 |
| still pending and unmitigated | 1 |
| wins at frozen confirmation-leg H/L | 24 |
| stopped | 84 |
| win rate | **22.22%** |
| mean R per decided trade | **−0.256R** |
| mean winning R | +2.3482R |
| winning-R range | +0.0009R to +15.6508R |
| 95% interval for mean R/trade | −0.701R to +0.189R |
| statistical verdict | **not proven** |

Every one of the 109 rows is lifecycle version 5 with the new target-model ID.
There were zero rows whose recorded target extreme occurred on or after the
mitigation candle. The 24 wins had 24 distinct R values, confirming that this is
not fixed-4R scoring. The unmitigated row remains flagged as `pending`; it is not
counted as a win or loss.

| timeframe | wins | stopped | pending |
|---|---:|---:|---:|
| 1m | 16 | 45 | 0 |
| 3m | 4 | 19 | 0 |
| 5m | 1 | 10 | 0 |
| 15m | 3 | 3 | 1 |
| 30m | 0 | 3 | 0 |
| 1h | 0 | 1 | 0 |
| 2h | 0 | 3 | 0 |
| 4h | 0 | 0 | 0 |
| 1d | 0 | 0 | 0 |

## Reproduction

```bash
curl -fL -o /tmp/tradinbot-v5.tgz \
  https://codeload.github.com/Thaum-blip/TradinBot/tar.gz/f085c6668517c9384229a4b1300767296092e323
rm -rf /tmp/tdigm-v5-source && mkdir -p /tmp/tdigm-v5-source
tar xzf /tmp/tradinbot-v5.tgz -C /tmp/tdigm-v5-source --strip-components=1
# Then, from tdigm-live:
python tools/import_csv.py \
  /tmp/tdigm-v5-source/historical_data_BTCUSDT_1m_20220701_to_20250630.csv \
  --symbol BTCUSDT --start 2022-07-01 --end 2025-06-30 \
  --out /tmp/tdigm-v5-candles
python tools/backfill_archive.py /tmp/tdigm-v5-candles \
  --symbol BTCUSDT \
  --timeframes 1m,3m,5m,15m,30m,1h,2h,4h,1d \
  --hvb-window 0 --trend-context --db /tmp/tdigm-v5.sqlite3
```

## Limits

This sample is one instrument from one historical source, has no fee/slippage
model, and produced no setups on 4h or 1d. It cannot establish the current win
rate across all active MEXC perpetuals. Its interval includes zero, so even this
sample does not demonstrate positive expectancy. A production claim requires a
fresh lifecycle-v5 archive or a much broader, exchange-matched replay; older
fixed-target rows must remain excluded.
