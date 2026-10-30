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
fetch 600 closed candles (per instrument per timeframe)
  -> feed them through Cell (TDIGM, regime, structure, trend map)
  -> on every bar where the structure confirms a setup, run the real _evaluate
  -> run the setup lifecycle over the rest of the history
  -> adopt that rebuilt cell as the live cell
```

So a setup confirmed 8 hours ago is found, and it is found by the *same code*
that will judge the next one. `tests/test_setups.py::test_backfill_and_live_agree_on_the_same_candle`
pins that: replay and live must produce the identical setup from the same candle.

**Window.** 600 bars per timeframe (6 days of 15m, 25 days of 1h, 100 days of 4h).
That is ten times the 60-bar life of a setup, so nothing tradeable can be missed
inside it. Older history is not scanned: a setup that old is not a setup anymore.

## 2. A failed request is retried, and reported

Every history call is tried **three times** with a backoff, for every timeframe.
Anything still empty is recorded in `warm_failures` and shown in the UI as
`instrument(s) unreachable`, and a background sweep retries every 60 seconds.
`tests/test_setups.py::test_a_failed_history_request_is_retried_instead_of_left_empty`
covers the 429-then-recovery case.

This is the one that made "500 symbols, zero setups" possible: 500 instruments ×
7 timeframes is 3,500 REST calls at start-up, and one rate-limited minute used to
be permanent.

## 3. Perpetuals, not spot

`market: "futures"` is the default in `config.json`. The feeds are the contract
ones:

| exchange | REST | stream | notes |
|---|---|---|---|
| MEXC | `contract.mexc.com/api/v1/contract/kline/BTC_USDT` | `wss://contract.mexc.com/edge` `sub.kline` | symbols are `BTC_USDT`; mapped to `BTCUSDT` at the edge of the feed |
| Binance | `fapi.binance.com/fapi/v1/klines` | `wss://fstream.binance.com` `@kline_1m` | `contractType: PERPETUAL`, `status: TRADING` |

`python run.py serve --market spot` still works if you ever want the other book.
Timeframes are now 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h (and 1d for a replay);
anything the exchange has no interval for is rolled up from 1m.

**Honest limitation:** the exchange hosts are not reachable from where this was
developed, so the futures feeds are exercised against recorded payload shapes
(`tests/test_futures.py`), not against the live endpoint. `python run.py check`
is the one command that proves it on *your* machine, and it now tests the
contract endpoints because the default market is futures.

## 4. Valid vs invalid — a setup is a living object

A setup is not a moment. Each one becomes a record with a state:

| state | meaning | still tradeable? |
|---|---|---|
| **pending** | price has not come back into the order block yet | **yes** |
| **active** | price is in the zone (the entry is filled) | **yes** |
| win | the target was reached | no |
| stopped | the stop was hit | no |
| expired | 60 bars went by without the zone being touched | no |
| invalidated | the stop was taken before the entry was ever filled | no |

`valid` = pending or active, and that is exactly the "it hasn't been mitigated
yet" test: mitigation means price traded back into the block. Nothing is deleted
when a setup dies — the card keeps it, with the state and the bar it happened on,
because "this idea died here" is information.

### The order block

The zone is **the last candle in the opposite direction before the displacement
that broke structure** — the classic definition, and the red box on your chart:

* a SHORT's block is the last *bullish* candle before the down break (supply),
  so it sits **above** the entry, between price and the stop;
* a LONG's block is the last *bearish* candle before the up break (demand).

If there is no opposite candle in the previous 12 bars (a straight run), the
break candle itself becomes the zone and the card says so.
`bot/setups.py::order_block` is the only implementation, and
`tests/test_setups.py::test_order_block_is_the_last_opposite_candle_before_the_break`
pins it.

## 5. What the page draws

For the selected setup, on real candles:

| mark | what it is |
|---|---|
| amber `BOS↑` / `BOS↓` | every 20-bar structure break in the window (not just the one in the setup) |
| purple dot | every TDIGM HVB — the price line crossing the centre band — in the window |
| red/green box | the order block, from the last opposite candle before the break |
| dashed purple | the liquidity level that was swept |
| dashed white / red / green | entry, stop, 2R, and the structure target |
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
* **+ top 500** — add the 500 biggest contracts by 24h turnover, added in the
  background and warmed as they land.

`max_symbols` is 600 in `config.json`; raise it if you want the whole board.
Adding 500 instruments does not block the page: cells show *warming*, and the
sweep retries anything that fails.

## 7. Reproduce the numbers

```bash
cd tdigm-live
python run.py check                 # contract REST + websocket reachable?
python run.py serve                 # LIVE perps, 7 timeframes
python -m pytest tests -q           # 122 tests
```

The setup rules themselves are unchanged from round 17 and still measured in
[`trend-rules.md`](trend-rules.md): `docs/trend-rules.md` explains which of those
gates the data supports and which it does not, and the page always shows the
evidence for a setup rather than asking you to trust it.
