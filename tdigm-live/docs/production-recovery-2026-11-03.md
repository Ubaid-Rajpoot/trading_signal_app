# Production recovery — universe coverage and zero-signal diagnosis

Date: 2026-11-03

This note records the failure chain, the repair, and the limits of the evidence. It is not a profitability claim.

## What made the page stop at 45 contracts

`config.json` contained 45 configured fallback symbols. Automatic discovery made one attempt at startup. Any exception or empty result was logged, but the process silently continued with those 45 rows and never retried.

The MEXC Futures parser also intersected authoritative `/api/v1/contract/detail` rows with `/api/v1/contract/ticker`, then discarded contracts with a missing ticker or non-positive `amount24`. A partial ticker response could therefore reduce hundreds of enabled contracts to a small plausible-looking subset without an error.

The repair makes contract detail authoritative:

- every enabled, visible USDT contract is included;
- ticker data ranks/enriches contracts but cannot remove one;
- malformed/empty detail raises an explicit feed error;
- a ticker failure retains the complete detail universe and is reported in diagnostics;
- automatic discovery rejects a suspiciously small result, keeps the configured fallback, shows a persistent UI warning, and retries;
- `/api/health` and `/api/universe` report mode, available/selected/fallback counts, attempts, timestamps, the last error, and feed parser diagnostics;
- successful discovery is refreshed periodically and still obeys `max_symbols`.

The sandbox cannot reach MEXC directly (TLS EOF), so no claim is made here about today's exact exchange count. Recorded response-shape tests prove that a 520-contract detail response remains 520 contracts even when the ticker endpoint fails. Production health now exposes the actual count instead of disguising failure as a 45-contract universe.

### Malformed Brotli response follow-up

Production subsequently exposed a transport-specific failure from MEXC contract detail: `brotli: decoder process called with data when can_accept_more_data() is False`. It means the server/proxy returned an invalid Brotli-compressed body (bytes after the decoder's end marker); it does not mean the JSON parser or trading rules failed. `httpx` advertised Brotli only on hosts where an optional Brotli package happened to be installed, which explained why the failure differed by machine.

The pooled REST client now advertises only `gzip, deflate`. If any compressed response still raises `httpx.DecodingError`, the same request is immediately retried with `Accept-Encoding: identity`. A regression test injects the production decoder exception and proves the identity retry succeeds, allowing automatic universe refresh to leave the 45-symbol fallback.

## Why zero signals was not accepted as a market conclusion

The setup funnel was exercised over a deterministic synthetic stress replay: 40 distinct 1-minute series × 5,000 bars = 200,000 closed candles. This is a starvation/regression test, not market evidence and not a backtest.

At commit `77d3e04` the replay produced:

- 14,744 liquidity sweeps;
- 1,056 exact paired-leg opposite body breaks;
- 0 signals.

The dominant conjunction was not an absence of structure. A fresh major range-breaking body close was correctly accepted by the structural gate, but the same candidate was then rejected because lagging ADX/ER still called the just-ended range a range. That contradicted the explicit policy that a clear major/macro range release may establish a new direction.

After the narrow correction, the identical generated candles produced:

- the same 14,744 sweeps;
- the same 1,056 paired body breaks;
- 9 signals.

Nothing was loosened for ordinary range candles. The exception requires a body close beyond the full remembered major/macro range edge, in the trade direction, within `fresh_shift_max_bars`. Internal BOS, EMA direction, HVB alone, and momentum candles cannot invoke it. Same-candle directed HVB, exact same-leg pairing, exactly-one-sweep proof, depth, structure direction, mitigation-first activation, and breaker-local stops remain mandatory. Lifecycle v5 subsequently replaced the then-current fixed 4R exit with the frozen pre-mitigation confirmation-leg H/L target. Lifecycle v6 / chain v5 subsequently required the paired endpoint→sweep approach to be direct, with no intervening pivot. Lifecycle v7 / chain v6 now also requires auditable previous/current purple-line and moving-MBL values for the same-candle HVB; see `docs/setups.md`.

The live funnel now reports:

`structural sweeps → paired body breaks → same-candle qualified HVBs → filter-passed signals`

alongside rejection counters and warm-up coverage. A future zero can therefore be separated into no data, no structure, no aligned HVB, or a named filter rejection.

## Full-universe warm-up

Deep-warming one symbol through all nine timeframes before moving to the next left most of a large universe blind. Startup is now two-pass and breadth-first:

1. load `startup_history_bars` (default 1,000) across every contract/timeframe;
2. deep replay the configured `history_bars` (5,000) across every contract.

Newly added batches use the same shallow-then-deep queue. Live closed bars remain buffered during either rebuild, so the speed improvement does not create a candle gap. The second pass still performs the longer historical scan and finds older unmitigated qualifying setups.

## Verification boundary

Automated tests use recorded MEXC response shapes and deterministic candles because this runner cannot make a live MEXC TLS connection. The production endpoints now provide the missing live facts: discovered count, parser diagnostics, websocket frames, ready/partial/empty symbols, failed cells, queue depth, and exact rejection funnel.
