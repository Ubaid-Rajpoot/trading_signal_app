# Round 11 — signals as a grid, and a tab for what is live

The complaint: *"signals are all in one line; if there are 200, do I slide along to
see them one by one? Can't there be a grid view? And a separate side tab for the live
signals, showing what is happening in each one — everything should be clearly
visible."*

Three changes, all in `web/index.html` plus a small paging addition to the read API.

## 1. The side panel is tabbed

The right-hand column is now **Signals**, with three tabs:

| tab | what it answers |
| --- | --- |
| **Live now** | every signal that is happening *right now*, one row each: entry, target, stop, distance of each from the live price, expected R after fees, R:R, how far price still is from the entry (bar), and whether it is **WAITING** or **FILLED** |
| **Archive** | the database, as a **paged grid of tiles** with server-side filters (everything / still open / target hit / stopped / invalidated) and a grid⇄list switch |
| **Tape** | the old live tape, moved here so the middle column is the chart and nothing else |

The live tab is the answer to *"kis me kia huwa"*: the four numbers were buried in a
paragraph inside the card you had to open. They are now a row per signal — and a row
click still opens the chart for that zone.

## 2. A grid, not a sliding line

* The archive is a **responsive grid**: `repeat(auto-fill, minmax(min(100%, 218px), 1fr))`
  — as many tiles across as the panel allows (two in the wide third column, more in the
  full-window drawer), so 200 signals are *scanned*, not scrolled in one column.
* A tile prints symbol, direction, timeframe, measured chance, state badge, result in R,
  grade, entry, target, stop and formation time. Reading order is the same as a card,
  one third of the height.
* **Paging is server-side.** `GET /api/journal` gained `offset` (and returns `total`),
  so the grid says `1–12 of 213 · page 1 of 18` and a page costs one query. Filters are
  server-side too: "target hit" over 5,000 rows is one indexed query, not a client-side
  scan.
* The **Journal drawer** is the full window: 1,000 px wide, the same grid, its own
  filters, its own pager, and a grid⇄list switch. That is where you go to look at the
  whole history.
* The **strip at the top** is a grid too: up to 12 tiles across the page, live first,
  then the newest from the archive, with `all 213 signals →` jumping to the archive tab.

## 3. Everything on the grid explains itself

A drawer in the Archive tab, **"what the badges mean"**, states plainly what LIVE /
TARGET HIT / STOPPED / MITIGATED / INVALIDATED / INTERRUPTED mean — including the two
that matter when a signal "disappears":

* **INVALIDATED** — a candle *closed* beyond the block, so the level that made the setup
  strong is gone. If the plan had filled, that is the stop being taken (−1R); if it never
  filled, the trade never triggered at all.
* **INTERRUPTED / NOT WATCHED** — the scanner restarted, or the instrument was removed,
  while the plan was open. The row is kept; it is simply not followed any more.

## The bug this round found

The quality suite started failing with *"every zone carries a grade: –"*. The cause was
real: `snapshot()` enriched only the **live** zones, so a resolved zone reached the page
(and the archive tile behind it) with **no measured grade and no market tag**. Everything
that is actually sent is enriched now, resolved or not, and
`tests/test_engine.py::test_a_resolved_zone_still_carries_its_grade_and_market` pins it.

## Reproduce

```bash
cd smc-radar
../.venv/bin/python -m pytest tests -q          # 92 passed
rm -f data/journal.sqlite3*
../.venv/bin/python -u run.py replay data/fut 60 --ticks 6 --port 8800 &
cd tools/ui && npm install --silent jsdom ws
node layout.mjs && node quality.mjs && node algo1m.mjs && node transports.mjs
```

`layout.mjs` gained checks for this round: the archive is a grid of tiles (not one long
line), the side panel has three tabs, the live tab lists every live signal with its
levels, the grid states `1–12 of N`, grid⇄list switches the same rows, and the tabs
actually switch the panes.

**Not verified:** no browser engine in this sandbox, so the exact pixel count per row is
reasoned from the CSS, not photographed. The DOM, the data behind it, the paging, the
filters and the tab behaviour are all covered.
