# The archive: every signal, kept

The complaint this round started from was *"sometimes no setup appears for
hours"*. Sparsity is real and is addressed in `docs/setups.md` and
`docs/trend-rules.md`. But sparsity alone did not explain how it *felt*: the page
was a window on the running process. A setup that printed at 03:10 and was still
on your chart at 09:00 was gone from the app if the server restarted, if you
reloaded a stale tab, or if the lifecycle finally closed it. Nothing was stored,
so nothing was shown, so nothing could be reviewed.

This round the scanner got a database and the page got a history.

## What is stored

Every setup is written to SQLite **the moment it is created**, and the same row
is updated for the rest of its life:

```
pending ──► active ──► win | stopped
   └──────► expired | invalidated
```

One row per setup, keyed by `symbol|timeframe|created_ms`. The row holds what you
filter on (`symbol`, `timeframe`, `side`, `grade`, `state`, `valid`, `mitigated`,
`score`, `rr`, dates) and the **whole setup as JSON** in `payload`, so a row is
readable on its own years later even if the columns change.

Default location: `tdigm-live/data/tdigm.sqlite3` (`database` in `config.json`,
or the `TDIGM_DB` environment variable). `data/` is git-ignored, so the archive
is yours and is never pushed.

Settings: WAL journalling and `synchronous=NORMAL`. A write happens every time a
setup is created or changed state - a scan that touches 500 instruments must not
be held up by an fsync, and a crash must not corrupt the file.

## What is guaranteed

| guarantee | how | test |
|---|---|---|
| a signal is saved as soon as it exists | `Engine._remember()` on publish and on every state change | `test_the_engine_writes_through_and_reads_back_on_boot` |
| a restart shows the archive **before any candle arrives** | `load_stored()` at build time, `/api/signals` reads SQLite | `test_the_archive_outlives_the_process` |
| a decided outcome is final | a `win`/`stopped` row is never overwritten by a re-scan replaying the same history | `test_a_decided_outcome_is_final` |
| a broken disk never stops the scanner | the store degrades to `enabled=False` and reports the problem; `/api/health` shows it | `test_a_broken_disk_never_stops_the_scanner` |
| old databases keep working | columns added later are migrated in place, never by recreating the table | `MIGRATIONS` in `bot/store.py` |

The "final" rule matters more than it looks. Rescanning 18 months of BTC replays
hundreds of setups that already ended; without it, a win would silently become
`pending` again and the win rate on the page would drift every time you pressed
**rescan**.

## Reading it back

* `GET /api/signals?limit=&symbol=&timeframe=&grade=&state=&side=&valid=&since=&search=`
  returns the rows plus `counts` (by state, by grade) and `results`.
* `GET /api/signals.csv?limit=` the same rows as CSV, because an archive you
  cannot take out is a trap.
* The page has **include archive** (on by default) in the filter bar. The live
  list and the stored list are merged by key; a live row always wins because it
  is newer. Decided setups are labelled `closed at target` / `closed at stop` /
  `closed, never filled`, and **valid only** does not hide them - that switch is
  about open positions, not history.

The archive line on the setups card is computed from the rows themselves, not
from anything in memory:

> 107 setups recorded · 43 won · 54 stopped · hit rate 44% · +0.33R per decided
> trade · grade A 29/78 (+0.115R) · grade B 14/19 (+1.211R)

R is scored against the fixed **2R** target, the convention every measurement in
`docs/` uses. Most setups have no structural target at all (the next liquidity
level is further away than 2R, or inside the entry), so scoring them on their own
target would mix two yardsticks and produce a number that cannot be compared with
the ablation table.

## Filling it from history

The live page can only record what it lives through. `tools/backfill_archive.py`
replays the recorded CSVs through the *same engine and filters*, writes every
setup, and then walks each one forward over the candles that followed to decide
`win` / `stopped` / `expired`:

```
python tools/backfill_archive.py data/btc --symbol BTCUSDT            # 107 setups
python tools/backfill_archive.py data/btc --symbol BTCUSDT --dry-run  # writes nothing
```

BTCUSDT, 2023-01-01 → 2025-06-30, 5m/15m/30m/1h: **107 setups, 43 won, 54
stopped, 10 never filled.**

## Thirty instruments at a time

Warm-up used to be one REST call after another: 500 symbols x 5 timeframes,
serial, and the higher timeframes only started looking when their turn came.
That is a large part of "no setup for hours" - the bot was not looking.

Now:

* `warmup()` and `rescan()` run a **pool of 30** (`workers` in `config.json`).
  `tests/test_parallel.py` asserts the pool really holds 30 in flight, for warm-up
  and for a rescan, and that a single broken instrument (a 429) neither stops the
  queue nor disappears silently.
* The runtime warm-up queue (**+symbols** on the page) is drained by the same 30
  consumers pulling from one FIFO, so an add is picked up within one turn and no
  instrument can starve the ones behind it.
* Live candles arrive over sharded websockets (60-400 symbols per socket
  depending on exchange), so the tick path was already parallel.

Honest caveat: the sandbox has **2 cores**, and the per-bar replay is pure
Python. Thirty workers overlap the *network* wait, which is what dominated; the
CPU part of a replay is still serialised by the interpreter. On a machine with
more cores the same code uses them only as far as the GIL allows. What is
measured and asserted is the pool size and the non-blocking behaviour, not a
speed-up claim I cannot test at 500 symbols.

## More signals

The archive made the sparsity measurable, and the measurement changed a default.
Full table in `docs/trend-rules.md`; the short version, BTCUSDT 1m → 5m/15m/30m/1h
over 18 months, fixed 2R:

| variant | signals | win% | R/trade |
|---|---|---|---|
| HVB had to print on the break candle | 68 | 41.7% | 0.25 |
| **HVB within 3 bars of the break** | 72 | 40.6% | 0.22 |
| **+ external HH/HL trend gate off** | **107** | **42.6%** | **0.28** |

So: the HVB window is 3 bars, and `trend_context` / `require_hh_hl` are now
**off by default**. The gate was removing a third of the signals without paying
for it in expectancy. It is still evaluated on every setup and it is what makes a
setup **grade A** instead of **grade B**, so nothing is lost - the information is
on the card, and the archive reports both grades separately.

Loosening further was measured and rejected: `max_internal_depth` 3 gives 89
signals at 0.18R/trade (window 0) - more rows, worse rows.

### A caveat about the grades

A = the external structure agreed (HH/HL or LH/LL), B = it did not. On this data
grade B (14 wins of 19, +1.21R) did better than grade A (29 of 78, +0.115R). That
is 19 setups - far too few to conclude that B is the better trade, and the
direction of the difference depends on the sample. Grade is a **description of
the setup**, not a promise; the page shows both so you can judge it rather than
being told.
