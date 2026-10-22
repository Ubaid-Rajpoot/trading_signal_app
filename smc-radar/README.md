# SMC radar — where the strong highs, algo candles and rejection blocks are

A second standalone app next to `tdigm-live`. It **does not trade** and it does
not invent an entry model: it maps the four structures from the trader's own
order-block book, on every instrument it is given, on every timeframe, and it
keeps each one on screen — with the time it formed — **until price mitigates it**.

```bash
cd smc-radar
python run.py serve                       # live: MEXC, every symbol you add
python run.py replay data/btc 40          # replay recorded candles: 2400 loaded first,
                                          # and each candle shown forming (--ticks 4)
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
* **Chart** — a real chart, opened automatically on the newest active zone.
  It holds **1000 candles** (deep history, not just the detection window), draws
  the zone block, the exact level, the swept and break levels, the
  mitigation/invalidation marker and the live price, and it grows the candle
  that is still forming in front of you. **Scroll** to zoom, **drag** to pan back
  through history, **drag the right edge** to rescale price, **double-click**
  (or `reset`) to follow the newest candle again. Touch: one finger pans, two
  fingers pinch. A crosshair shows OHLC for the candle under the cursor.
* **Live tape** — every formation and every resolution as it happens.
* **Symbols drawer** — search the exchange's list, add pairs, add the top 50 or
  every pair, remove pairs. The list is written to `config.json`.
* **Settings drawer** — every threshold in the table below, applied live to
  every instrument (the map is re-read from its own candles, so what you see is
  what the new setting would have found) and written to `config.json`.

## 2b. Live, continuous data — how it keeps coming

The first version needed a restart to show anything new. That is fixed in five
places, each one measurable:

| what | where | what it does |
|---|---|---|
| **push + heartbeat** | `bot/server.py` | every candle and tick is pushed; a 5-second heartbeat pushes the state even when nothing moved, so a page never goes stale |
| **three transports** | `web/index.html` | websocket first, **server-sent events** (`/api/stream`) if a proxy eats websockets, **polling** every 3s if even that fails. A watchdog notices 6s of silence, and rebuilds both pushes after 15s |
| **stall reconnect** | `heartbeat` in `bot/server.py` + `BaseFeed.force_reconnect` | if the exchange socket stays open but silent for 120s, the feed is rebuilt automatically |
| **forming candle** | `bot/engine.py` (`_apply_tick`, `engine.bars`) | the exchange's unfinished 1m candle updates the charts on every tick; the detection map waits for the close, because a wick that is not final cannot confirm a pattern |
| **incremental refresh** | `/api/candles?since=` | an open chart asks only for candles it has not seen, so it can follow the market all day |

`python tools/smoke.sh` proves all of this against a running server: the state
advances on its own, the candle endpoint serves a deep history, the incremental
refresh returns only new bars, and both SSE and the websocket push.

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
../.venv/bin/python -m pytest tests -q                 # 26 passed
../.venv/bin/python tools/scan_now.py --replay data/btc --timeframes 5m 15m 1h
PORT=8891 bash tools/smoke.sh                          # end-to-end HTTP/WS/SSE checks
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

The page was smoke-tested headlessly against the running replay server, three
times over, once per transport:

* **websocket** — nine tabs, cards with formation time and evidence, canvas
  chart, wheel zoom in **and** out, price-axis drag, drag-to-pan back through
  history, crosshair, reset-to-follow, self-refreshing chart, resolved-zone
  toggle;
* **server-sent events** (websocket blocked) — the map keeps advancing;
* **polling** (websocket and EventSource both unavailable) — the map keeps
  advancing.

The chart's own drawing was checked by counting what the canvas actually
received on the last frame (candles, zone block, level lines), and the growing
candle path by a test that feeds an unfinished 1m bar and reads it back through
`engine.bars()` and `snapshot()["price"]`.

## 5. Honest limits

* **The exchange is not reachable from the machine this was built on.** MEXC's
  REST and websocket are blocked in the build sandbox, so the *connectivity*
  path was verified only up to "the server starts, serves the page, and reports
  the feed as reconnecting". Everything else — deep history, the forming candle,
  the push, the three transports, the reconnect logic — is covered by the tests
  and the smoke script above, and every number on the page comes from real
  recorded candles rather than from a live feed. `python run.py check` tells you
  in one line whether your machine can reach MEXC. If your MEXC stream shows
  "live" and the candle count keeps climbing, the live path is working; if the
  badge says reconnecting, the network is the problem, not the page.
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
