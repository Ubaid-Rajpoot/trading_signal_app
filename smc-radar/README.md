# SMC radar — where the strong highs, algo candles and rejection blocks are

A second standalone app next to `tdigm-live`. It **does not trade** and it does
not invent an entry model: it maps the four structures from the trader's own
order-block book, on every instrument it is given, on every timeframe, and it
keeps each one on screen — with the time it formed — **until price mitigates it**.

```bash
cd smc-radar
python run.py serve                       # live: MEXC, every symbol you add
python run.py replay data/btc 40          # replay recorded real candles
python run.py check                       # can this machine reach the exchange?
python run.py universe --top 20           # what the exchange lists, biggest first
python tools/scan_now.py --preset top20   # one-shot scan, prints a table
```

The page opens on **http://localhost:8800**.

---

## 1. The four patterns, exactly as the book defines them

Page numbers are from `docs/reference/book/ict-institutional-smc-trading.pdf`
(the same PDF at the repo root `docs/reference/book/`).

| Page | Quote (verbatim) | What the code does |
|---|---|---|
| **42** | "Strong High is the High that Caused Manipulation and Structure Breaking, while Strong Low is the Low that Caused Manipulation and Structure Breaking." / "Weak Low/High is the Low that fail to break structure … for every strong LOW there is a weak HIGH" | A swing high is *swept* (a later candle trades above it), then a candle **closes through the opposite swing** ⇒ `strong_high`. The opposite swing is emitted as `weak_low` in the same event. Mirror for lows. |
| **45** | "VERY STRONG HIGH = HIGH GRAB LIQUIDITY (HIGH CAUSED MANIPULATION) BREAK STRONG HIGH + HAVE AN INDUCEMENT." | The same event, **plus** the grabbed level must itself be a major (strong) level **and** an inducement (equal highs within `induce_tol × ATR`) must sit at the extreme ⇒ `very_strong_high`. |
| **67–68** | "AC = ALGO CANDLE — the candle is what develops when it absorbs … and it forms FVG immediately afterwards"; "AC IS WHAT MAKES THE HIGH OR LOW STRONG." | The pivot candle of the swing must be followed within `disp_bars` by a **fair value gap** of at least `min_fvg_atr × ATR` in the direction of the displacement ⇒ `algo_bear` (at a high) / `algo_bull` (at a low). |
| **69** | "VERY STRONG ALGO CANDLE = GRAB LIQUIDITY / BREAK STRONG HIGH OR LOW (MOMENTUM SHIFT) HAVE AN INDUCEMENT / FVG." | The algo candle **plus** a liquidity grab **plus** a momentum shift (a close through the opposite swing) **plus** an inducement ⇒ `very_strong_algo_bear` / `very_strong_algo_bull`. |
| **88, 90, 91** | "HTF REJECTION BLOCK IS A LTF ALGO CANDLE"; "STRONG REJECTION BLOCK = HAVE AN INDUCEMENT + forms on a session (HIGH OR LOW) … NY high/low, London high/low, sometimes Asia" | The algo candle must be the **extreme of its own session** (the run of candles inside that UTC session) and must have an inducement ⇒ `rejection_block_bear` / `rejection_block_bull`. |
| **48** | "WHEN A HIGH OR A LOW DEVELOPS WEAKNESS THE STRONG H/L BECOMES WEAK. MITIGATION WHEN THE PRICE IS MITIGATED" | A zone stays `active` until price comes back into the block (`mitigated`, with time and price) or a candle **closes beyond the extreme** (`invalidated`). |

Two things worth saying plainly:

* The message that arrived with this round attributes "very strong high" to
  **p.42**. The definition is on **p.45**; p.42 defines strong / weak pairs. The
  code follows p.45.
* A candle can be an algo candle **and** a rejection block (the block is the
  algo candle that landed on a session extreme). Both are shown; the tape lists
  both. That is the book, not a duplicate bug.

### What is measured, per zone

* **when** it formed (`time` = the close time of the candle that created it)
* **the exact level** (the high/low that has to be mitigated) and the **block**
  (`top`/`bottom`, the body or the wick of that candle)
* **the evidence** that made it — every zone carries the list of conditions it
  met, so no card is a black box: liquidity grab, structure break, major level,
  inducement (with the count of equal highs/lows), FVG (with its price band),
  momentum shift, session
* **strength** = how many of those conditions were met (1–6)
* **status**: `active` → `mitigated` (time + price) or `invalidated` (time + price)
* swept level and break level, drawn as reference lines on the chart

### Mitigation, and why nothing ever disappears

`mitigation_mode` chooses what "mitigated" means:

* `zone` (default) — price trades back into the block (`top`…`bottom`);
* `level` — price reaches the exact high/low.

Invalidation always wins if a single candle both enters the block and closes
beyond the extreme: the high is broken, so it stopped being supply.

A zone is never deleted because it resolved. It moves to the history list with
its resolution time and price; the tape keeps the record; only a zone older than
`max_zone_age` bars is retired (p.48's "the strong level became weak").

---

## 2. The page

* **Nine tabs** — All · Strong high · Very strong high · Algo candle · high ·
  Rejection block · high · and the four low-side mirrors. Each tab carries the
  live count of *active* zones of that kind.
* **Filters** — symbol search, timeframe chips, "only active", weak pairs,
  sort by newest / closest to price / strongest evidence / symbol.
* **Cards** — symbol, timeframe, pattern, status, level, distance from the live
  price in %, formation time + age, block bounds, session, the evidence list,
  condition count, and the mitigation/invalidation line once it happens.
* **Chart** — clicking a card draws that instrument and timeframe from the
  scanner's own candles: the zone block, the level, the swept and break levels,
  the mitigation/invalidation marker, and the live price.
* **Live tape** — every formation and every resolution as it happens.
* **Symbols drawer** — search the exchange's list, add pairs, add the top 50 or
  every pair, remove pairs. The list is written to `config.json`.
* **Settings drawer** — every threshold in the table below, applied live to
  every instrument (the map is re-read from its own candles, so what you see is
  what the new setting would have found) and written to `config.json`.

## 3. Settings (all of them are in `config.json`)

| key | default | meaning |
|---|---|---|
| `pivot_len` | 3 | bars each side of a minor swing (SHIFT) |
| `major_len` | 12 | bars each side of a major swing (MS) |
| `disp_bars` | 3 | how many candles the displacement may take to print its FVG |
| `min_fvg_atr` | 0.25 | an FVG smaller than this × ATR is noise, not displacement |
| `induce_tol` | 0.25 | equal highs/lows are "equal" within this × ATR |
| `induce_count` | 2 | this many equal levels is an inducement |
| `atr_period` | 14 | Wilder ATR |
| `lookback` | 240 | bars kept per instrument/timeframe |
| `max_zone_age` | 2000 | bars before an unmoved zone is retired |
| `mitigation_mode` | `zone` | `zone` = price enters the block, `level` = the exact high/low |
| `sessions` | Asia 0–8, London 7–16, NY 12–21 (UTC) | session edges for rejection blocks |

## 4. What was verified, and how to reproduce it

```bash
cd smc-radar
../.venv/bin/python -m pytest tests -q                 # 19 passed
../.venv/bin/python tools/scan_now.py --replay data/btc --timeframes 5m 15m 1h
```

The scan above runs on **real Binance 1-minute candles** for BTCUSDT,
1–7 January 2025 (10,080 bars, file `data/btc/BTCUSDT-1m-2025-01.csv`, not
committed — see `docs/data.md`). At the time of writing it reports **32 active
zones** across 5m/15m/1h, e.g. `strong_high BTCUSDT 5m 98,300.64
(97,989.51–98,300.64) formed 07 Jan 15:54`, `very_strong_algo_bull … 02 Jan
00:19`, and `strong_low … 01 Jan 04:59`. The numbers move as the replay
advances; the command is what matters.

Tests (`tests/test_map.py`, `tests/test_engine.py`) pin the definitions:

* sweep of a swing high + break of the opposing swing ⇒ strong high, with the
  swept level and the evidence recorded;
* the weak counterpart is emitted;
* very strong only when a major level was grabbed **and** an inducement exists;
* the low-side mirror of the same path;
* return into the block ⇒ `mitigated` with a price; close beyond the extreme ⇒
  `invalidated`; `level` mode waits for the exact extreme;
* FVG maths, equal-level counting, session bucketing (UTC), Wilder ATR matches
  the slow reference implementation;
* the engine turns real candles into every timeframe, keeps 240 bars, pushes a
  JSON state, and a settings change from the UI lands in `config.json`.

The page was smoke-tested headlessly against the running replay server
(nine tabs, cards with formation time and evidence, chart with candles and zone
block, tab filtering, both drawers, live tape).

## 5. Honest limits

* **The exchange is not reachable from the machine this was built on.** MEXC's
  REST and websocket are blocked in the build sandbox, so the live path was
  verified only up to "the server starts, serves the page, and reports the feed
  as reconnecting" — every chart, zone and number above comes from real recorded
  candles, not from a live feed. `python run.py check` tells you in one line
  whether your machine can reach it.
* **Sessions are UTC** and overlap (London 7–16, NY 12–21). The book does not
  give exchange-local session windows; if your charts use a different session
  clock, change the numbers in Settings.
* **Inducement is a tolerance, not a definition.** The book says "inducement"
  without a distance; `induce_tol × ATR` (0.25 by default) is the honest,
  adjustable approximation, and every card shows the count it found so you can
  judge it.
* **The rejection block is tested at one timeframe at a time.** The book's
  "HTF rejection block is an LTF algo candle" (p.84) needs a multi-timeframe
  link; here the same candle appears on each timeframe separately, and it is
  your call which one is the HTF. The tags make that visible, but the app does
  not yet draw the HTF↔LTF pair as one object.
* **This is a map, not a strategy.** It does not say buy or sell, has no stop or
  target, and no win rate is claimed. Nothing here has been backtested for edge —
  adding a number to a structure would be exactly the invented rule we agreed
  not to add.
