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
| a decided outcome is final within one lifecycle version | ordinary rescans cannot rewrite it; a versioned exit-rule change intentionally requires deep replay | `test_a_decided_outcome_is_final`, `test_a_new_lifecycle_version_can_regrade_an_old_final_outcome` |
| a broken disk never stops the scanner | the store degrades to `enabled=False` and reports the problem; `/api/health` shows it | `test_a_broken_disk_never_stops_the_scanner` |
| old databases keep working | columns added later are migrated in place, never by recreating the table | `MIGRATIONS` in `bot/store.py` |

The "final" rule matters more than it looks. Ordinary rescans cannot turn a win
back into `pending`. The exception is explicit `lifecycle_version` advancement:
v2 changed the fill to mitigation and used a fixed 4R target; v3 added one
selected structural sweep and breaker-local post-fill stops; v4 added
degree-aware clean outer legs plus full remembered trend consistency. Lifecycle
v5 replaces fixed R with the favorable H/L of the sweep→same-candle HVB/BOS leg,
extended only by fully closed pre-mitigation candles and frozen before the
mitigation candle. Lifecycle v6 / chain v5 additionally required a direct move
from the paired opposite endpoint into the sweep, with no intervening pivot;
only the post-sweep path may oscillate. Lifecycle v7 / chain v6 removes alternate
HVB triggers and stores mathematical proof that the purple line crossed the
moving MBL on the body-break candle. Rows graded under any older rule must be
replayed rather than contaminating the current win rate.

## Reading it back

* `GET /api/signals?limit=&symbol=&timeframe=&grade=&state=&side=&valid=&since=&search=`
  returns the rows plus `counts` (by state, by grade) and `results`.
* `GET /api/signals.csv?limit=` the same rows as CSV, because an archive you
  cannot take out is a trap.
* The page has **include archive** (on by default) in the filter bar. The live
  list and the stored list are merged by key; a live row always wins because it
  is newer. State filters expose pending, active, confirmation-leg target wins,
  stops and unfilled invalidations separately, and every row displays its stored
  `state_reason`.

The archive line on the setups card is computed from current-version rows, not
from memory:

> win rate = mitigated confirmation-leg target wins / (those wins + mitigated stops)

Risk/reward does not begin on the body-break candle. The target starts as the
favorable extreme of the sweep→same-candle HVB/BOS displacement leg and may
extend on fully closed candles while pending. A later order-block or eligible
aggressive-breaker touch freezes entry, risk, and the last extreme before that
mitigation candle. The mitigation candle itself is excluded. A pre-mitigation
stop is `invalidated`, carries no realised R and is excluded. Legacy fixed-R
studies remain below as labelled historical experiments, but they are not mixed
into lifecycle-v7 / chain-v6 results.

## Filling it from history

The live page can only record what it lives through. `tools/backfill_archive.py`
replays the recorded CSVs through the *same engine and filters*, writes every
setup, and then walks each one forward over the candles that followed to decide
`win` / `stopped` / `expired`:

```
python tools/backfill_archive.py data/btc --symbol BTCUSDT            # 107 setups
python tools/backfill_archive.py data/btc --symbol BTCUSDT --dry-run  # writes nothing
```

An older BTCUSDT 2023-01-01 → 2025-06-30 report produced **107 setups, 43 won,
54 stopped, 10 never filled under legacy 2R scoring**. It is retained only as a
historical audit; the backfill command now applies lifecycle v7 / chain v6 and
is not expected to reproduce those counts. A compatible real dataset is not
present in this checkout, so that legacy archive cannot provide a current
confirmation-leg-target rate. A separately fetched, hash-pinned BTCUSDT sample
was replayed under lifecycle v7 / chain v6 as a deterministic regression; its
26.98% win rate and −0.310R/trade were statistically **not proven** and must not
be extrapolated to the production universe. All 64 retained rows had zero pivots
between paired endpoint and sweep and valid four-value moving-MBL cross proof.
See [`lifecycle-v7-regression-2026-11-04.md`](lifecycle-v7-regression-2026-11-04.md).

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

The archive made sparsity measurable. The following table is a **legacy gate
experiment**, retained for reproduction only; it used fixed 2R and does not
represent the current same-candle HVB, trend-gated, mitigation-first
confirmation-leg-target policy:

| variant | signals | win% | R/trade |
|---|---|---|---|
| HVB had to print on the break candle | 68 | 41.7% | 0.25 |
| **HVB within 3 bars of the break** | 72 | 40.6% | 0.22 |
| **+ external HH/HL trend gate off** | **107** | **42.6%** | **0.28** |

Those numbers do not override the shipped rules: `hvb_window: 0` requires the
same candle, and current trend/structure gates remain configured in
`config.json`. New lifecycle-v7 / chain-v6 results are reported separately only
after a fresh deep replay.

### A caveat about the grades

A = the external structure agreed (HH/HL or LH/LL), B = it did not. On this data
grade B (14 wins of 19, +1.21R) did better than grade A (29 of 78, +0.115R). That
is 19 setups - far too few to conclude that B is the better trade, and the
direction of the difference depends on the sample. Grade is a **description of
the setup**, not a promise; the page shows both so you can judge it rather than
being told.
