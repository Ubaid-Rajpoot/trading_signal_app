# Round 9 — futures only, and a signal you cannot lose

Three things were asked for in this round, and one of them turned out to be a real
bug with a real cause:

1. **Remove the spot side completely.** The app is a MEXC **futures** scanner now.
2. **Futures must produce a lot of signals.** They did not, and the reason was not
   the pattern engine.
3. **A signal must never vanish.** Every signal is now written to SQLite when it
   forms and kept forever — and a second bug, which silently erased a signal's
   result, is fixed.

Everything below is measured on the **recorded 1m BTC_USDT futures candles** in
`data/fut/` (real MEXC data), replayed through the same engine the live app runs.

```bash
# the whole round, from a clean checkout
cd smc-radar
../.venv/bin/python tools/make_data.py --source /tmp/TradinBot-main/temp_data
rm -f data/journal.sqlite3*
../.venv/bin/python -u run.py replay data/fut 600 --port 8800 &
curl -s localhost:8800/api/journal/stats
```

## 1. Spot is gone

| where | before | after |
| --- | --- | --- |
| `config.json` | `markets: ["futures","spot"]`, spot symbols | `markets: ["futures"]`, `symbols: []`, 40 contracts, `auto_futures: 60` |
| `bot/feed.py` | `CompositeFeed` with two sockets | `make_market_feed()` returns a bare `MexcFuturesFeed` |
| `run.py` | `--spot` / `--futures` flags | futures only |
| page | two sections, spot KPIs | one section, the spot KPI is now **Archived signals** |
| data | `data/btc/`, `data/fut/` | `data/fut/` only |

`CompositeFeed` itself stays in the module (the tests cover it, and the standalone
`tdigm-live/` app still uses the same idea), but no configuration can select spot.

## 2. Why futures produced no signals — two bugs, not a shortage of setups

**Bug A — the kline window was always one minute.** `MexcFuturesFeed.history()`
asked the contract API for `Min15` candles but computed the number of candles to
request with 60-second candles. 15m therefore received ~66 bars instead of ~1000,
never reached the 240-bar lookback, and produced (almost) no zones. Fixed by
`future_interval()` / `future_seconds()`, which convert the timeframe first.

**Bug B — one failure switched the whole warm-up off.** `Engine.warmup()` returned
early for *every remaining symbol* once eight calls in a row had failed. Sixty
contracts is three hundred REST calls; the contract API rate-limits; the first burst
of failures turned the scan off, and the scanner then waited for the live stream to
fill 240 bars — at 15m that is two and a half days. Now a failure costs **one**
instrument: the pass backs off (0.5 s → 15 s) instead of stopping, every failure is
recorded in `warm_problems`, and a background `healer()` re-requests the instruments
that are still short on history every two minutes.

`GET /api/diagnostics` prints, per contract and per timeframe, the number of bars,
zones, ATR, whether the lookback was reached, and the warm-up error — so "this
contract shows nothing" has an answer on screen instead of being a mystery.

## 3. The stop: "level" → "block"

The user's rule is *stop beyond the structure / the red box*. The shipped default
was `plan_stop_mode: "level"` — 0.10 ATR beyond the exact high/low, which is inside
one candle's noise. On the same 36-day replay, same model, same fee gate:

| stop | signals / day | hit rate | avg R (before fees) |
| --- | --- | --- | --- |
| `level` (0.10 ATR beyond the extreme) | 4.1 | 41.7 % | −0.25 |
| `block` (beyond the whole order block) | **14.8** | **64.7 %** | **−0.08** |

The block stop is 3.6× the signal volume and 23 points of hit rate better, and it
is what the rule says. `"level"` is still selectable in the settings panel.

**This is not a profitable strategy yet, and the numbers say so.** At the block stop
the average plan pays about 0.55 R while it risks 1 R, so it needs roughly 65 % to
break even and it delivers 64.7 % — i.e. flat before fees and negative after them.
The round-8 model bands (A+ 97 %, A 97 %, B 95 %) were measured **in the study**, on
the study's own target rule; they are not what this execution produces, and the page
now prints the archive's own figures next to the model's.

## 4. The database: a signal cannot vanish any more

`bot/journal.py` — one SQLite file (`data/journal.sqlite3`), WAL, stdlib only.

* A row is written the moment a zone is formed with `prob ≥ journal_prob_min`
  (0.60) **or** is a signal. It carries the symbol, market, timeframe, kind, side,
  grade, probability, the measured band (`win`/`n`), conditions met, level, entry,
  **stop**, **target**, R:R, risk %, fee in R, net R, trade-ready, signal flag,
  status, plan state, formed/closed timestamps — plus the whole zone as JSON in
  `snapshot`.
* The same row is updated as the plan fills, reaches its target or takes its stop.
  **Nothing is ever deleted.**
* A broken disk cannot stop the scanner: if the file cannot be opened the journal
  reports `problem` and the app runs without it.

**Bug C — the result was erased.** `sync_journal()` re-writes a zone on every change.
The live zone object still says `result: None` after the plan resolved, and a plain
`UPDATE` therefore blanked the stored outcome on the very next push: the row stayed,
but "target, +1.6 R" was gone. That is exactly the "signals lose their details"
complaint. `result`, `result_r` and `closed_at` are now write-once (`COALESCE`), and
`tests/test_journal.py` pins it.

**Two hit rates, not one.** The archive keeps the C band as well, so counting "hit
rate" over every graded row answers a different question than the scoreboard of what
the page called a signal. `stats()` now reports both (`signal_hit_rate`,
`signal_avg_r` next to `hit_rate`, `avg_r`) and the drawer prints the signal-only
figures first.

API: `/api/journal` (filters `symbol`, `timeframe`, `grade`, `status`, `min_prob`,
`signals_only`, `since`, `limit`, `order`), `/api/journal/stats`,
`/api/journal/{key}`, `/api/journal.csv`, `/api/diagnostics`.

Page: the **Journal** drawer (🗄 button) with symbol / grade / status / window
filters, the CSV export, and the archive KPI in the top strip.

## What is verified, and what is not

Verified (`tests` 78 passed, `tools/smoke.sh`, `tools/ui/*.mjs` all green):

* the page, the candles, the stream, the websocket;
* the journal writes, reads, exports, survives a restart, and never loses a result;
* the warm-up tries every instrument even when the exchange is refusing;
* futures-only configuration builds a futures-only feed.

**Not verified offline:** the live MEXC connection, `auto_expand_futures()` (a
replay has no universe list) and the `healer()` against the real contract API — the
sandbox cannot reach MEXC. All of them fail soft: the app keeps scanning what it has
and `warm_problems` records what went wrong.
