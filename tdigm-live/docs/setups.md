# Setups: found, drawn, and kept honest

This page answers the four things that were wrong with the previous round, in the
order they bit:

1. the app could only ever show setups that confirmed **while it was running**;
2. a failed history request left an instrument **empty for the whole session**,
   silently;
3. it read **spot** prices while you trade the **perpetual**;
4. the page had no drawing, so "where did it break, where was the HVB, where is
   the setup" could not be answered at a glance.

## 1. A setup that already exists is found at start-up

The engine used to `seed()` history into the candle buffer and evaluate only the
candles that arrived afterwards. If a setup confirmed ten minutes before you
opened the page — the 1H BTC order block on your own chart — it did not exist as
far as the bot was concerned.

Warm-up now **replays** the fetched history through the same pipeline live uses:

```
fetch 5,000 closed candles (per instrument per timeframe; paged where needed)
  -> feed them through Cell (TDIGM, regime, structure, trend map)
  -> on every bar where the structure confirms a setup, run the real _evaluate
  -> run the setup lifecycle over the rest of the history
  -> adopt that rebuilt cell as the live cell
```

So a setup confirmed 8 hours ago is found, and it is found by the *same code*
that will judge the next one. `tests/test_setups.py::test_backfill_and_live_agree_on_the_same_candle`
pins that: replay and live must produce the identical setup from the same candle.

**Window.** 5,000 bars per timeframe (about 52 days of 15m, 208 days of 1h,
and 833 days of 4h, where the contract has that history). MEXC contract history
is capped at 2,000 rows per response, so the feed pages backwards and de-duplicates
windows. An untouched setup has no age-only expiry in the shipped configuration;
it remains valid until mitigation or price invalidation, and every valid archive
row is rehydrated after restart so new candles keep its state current.

## 2. A failed request is retried, and reported

Every history call is tried **three times** with a backoff, for every timeframe.
Anything still empty is recorded in `warm_failures` and shown in the UI as
`instrument(s) unreachable`, and a background sweep retries every 60 seconds.
`tests/test_setups.py::test_a_failed_history_request_is_retried_instead_of_left_empty`
covers the 429-then-recovery case.

This is one cause of "hundreds of symbols, zero setups": thousands of REST
windows are needed at start-up, and one rate-limited minute used to be permanent.
The live socket now starts immediately; per-symbol closed bars are buffered while
that symbol warms, so the deep scan does not miss new signals.

## 3. Perpetuals, not spot

`market: "futures"` is the default in `config.json`. The feeds are the contract
ones:

| exchange | REST | stream | notes |
|---|---|---|---|
| MEXC | `contract.mexc.com/api/v1/contract/kline/BTC_USDT` | `wss://contract.mexc.com/edge` `sub.kline` | symbols are `BTC_USDT`; mapped to `BTCUSDT` at the edge of the feed |
| Binance | `fapi.binance.com/fapi/v1/klines` | `wss://fstream.binance.com` `@kline_1m` | `contractType: PERPETUAL`, `status: TRADING` |

`python run.py serve --market spot` still works if you ever want the other book.
Timeframes are 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h and 1d. MEXC-native
history is used where available; 3m is built from 1m and 2h from clock-aligned
1h history. The live 1m stream rolls every configured timeframe locally.

**Honest limitation:** the exchange hosts are not reachable from where this was
developed, so the futures feeds are exercised against recorded payload shapes
(`tests/test_futures.py`), not against the live endpoint. `python run.py check`
is the one command that proves it on *your* machine, and it now tests the
contract endpoints because the default market is futures.

## 4. Valid vs invalid — a setup is a living object

A setup is not a moment. Each one becomes a record with a state:

| state | meaning | still tradeable? |
|---|---|---|
| **pending** | neither the order block nor eligible breaker has been touched; no trade/target exists | **yes** |
| **active** | mitigation fixed the entry and armed the 1:4 target | **yes** |
| win | the post-mitigation 4R target was reached | no |
| stopped | the stop was reached after mitigation | no |
| expired | optional positive `setup_max_bars` limit elapsed (disabled by default) | no |
| invalidated | stop was reached while still unmitigated; no trade was filled | no |

`valid` = pending or active. `unmitigated` specifically means pending. Every row
stores `state_reason`, so “invalidated” is never an unexplained label: an integrity
quarantine names the broken proof, while a lifecycle invalidation says that the
stop preceded every eligible zone. Unfilled invalidations do not enter the win
rate. Nothing is deleted when a setup dies.

### The order block

The zone is **the last candle in the opposite direction before the displacement
that broke structure** — the classic definition, and the red box on your chart:

* a SHORT's block is the last *bullish* candle before the down break (supply),
  so it sits **above** the entry, between price and the stop;
* a LONG's block is the last *bearish* candle before the up break (demand).

If there is no opposite candle in the previous 12 bars (a straight run), the
break candle itself becomes the zone and the card says so.
`bot/setups.py::order_block` is the only implementation.

### The aggressive-shift breaker block

The book's pp.88–89 place the breaker on the left of an aggressive market
structure shift and explicitly say that an aggressive momentum shift makes the
breaker strong. For this paired-leg strategy that is deterministic: the candle
that formed the same-leg endpoint now being body-broken is the breaker candidate.
It is enabled only when the directed break candle body is at least
`breaker_body_atr` (default 1.0) times the preceding ATR and the zone lies between
displacement close and stop. A normal break does not invent a breaker.

The order block and breaker are parallel mitigation choices. The first proximal
edge touched after confirmation fills the setup. For SHORT that is a zone's low;
for LONG it is its high. At that moment—and not at the break close—the engine
freezes risk and computes `target = entry ± 4 × |entry − stop|`. If a mitigation
candle also reaches stop and target, stop wins; if it reaches a zone and stop,
the row is `stopped`, not the factually wrong “unfilled invalidated”.

## 5. What the page draws

For the selected setup, on real candles:

| mark | what it is |
|---|---|
| amber `BOS↑` / `BOS↓` | every 20-bar structure break in the window (not just the one in the setup) |
| purple dot | every TDIGM HVB — the price line crossing the centre band — in the window |
| blue box | order block, from the last opposite candle before displacement |
| purple box | paired-endpoint breaker, only for an aggressive shift |
| dashed purple | the liquidity level that was swept |
| dashed white / red / green | mitigation entry, stop, and fixed 4R target; entry/target begin at mitigation |
| vertical line | the candle the setup confirmed on |

The markers are computed with the same functions the live engine uses
(`break_signal`, the TDI centre cross), so the chart cannot show a break the
engine would not have seen (`Engine._markers`).

Every instrument/timeframe cell in the matrix is clickable and opens its chart,
so a cell that says `range` or `sweep high` can be checked against the candles
that produced it.

## 6. Filters on top, and more instruments

The bar above the setups list:

* **find** — symbol search;
* **timeframe chips** — all / 1m … 4h (multi-select);
* **side** — both, long only, short only;
* **state** — valid (pending + filled), pending only, filled only, every state;
* **sort** — newest, best score, best R:R, A→Z;
* **valid only** — the default (a setup you cannot act on is not a signal);
* **HVB required** — only setups whose confirming candle printed an HVB;
* **↻ rescan** — re-read every instrument's history and re-run the scan with the
  current rules (also runs automatically after a filter change);
* the exchange-universe controls remain available for manual overrides, while
  `auto_universe: true` loads every active USDT perpetual at boot.

`max_symbols` is 2000 in `config.json`, deliberately above the expected MEXC
futures universe. Adding the full universe does not block the page: cells show
*warming*, live closes are buffered, and the retry sweep revisits failures.

## 7. Reproduce the numbers

```bash
cd tdigm-live
python run.py check                 # contract REST + websocket reachable?
python run.py serve                 # LIVE perps, 9 timeframes
python -m pytest tests -q           # deterministic offline suite
```

The setup rules themselves are unchanged from round 17 and still measured in
[`trend-rules.md`](trend-rules.md): `docs/trend-rules.md` explains which of those
gates the data supports and which it does not, and the page always shows the
evidence for a setup rather than asking you to trust it.
