# Chain-v8 production signal-flow repair — 2026-11-01

## What was actually wrong

The scanner used one uniform deep-replay floor: 5,000 bars per timeframe. That sounds deep but represents very different calendar periods. On MEXC, 3m is derived from 1m, so 5,000 3m bars cover only 10.4 days. The strict v4.1 + chain-v8 signal present on 2024-12-11 was outside the old 2024-12-21 through 2024-12-31 startup window. The policy could publish it, but production never replayed far enough to see it.

The fix keeps the 5,000-bar floor and adds a 30-calendar-day floor for deep replay. The breadth-first 1,000-bar startup remains unchanged, so every contract becomes live before the expensive pass. On the lower timeframes the deep targets are now:

| timeframe | old target | new target | calendar coverage |
|---|---:|---:|---:|
| 1m | 5,000 | 43,200 | 30 days |
| 3m (derived) | 5,000 | 14,400 | 30 days |
| 5m | 5,000 | 8,640 | 30 days |
| 15m+ | 5,000 | at least 5,000 | 52 days or more |

The shared 1m request for native 1m and derived 3m is 43,209 bars, below the feed's guarded 50,000-bar maximum. MEXC's 2,000-row cap is still paged by the existing production feed.

No v4.1 gate was disabled or weakened. The same-candle moving-MBL HVB, trend context, HH/HL or mirror structure, BOS, depth, regime, external-structure direction, and both 1.0-prior-ATR directed displacement proofs remain mandatory.

## Corrected decision funnel

The old diagnostic called every failed-gate hit a “filtered setup.” One setup can fail five gates, so the reported 757 was not 757 completed setups. The engine now records:

- completed setups;
- same-candle HVB-aligned setups;
- filtered setups;
- passed setups;
- exactly one first decisive blocker per rejected setup;
- every overlapping rejection hit;
- blocker combinations;
- all of the above per timeframe.

`/api/health`, `/api/watch`, engine snapshots, and `tools/scan_history.py` expose the corrected funnel. The dashboard's “top rejection” now uses first blockers rather than inflated overlapping hits.

## Full real-history replay

Source: 1,317,520 public BTCUSDT one-minute bars, 2022-07-01 through 2024-12-31 (915 days), rolled into all nine production timeframes. The replay ran the real engine with the shipped v4.1 filters and chain-v8 displacement policy:

```bash
python tools/scan_history.py /tmp/tdigm-btc \
  --symbol BTCUSDT \
  --timeframes 1m,3m,5m,15m,30m,1h,2h,4h,1d \
  --target range --no-pool --no-score
```

| timeframe | completed | HVB-aligned | filtered | passed |
|---|---:|---:|---:|---:|
| 1m | 120 | 18 | 120 | 0 |
| 3m | 44 | 10 | 42 | 2 |
| 5m | 25 | 4 | 25 | 0 |
| 15m | 7 | 2 | 7 | 0 |
| 30m | 4 | 1 | 4 | 0 |
| 1h | 3 | 1 | 3 | 0 |
| 2h | 1 | 0 | 1 | 0 |
| 4h | 1 | 0 | 1 | 0 |
| 1d | 0 | 0 | 0 | 0 |
| **total** | **205** | **36** | **203** | **2** |

First decisive blockers were 196 unclear-left-structure failures, six missing same-candle HVBs, and one underpowered centre cross. The larger ADX, ER, depth, HVB, and external-direction values are overlapping gate hits, not additional setups. This confirms that publication is rare on one symbol because the required v4.1 intersection is strict—not because persistence or the page discards accepted signals.

## Production-window reproduction

Replaying the final 30 days of the same corpus through all nine timeframes finds the strict chain-v8 LONG on 3m:

```text
43,200 1m bars / 30 days
8 completed setups -> 3 same-candle HVBs -> 7 filtered + 1 passed
3m: 1 passed LONG
signal time: 2024-12-11 13:32:59.999 UTC
sweep displacement: up, 1.16 prior ATR
paired-break displacement: up, 4.92 prior ATR
```

The former 10.4-day 3m replay could not contain this event. The new 30-day window does.

A compact unmodified 3,010-row slice is checked into `tests/fixtures/BTCUSDT-3m-chain8-2024-12.csv.gz`. `tests/test_historical_signal.py` runs that data through MEXC-style 1m-to-3m derivation, every shipped v4.1 gate, chain-v8 publication integrity, lifecycle registration, and SQLite persistence. It asserts the exact signal timestamp and both immutable directed-displacement proofs; the fixture contains no synthetic alert.

## Validation boundary

This environment could not establish a live TLS session to MEXC, so the evidence above is real recorded-market replay through the production code, not a claim of current live-MEXC connectivity. Feed parsing, futures universe selection, pagination, warm-up concurrency, live buffering, publication, persistence, APIs, and UI delivery remain covered by deterministic tests and the full page smoke test.
