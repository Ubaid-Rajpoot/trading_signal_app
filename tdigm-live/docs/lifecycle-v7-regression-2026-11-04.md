# Lifecycle-v7 / chain-v6 deterministic regression — 2026-11-04

> **Historical pre-displacement evidence.** Current chain v8 keeps these trend,
> structure, MBL and lifecycle rules but additionally requires auditable
> prior-ATR displacement on both the sweep and paired-break candles. These
> chain-v6 results are not mixed into current statistics.

This is a reproducibility and policy-regression check, **not a profitability
claim for the MEXC futures universe**. It runs the shipped sweep/HVB/structure
policy on one public BTCUSDT candle file because no production signal archive is
present in this checkout and the development sandbox cannot reach MEXC.

## Fixed input

- Source repository: `Thaum-blip/TradinBot`, commit
  `f085c6668517c9384229a4b1300767296092e323`.
- Source file: `historical_data_BTCUSDT_1m_20220701_to_20250630.csv`.
- SHA-256: `a7c45107b3095a75f1a65f675e01cf1e8982a031587ef7f2ca2f7b3b9cf7a0fe`.
- Despite its filename, the file itself contains 1,317,520 closed one-minute bars
  from 2022-07-01 00:00:00 through 2024-12-31 23:59:00.
- Timeframes: all configured frames — `1m,3m,5m,15m,30m,1h,2h,4h,1d`.
- Policy: chain-v6 `mbl_centre_cross_v1` HVB proof; chain-v5 direct paired-
  endpoint→sweep approach with zero intervening pivots; exactly one same-side
  sweep; same-candle directed MBL-cross HVB/body break; strict structural-context
  and regime gates; order-block or eligible-breaker mitigation; lifecycle-v7
  target model `hvb_bos_leg_extreme_v1`.

`mbl_centre_cross_v1` stores all four crossing values. For bullish HVB,
`previous fast <= previous MBL` and `current fast > current MBL`; bearish is the
exact mirror. Crossing the fixed RSI-50 axis, slow line, or either outer blue band
cannot satisfy this proof. HVB remains mandatory in score mode and cannot be
disabled by saved settings.

## Result

| measure | result |
|---|---:|
| setups retained | 64 |
| mitigated / decided | 63 / 63 |
| still pending and unmitigated | 1 |
| wins at frozen confirmation-leg H/L | 17 |
| stopped | 46 |
| win rate | **26.98%** |
| mean R per decided trade | **−0.310R** |
| mean winning R | +1.5586R |
| winning-R range | +0.0009R to +8.8204R |
| 95% interval for mean R/trade | −0.713R to +0.094R |
| statistical verdict | **not proven** |

Every row is `chain_version: 6`, `lifecycle_version: 7`, uses HVB model
`mbl_centre_cross_v1` and target model `hvb_bos_leg_extreme_v1`, and carries
approach model `direct_from_paired_endpoint_v1` with
`approach_pivot_count: 0`. A direct database audit found:

- zero rows whose four stored HVB values failed the claimed moving-MBL cross;
- zero rows with missing/non-zero direct-approach pivot proof;
- zero rows whose target extreme occurred on or after mitigation.

The 17 wins had 17 distinct R values, confirming variable confirmation-leg
scoring. The one unmitigated setup remains `pending` and is not counted as a win
or loss. The interval is the two-sided normal approximation
`mean ± 1.96 × sample-SE` over the 63 decided R outcomes; it is descriptive, not
a guarantee.

The setup count is unchanged from the superseded lifecycle-v6 replay because
that replay's shipped configuration already used `hvb_reference: mbl`. The
correction is still policy-significant: the fixed-50 alternate trigger is now
removed, score mode cannot replace HVB, and every retained row must carry
mathematical same-candle MBL-cross proof rather than an unauditable `hvb_side`
label.

| timeframe | wins | stopped | pending |
|---|---:|---:|---:|
| 1m | 10 | 23 | 0 |
| 3m | 4 | 12 | 0 |
| 5m | 0 | 7 | 0 |
| 15m | 3 | 1 | 1 |
| 30m | 0 | 2 | 0 |
| 1h | 0 | 0 | 0 |
| 2h | 0 | 1 | 0 |
| 4h | 0 | 0 | 0 |
| 1d | 0 | 0 | 0 |

## Reproduction

```bash
curl -fL -o /tmp/tradinbot-v7.tgz \
  https://codeload.github.com/Thaum-blip/TradinBot/tar.gz/f085c6668517c9384229a4b1300767296092e323
rm -rf /tmp/tdigm-v7-source && mkdir -p /tmp/tdigm-v7-source
tar xzf /tmp/tradinbot-v7.tgz -C /tmp/tdigm-v7-source --strip-components=1
# Then, from tdigm-live:
python tools/import_csv.py \
  /tmp/tdigm-v7-source/historical_data_BTCUSDT_1m_20220701_to_20250630.csv \
  --symbol BTCUSDT --start 2022-07-01 --end 2025-06-30 \
  --out /tmp/tdigm-v7-candles
python tools/backfill_archive.py /tmp/tdigm-v7-candles \
  --symbol BTCUSDT \
  --timeframes 1m,3m,5m,15m,30m,1h,2h,4h,1d \
  --hvb-window 0 --trend-context --db /tmp/tdigm-v7.sqlite3
```

## Limits

This sample is one instrument from one historical source, has no fee/slippage
model, and produced no setups on 1h, 4h, or 1d. It cannot establish the current
win rate across all active MEXC perpetuals. Its confidence interval includes
zero, so even this sample does not demonstrate positive expectancy. A production
claim requires a fresh chain-v6/lifecycle-v7 archive or a much broader,
exchange-matched replay; all older policy rows remain excluded.
