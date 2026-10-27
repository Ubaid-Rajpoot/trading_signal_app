# SMC radar — where the strong highs, algo candles and rejection blocks are

A second standalone app next to `tdigm-live`. It **does not trade** and it does
not invent an entry model: it maps the four structures from the trader's own
order-block book, on every instrument it is given, on every timeframe, and it
keeps each one on screen — with the time it formed — **until price mitigates it**.

**Futures and spot are separate markets** with their own lists, their own
candles and their own section on the page, and **every zone carries a trade
plan** — limit entry at the block edge, stop just beyond the exact high/low,
target at the next liquidity pool — with its live state and its R:R. What that
plan was measured to do on 90 days of real 1m BTC is in
[`docs/plan-btcusdt-1m.md`](docs/plan-btcusdt-1m.md).

```bash
cd smc-radar
python run.py serve                       # live: MEXC, every symbol you add
python run.py replay data 40              # replay BOTH markets (spot + futures files):
                                          # 4800 candles loaded first, then streamed
python run.py check                       # can this machine reach MEXC (spot AND futures)?
python run.py universe --top 20                    # spot pairs, biggest turnover first
python run.py universe --top 20 --market futures   # contracts, biggest turnover first
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
* **Filters** — symbol search, timeframe chips, **view: active only / active +
  recently mitigated (default) / everything**, a window selector (15 min … 24 h),
  **market: futures + spot (default) / futures only / spot only**, **quality:
  trade-ready (default) / A+ and A (≥ 70 %) / A+ only (≥ 85 %) / sniper
  (R:R ≥ 1.5) / tradeable now / on a strong level / everything**, weak
  pairs, sort by **measured chance (default)** / newest / closest to price /
  strongest evidence / symbol. The default order is the measured probability of
  reaching the target, best first, so the first card on the page is the best
  setup the scan has found — wherever it is in the list of instruments.
  Active zones always lead the list; a resolved zone keeps its card, dimmed,
  with the time it formed **and** the time and price that mitigated it. That is
  what keeps a 1m pattern on screen: a 1m zone is usually mitigated within
  minutes, so an "active only" list can be empty for it by the time you look.
* **Cards** — symbol, timeframe, pattern, **grade (A+ … C)**, status, level,
  distance from the live price in %, formation time + age, block bounds, session,
  **the live market regime (trend ↑ / trend ↓ / range) with a `TRADEABLE NOW`
  badge** when the trend is on the zone's side, **block and displacement size in
  ATR**, the evidence list, **same-level and higher-timeframe confluence**, the
  **order-block candle** the rectangle is drawn on, condition count, and the
  mitigation/invalidation line once it happens.
* **Chart** — a real chart, opened automatically on the newest active zone.
  It holds **1000 candles** (deep history, not just the detection window), draws
  **every zone of that instrument and timeframe** behind the selected one (the
  `zones` button turns them off; the nearest 24 are drawn so the chart stays
  readable), outlines **the order-block candle itself** in amber (`OB`, with an
  `◀ OB` / `OB ▶` arrow when it is outside the visible window), draws the zone block, the exact level, the swept and break levels, the
  mitigation/invalidation marker and the live price, and it grows the candle
  that is still forming in front of you. **Scroll** to zoom, **drag** to pan back
  through history, **drag the right edge** to rescale price, **double-click**
  (or `reset`) to follow the newest candle again. Dragging moves the chart **in
  both directions** — grab the candles and pull them up or down to see where the
  order block really sits — and `⛶` (or `F`) puts the chart **full screen**
  (`Esc` leaves). The keyboard also has `+` / `-` to zoom, arrows to pan and `R`
  to reset. Touch: one finger pans, two fingers pinch. A crosshair shows OHLC for
  the candle under the cursor and the time of that candle on the time axis.
  The last candle keeps **the same right-hand margin TradingView uses** (14% of
  the view, at least four candles) instead of being glued to the price axis, and
  you can drag or scroll further into that empty space; `follow` snaps back.
  In live mode the chart draws a **countdown to the candle close** (`⏱ 02:41`)
  that runs every second and turns amber in the last ten seconds, next to a
  `● live` marker while the candle is still forming. A replay has no wall clock
  behind it, so it says `▶ replay` rather than faking a timer.
* **Live tape** — every formation and every resolution as it happens.
* **Symbols drawer** — search the exchange's list, add pairs, add the top 50 or
  every pair, remove pairs. The list is written to `config.json`.
* **Settings drawer** — every threshold in the table below, applied live to
  every instrument (the map is re-read from its own candles, so what you see is
  what the new setting would have found) and written to `config.json`.

## 2b. Live, continuous data — how it keeps coming

### If `candles_seen` / `ticks_seen` stay at 0

That was a real report: `connected: true`, `feed: "live - 12 symbols on 1
socket(s)"`, and yet nothing arriving. Three things changed so it cannot happen
again, and so the reason is never a mystery:

1. **The REST poller is now the safety net** (`bot/poller.py`, on by default in
   live mode). Every 6 seconds it asks the exchange's own kline endpoint for the
   last 3 one-minute candles of every symbol. The newest, still-open candle is
   pushed as a **tick**; the first time a candle is seen **closed** it is pushed
   as a closed candle (so 5m/15m/1h are still built from real 1m candles). When
   the websocket is healthy the poller steps back to a 30-second top-up; when
   the websocket is silent for 45 seconds it becomes the primary feed.
   Duplicates are impossible: the engine ignores any candle it already has.
2. **The websocket host is verified, not assumed.** MEXC has moved hosts; both
   `wbs.mexc.com` and `wbs-api.mexc.com` are tried, and a socket that connects
   but sends no kline within 20 seconds is abandoned (`bot/feed.py`). So
   "connected" now means "a candle actually came through".
3. **`/api/health` says why.** New fields:

```json
"stream":    {"messages": 41, "parsed": 41, "ignored": 0, "subscribed": 12,
              "seconds_since_message": 0.4, "samples": ["…raw frame…"]},
"rest_poll": {"enabled": true, "primary": false, "requests": 120, "errors": 0,
              "closed_pushed": 118, "ticks_pushed": 0, "age": 1.2},
"warming":   {"active": false, "done": 60, "total": 60, "errors": 0},
"delivering": true
```

Read it like this: `stream.messages = 0` after a minute → the socket is giving
you nothing (the poller is then carrying the app, `rest_poll.primary = true`);
`messages > 0` but `parsed = 0` → the exchange changed its message format and
`stream.samples` contains the raw frame to send me; `rest_poll.errors > 0` →
REST is failing too, so it is a network problem; `delivering: false` with both
counters at 0 → nothing is reaching the machine.

### Starting up

Start-up used to warm every instrument × timeframe over REST *before* serving
anything, which is why the browser waited. Now the page and `/api/health` answer
in **~0.5 s** (measured: `tools/smoke.sh` prints the number) and the warm-up runs
in the background, four instruments at a time, reporting itself as
`warming.done / warming.total` on the page header ("warming 23/60").

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

### What changed, and what did not

* **Detection did not change.** The rules that decide *whether* a strong high /
  very strong high / algo candle / rejection block exists are the ones from the
  book, and `bot/map.py` still contains exactly one implementation of each. In
  round 4 `map.py` was byte-identical to the first `smc-radar` commit and the
  same recorded BTC replay produced the same 1m zones at the same rate.
* **Round 5 changes what is drawn and how it is ranked, not what is found.**
  Three additions, all additive and all visible on every card:
  1. **the rectangle is the order block** — it starts at the exact high/low the
     pattern is about and extends to the far side of the last opposite-colour
     candle with the biggest body (`ob` on every zone). Before, the box was the
     body of the pivot candle, which is why a rectangle could look tiny and still
     say "active" after price had already been through the area.
  2. **a market-regime gauge** (Kaufman efficiency ratio + Wilder ADX) per
     instrument and timeframe, exposed live as `now_trend` and per zone as
     `trend` (the regime when it formed) and `entry_trend` (the regime at the
     touch).
  3. **a grade** (A+ … C, score 0–100) built from measured features and
     **calibrated on 90 days of real 1m BTC** — see
     [`docs/quality-btcusdt-1m.md`](docs/quality-btcusdt-1m.md). Reproduce it
     with `python tools/zone_quality.py --csv study-data/BTCUSDT-1m-90d.csv
     --timeframe 1m --horizon 240`.

The default quality filter shows **A+ and A** (83% of those zones travel 2×ATR
in your favour before the level is closed through, against 54% for C). Nothing
is deleted: choose *everything, incl. thin / range* in the quality filter and
every zone comes back, with the reason it was hidden shown on its card.

### Round 6 — two markets and a trade plan

* **Futures is a first-class market, not a spot alias.** `bot/feed.py` has
  `MexcFuturesFeed` (contract REST + contract websocket) and `CompositeFeed`,
  which routes each symbol to its own market's feed and merges the queue;
  `market_of()` tags a symbol as `futures` whenever it is written the contract
  way (`BTC_USDT`). The engine stamps `market` on every zone and reports
  `market_counts`; the API serves `/api/zones?market=`, `/api/universe?market=`
  and persists **two** lists (`symbols`, `futures_symbols`). The page shows two
  sections, **futures first**, each with its own count, its own KPI card, and a
  `FUT` / `SPOT` badge on every card. The market filter shows one, the other, or
  both.
* **Every zone carries a trade plan** (entry / stop / target / R:R) and its live
  state: `waiting for the entry` → `filled` → `target hit` / `stopped out`. The
  entry is the block edge, the stop is `0.10 × ATR` beyond the exact high/low,
  the target is `next_liquidity()` — the next strong level, swing or session
  extreme in front of the trade, i.e. the user's own exit rule. A zone with no
  pool in front says *"no pool in front"* rather than inventing a level.
* **Two new quality filters** — `sniper · R:R ≥ 1.5` and
  `sniper · R:R ≥ 1.5 + algo candle`. They exist because the study found them to
  be the only subsets with a clearly positive expectancy on the 90-day sample
  (+0.80 R and +0.96 R per trade), while every plan together is −0.11 R.
* **The replay plays both markets at once.** `ReplayFeed` merges the CSV files
  on time (one cursor per file, oldest candle first) instead of playing one file
  after the other — otherwise one section would freeze while the other
  streamed, which is not what two feeds look like. `chart.mjs` caught exactly
  that: its "the newest candle is the one still forming" check failed while the
  spot file was finished and the futures file had not started.
* **Nothing was deleted.** Detection, grading and the round-5 drawing are
  unchanged; the default quality filter is still A+ and A; old modes are one
  click away.

### Round 13 — the contract list, the disappearing trade, and the range gate

Five reported failures and one accusation, all fixed and measured:
[`docs/round-13-range-gate-and-storage.md`](docs/round-13-range-gate-and-storage.md).

* **`DecodingError: brotli ... can_accept_more_data() is False`** — the exchange
  served plain JSON labelled `content-encoding: br`; a decoder with nothing left to
  accept raises exactly that. The transport now reads the body **raw** and decides
  itself (JSON by its first byte, gzip/deflate by magic, brotli if the package is
  installed), so no third-party decoder can get stuck. The contract list is retried
  on a backoff and the last good one is kept, with its age.
* **`warm-up TON_USDT 1h: no kline`** — one timeframe without history no longer
  empties the instrument: three attempts, then a **roll-up from 1m** (which always
  exists), and only a symbol with no 1-minute history is reported (once, in
  Diagnose).
* **Noisy toasts** — "re-requesting history" speaks only when the set of thin
  instruments changes; timeouts are 45 s with a retry; the replay's "map restarted"
  note now explains itself.
* **A trade vanished at the instant it opened.** A zone stopped being "active" the
  moment price entered the block — which is when its plan fills. Liveness is now the
  **plan**: a filled trade stays live until its target or stop, in the frame, in the
  journal (`OPEN_SQL`) and in the "open now" count. Measured before the fix: 208
  filled plans were missing from the live list.
* **…and it survived a restart.** Open plans are **adopted** from SQLite at
  start-up (`restore_hours: 12`, `restore_max: 150`), after the history loads, so the
  arriving candles keep deciding them. A real restart adopted 150.
* **Signals in a ranging market.** The book's own structure test is now code
  (`MarketMap.structure` + `trade_gate`): HH+HL / LH+LL from confirmed swings, equal
  highs/lows = liquidity not structure, a 79 % retracement = no structure, the
  timeframe above must agree, and a sell belongs in premium / a buy in discount.
  Every card prints the verdict or the reason it failed.
* **The numbers, honestly** (90 held-out days, the plan as traded):
  at p≥0.85 the gate moves the hit rate **95.4 % → 97.7 %** while removing **97 %**
  of the signals and 0.02 R of expectancy (1.7 % of every plan formed passes:
  344 of 20 717 on 90 held-out days). It is the book's rule and the user's rule, so
  it ships on — and `"structure_gate": false` turns it off.
* **Why the account emptied:** R:R 0.31 means **one stop costs 3.2 wins**; at a
  "96 %" sounding size, one stop is the account. The card now says so in amber, and
  every percentage carries its sample size (`97 % · n=151`).
* The model was re-fitted on the plan the app **actually trades** (`--stop-mode
  block`, stop beyond the block) — the old 97 % described a `level` stop nobody was
  taking. New bands, holdout, gate on: A+ 100 % (n=16) · A 98 % (n=44) · B 95 %
  (n=95) · C 87 % (n=186, −0.01 R → never a signal).

### Round 12 — the signal that "disappeared", and why the app got slow

Two reports, one cause each.

* **"A signal vanished although it was neither filled nor invalidated."** It was not the
  pattern engine and not the database: the pushed frame carried **the top 200 active
  zones by probability**, so with more than 200 live zones the ones below the cut were
  never sent — the page could not draw what it did not receive. Signals now **lead the
  frame**, the cap can only ever drop a non-signal, and every frame says
  `active_total / active_shown / signal_total / signals_shown`. The list header prints
  *"the frame carries 200 of 960 live zones"* with a **load every live zone** button that
  pulls the complete list in one request. The database never lost anything: a signal is
  written when it forms, whatever the frame does — and `tools/ui/perf.mjs` checks every
  live signal is readable from `/api/journal/{key}` with the same entry and stop.
* **"The whole software became very slow."** Bytes, then rendering. A frame carried the
  prose with it (~1.3 KB per zone) and went out 2–3 times a second: ~419 KB per frame. It
  now carries only what a card prints (~0.57 KB), the cadence is paced by scan width
  (0.35 s → 0.7 s at 60 instruments, 1.2 s up to 200, 2 s beyond), and the sentences are a
  single request for the zone you actually open (`GET /api/zones/{key}`): **419 KB → 130 KB
  per 200-zone frame, ≈0.1 MB/s instead of ≈1 MB/s**. On the page, every pane is drawn
  only when its own signature changes and the list draws 60 cards at a time — an idle
  frame now costs **2.0 ms and keeps the same DOM nodes**, where before it rebuilt 200
  cards every 0.35 s.

Detail, tables and the reproduce commands:
[`docs/round-12-vanishing-and-speed.md`](docs/round-12-vanishing-and-speed.md).

### Round 11 — signals are a grid, and the live ones get their own tab

*"Signals are all in one line — if there are 200, do I slide along one by one? Can't there
be a grid view? And a separate side tab for the live signals, so I can see what is
happening in each."*

* **The side panel is tabbed**: **Live now** (one row per live signal - entry, target,
  stop, how far each is from the live price, expected R after fees, R:R, and whether it
  is WAITING or FILLED), **Archive** (a paged grid of every stored signal with
  server-side filters and a grid⇄list switch), and **Tape**.
* **A real grid, not a sliding line**: tiles of `minmax(218px, 1fr)` - as many across as
  the panel allows - each printing symbol, direction, timeframe, measured chance, state
  badge, result in R, grade, entry, target, stop and time. `GET /api/journal` gained
  `offset` and returns `total`, so the grid says `1–12 of 213 · page 1 of 18` and one
  page is one indexed query. The top strip is a 12-tile grid too, with
  `all N signals →` jumping into the archive tab; the **Journal** drawer is the
  full-window version (1,000 px, same grid, own filters and pager).
* **"what the badges mean"** sits inside the archive tab: LIVE · TARGET HIT · STOPPED ·
  MITIGATED · INVALIDATED · INTERRUPTED / NOT WATCHED, each in one sentence - including
  the two that matter when a signal seems to vanish.
* **A real bug fell out of it**: only live zones were enriched, so a *resolved* zone
  reached the page (and its archive tile) with no grade and no market. Everything that is
  sent is enriched now; a test pins it.

Detail: [`docs/round-11-signal-grid.md`](docs/round-11-signal-grid.md).

### Round 10 — nothing vanishes, nothing spills, and the exchange cannot break a button

Three things came back, and all three were fair:

* **"Signals still vanish."** The round-9 archive stored them; four other paths still hid
  or mis-stated them. The strip at the top showed *only* the live map, so a plan that hit
  its target left the page while sitting safely in SQLite — the strip now merges the live
  map with the database and keeps the finished ones on screen as **TARGET HIT / STOPPED /
  MITIGATED**. `sync_journal()` no longer freezes a row whose probability fell below the
  floor; removing an instrument marks its live rows **untracked** instead of leaving them
  "active" for ever; a restart marks the plans it can no longer follow **interrupted**
  (kept, with a note); and a decided outcome is now **final**, so replaying the same
  candles cannot rewrite "target +1.6 R" into "stopped −1 R". Measured across two
  processes on one database: 15 signals / 6 finished / 5 targets / **+0.245 R** in both
  runs — the scoreboard is idempotent, and a failed poll can no longer empty the page.
* **"Adding a coin throws `DecodingError: brotli: …`."** Real, and the scanner's fault:
  every REST call used a default HTTP client that advertises `br`, and a body handed to a
  reused brotli decoder throws `can_accept_more_data() is False` half-way. Requests now
  advertise **`gzip, deflate` only**, retry once with `identity`, retry `429`/`5xx` but
  never a `4xx`, and turn an unreadable body into an error that names the URL. The API
  reports a broken exchange list as data (`ok: false`) instead of a 500, and the page
  translates decoder/network/timeout text into a sentence. Toasts stack now instead of
  overwriting one another.
* **UI/UX.** The page uses the screen: `max-width` 2400 px, an `auto-fit` KPI band, and a
  **third column from 1780 px** (cards · chart + tape · **signal archive**) so a wide
  monitor is not two empty gutters. The card list is no longer a scroll box inside a
  scrolling page (`max-height: 1120px; overflow-y: auto` clipped tall cards), the
  right-hand chips wrap as one group, entry/stop/target use three shrinkable columns, and
  every value is `overflow-wrap` inside a clipped card — content cannot leave the box any
  more. `Symbols → Diagnose` prints bars per timeframe, zones and the warm-up error for
  every instrument, and a script error now shows up as a toast instead of a page that
  quietly stops updating.

Full detail, the measured tables and the reproduce commands:
[`docs/round-10-ui-and-integrity.md`](docs/round-10-ui-and-integrity.md).

### Round 9 — futures only, and a signal you cannot lose

Three demands: *remove the spot side completely*, *futures must produce a lot of
signals*, and *a signal that has been shown must be stored with its details so it can
never be lost*. The first two turned out to share one root cause, and the third turned
up two bugs of its own.

* **Spot is gone.** `config.json` is `markets: ["futures"]` with 40 contracts and
  `auto_futures: 60` (the top contracts by turnover are added when a live scan
  starts); `make_market_feed()` returns a bare `MexcFuturesFeed`; `run.py` has no spot
  path; the page has one market section, and the spot KPI became **Archived signals**.
  `data/btc/` is deleted, `data/fut/` is the only candle folder.
* **"Futures gives no signals" was two bugs.** (1) The contract kline window was
  computed with 60-second candles whatever the timeframe, so `Min15` got ~66 bars
  instead of ~1000, never reached the 240-bar lookback, and produced nothing. (2)
  `warmup()` gave up on **every remaining instrument** after eight consecutive
  failures — and 60 contracts is 300 rate-limited REST calls, so the first burst
  switched the scan off and it then waited for the live stream to fill 15m history
  (two and a half days). Now one failure costs one instrument: the pass backs off and
  a `healer()` re-requests whatever is still thin. `GET /api/diagnostics` prints
  bars / zones / ATR / lookback / warm-up error **per contract and timeframe**, so an
  empty contract has a visible reason.
* **The stop moved to the rule.** The shipped stop was 0.10 ATR beyond the exact
  high/low — inside one candle's noise. The block stop (beyond the whole order block,
  which is what "stop beyond the red box" says) on the same 36-day replay, same model,
  same fee gate: **4.1 → 14.8 signals per day**, hit rate **41.7 % → 64.7 %**, average
  **−0.25 R → −0.08 R** (before fees). Round 8's *study* had found the block stop
  worse; the app's own bookkeeping disagrees, so the app now follows the measurement
  it can reproduce, `plan_stop_mode: level` is still in the settings drawer, and the
  conflict is written down rather than hidden. **Neither stop setting is profitable
  yet** — the block plan pays only ~0.55 R per 1 R risked, so 64.7 % is break-even
  before fees — and the page no longer prints the study's 97 % as if it were live.
* **Every signal is in SQLite.** `bot/journal.py` (stdlib, WAL) writes a row the moment
  a zone reaches `journal_prob_min` (0.60) or becomes a signal, with symbol, market,
  timeframe, kind, side, grade, probability, the measured band, conditions, level,
  **entry / stop / target**, R:R, risk %, fee in R, net R, trade-ready, status, plan
  state and timestamps — plus the entire zone as JSON. The row is updated through its
  life and **never deleted**; `result` / `result_r` / `closed_at` are write-once,
  because a plain update from the live zone was erasing them (the actual "my signal
  lost its details" bug). Two hit rates are reported — the signals only, and every
  archived row — and the drawer prints the signal scoreboard first.
* **The page shows it.** A **Journal** drawer with symbol / grade / status / window
  filters, a CSV export (`/api/journal.csv`) and the archive KPI in the top strip;
  `/api/journal`, `/api/journal/stats`, `/api/journal/{key}`, `/api/diagnostics`.
  A replay of the recorded futures candles stores ~2500 graded setups and 150+ signals
  in 30 days on a single contract, and every one of them is still readable after it is
  mitigated, invalidated or stopped.

Full tables, the exact replay command and what could not be verified offline:
[`docs/round-9-futures-and-journal.md`](docs/round-9-futures-and-journal.md).

### Round 8 — the exit was the bug: many signals, 97 % measured, and a reward that pays

The complaint: *"futures still has too few signals; I want a system with a lot of
signals on 1m and 5m, and those signals should be above 90 % accuracy."* The
measurement said the patterns were not the problem — the **exit** was.

* **The exit was a few ticks away.** "Exit at the next liquidity level" was coded
  as *the nearest* pool, and on 1m structure that is often a 3-bar swing inside the
  block. The plan risked 0.25 % to make 0.03 %, so the 0.08 % round-trip fee cost
  more than the reward: **−0.01 R per trade** on the held-out block, and no stop
  width escaped it. The exit rule now skips pools closer than **1 ATR** — a tick
  inside the block is not a liquidity level — and takes the next real one. Outcome
  on the unseen 90 days: median reward **0.75 R** instead of 0.15 R, and the
  selected signals net **+0.54 R** each instead of −0.01 R.
* **The model now sees the plan.** Round 7's features were the book's conditions
  only, so it could not tell a setup with room to run from one already at its
  target. Round 8 adds the plan's own geometry (`log(risk_pct)`, `block_atr`) and
  the timeframe. AUC 0.789 on the held-out block, calibration 0.80–0.90 predicted
  → 87 % actual.
* **The letters are accuracy bands now.** A+ = measured ≥ 90 % (97 % actual),
  A = ≥ 85 % (97 %), B = ≥ 75 % (95 %), C = ≥ 60 % (90 %); below that the grade is
  an honest `–` and it is not a signal. The page opens on **A+ and A**: at the
  85 % bar, 341 out-of-sample signals in 90 days on BTC alone — **1.4 a day on 1m,
  1.5 a day on 5m**, ~70 a day across the 24+24 instruments in `config.json`, and
  about 90 across all four timeframes. Higher timeframes stay small, as asked.
* **The page leads with them.** A new **signal strip** at the top shows the live
  signals with entry / stop / target, the measured hit rate of their band and the
  R each is expected to make after fees; the hero KPIs print *live now*,
  *measured hit rate* and *signals per day*; the default filter is
  `signals · measured ≥ 85 %`; `GET /api/signals?signal=1` and `GET /api/model`
  serve the same thing to anything else you want to build on top.
* **Nothing was deleted and no default was swapped silently.** `plan_target_atr_min:
  0` restores round 7's nearest-pool exit exactly, `plan_stop_mode: level|block`
  exposes the stop choice that was measured (the block stop is *worse*: it halves
  the reward and only adds 2 points of accuracy), and both live in the settings
  drawer next to the signal bar, saved into `config.json`.

Full tables, intervals and the reproduce command: [`docs/signals.md`](docs/signals.md).

### Round 7 — the letter is measured, the fee is part of the plan, and the live feed works

Three complaints came back from the user, and all three were real:

* **"Adding a symbol gives an error."** `CompositeFeed` — the object that fronts
  spot *and* futures in live mode — carried the *replay* implementation over it:
  its `candles()` iterated `self.files` (which it does not have) and it had **no
  `universe()` at all**, so in live mode it streamed nothing and adding from the
  exchange list failed. Both are rewritten (one pump task per market, merged
  queue; a universe that tags every item with its market and keeps the working
  half when one market is down), and `tests/test_feed.py` pins them. `python
  run.py check` now tests **both** markets, and `/api/universe` reports per-market
  errors instead of one opaque failure. The page side was hardened too: adding a
  symbol, "add top 50" and "add every instrument" now print the actual answer
  (HTTP status, the exchange's complaint, a rate limit) instead of a button that
  looks dead when the request fails.
* **"A+ is printed on setups that meet few conditions."** Correct, and the fix is
  not to count conditions. Measured: the count does not rank outcomes (5 cond.
  62 %, 8 cond. 59 %, same on a held-out block). So the grade is now
  **P(the plan reaches its target before its stop)**, from a model fitted on 90
  days and validated on the next 90 (AUC 0.735; A+ band 87–95 % actual, C band
  28–46 %). The card lists **all ten of the book's conditions**, met and missed,
  so nothing is hidden. Full report: [`docs/conditions.md`](docs/conditions.md).
* **"For futures no signal comes at all, though MEXC has 1600 coins."** The feed
  bug above was the main cause; on top of it the scan is now sized for 1600
  instruments: `max_symbols` 2000, parallel warm-up workers (`warm_concurrency`),
  a bounded zone store (`max_zones`), a capped and **ranked** push
  (`max_active_zones`), a lean frame above 120 instruments, and a new ranked
  endpoint `GET /api/signals?market=&timeframe=&grade=&tradable=&limit=` that
  returns the best setups across every instrument instead of the newest 200.

**The finding that explains "my stops get hit":** the fee is charged on the whole
position while the risk is only the stop distance, so cost in R is
`round-trip fee ÷ stop distance`. The median stop on 1m BTC is **0.11 % of price**
and a MEXC futures round trip is **0.08 %** — the fee was **0.7–1.8 × the entire
risk**. Every zone now carries `plan.risk_pct`, `fee_r`, `net_r` and a
`tradable` flag, the card prints them, and the default filter is
**trade-ready**. On the measured data that takes 1m from −1.29 R to break-even and
makes 15m/30m genuinely positive (+0.23 / +0.58 R).

The page's quality filter is now: **trade-ready (default) · A+ and A (≥ 70 %) ·
A+ only (≥ 85 %) · sniper (R:R ≥ 1.5) · tradeable now (in trend) · on a strong
level · everything**, and the list is sorted by measured chance unless you pick
another order. Nothing was removed: every older filter, tab and toggle is still
there.

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
| `ob_lookback` | 8 | how many candles are searched for the order-block candle |
| `ob_min_height_atr` | 0.12 | a rectangle is never thinner than this × ATR |
| `trend_lookback` | 40 | bars behind the efficiency ratio |
| `trend_er_min` | 0.25 | efficiency ratio at/above which the market counts as trending |
| `trend_adx_min` | 18 | and the ADX(14) must agree |
| `confluence_tol_atr` | 0.5 | "the same level" is within this × ATR |
| `confluence_age` | 400 | bars a strong level stays relevant for confluence |
| `plan_stop_atr` | 0.10 | the stop sits this × ATR beyond the exact high/low |
| `plan_pool_bars` | 120 | how far back the session extreme used as a target may be |
| `plan_pool_age` | 600 | bars a strong level stays a valid target |
| `cond_disp_atr` | 2.0 | "aggressive displacement" = this many ATR (a condition) |
| `plan_stop_mode` | **block** | round 9: stop beyond the whole order block (the rule) instead of 0.10 ATR beyond the extreme — measured 3.6× the signals and +23 points of hit rate on the replay |
| `plan_target_atr_min` | **1.0** | round 8: the exit pool must be at least this × ATR away (`0` = round 7's nearest pool) |
| `signal_prob_min` | 0.85 | what the page calls a signal: measured at this bar *and* the fee clears |
| `prob_cuts` | A+ 0.90, A 0.85, B 0.75, C 0.60 | measured probability → letter (each letter's own record is in `docs/signals.md`) |
| `fee_maker_bps` / `fee_taker_bps` | 2 / 6 | MEXC futures fees used to cost the plan |
| `fee_max_r` | 0.30 | a plan costing more than this share of the risk is not tradable |

Live-only keys (top level of `config.json`):

| key | default | meaning |
|---|---|---|
| `rest_poll_seconds` | 6 | how often the REST poller refreshes candles when the socket is silent |
| `rest_poll_quiet_after` | 45 | seconds of socket silence before the poller becomes the primary feed |
| `rest_poll_always` | false | force the poller to poll ticks even when the socket is healthy |

## 4. What was verified, and how to reproduce it

```bash
cd smc-radar
../.venv/bin/python -m pytest tests -q                 # 65 passed
../.venv/bin/python tools/scan_now.py --replay data/btc --timeframes 5m 15m 1h
../.venv/bin/python tools/zone_quality.py --csv study-data/BTCUSDT-1m-90d.csv --timeframe 1m --horizon 240
../.venv/bin/python tools/plan_study.py --csv study-data/BTCUSDT-1m-90d.csv --timeframe 1m --horizon 240
../.venv/bin/python tools/condition_study.py --csv study-data/BTCUSDT-1m-90d.csv --timeframe 1m
../.venv/bin/python tools/fit_model.py --train study-data/BTCUSDT-1m-90d.csv \
    --holdout study-data/BTCUSDT-1m-90d-holdout.csv --target-atr-min 1.0   # docs/signals.md
PORT=8891 bash tools/smoke.sh                          # HTTP/WS/SSE + start-up time + health
cd tools/ui && npm i && node chart.mjs && node transports.mjs && node algo1m.mjs && node perf.mjs && node quality.mjs && node layout.mjs
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

New in round 5, all covered by `tests/` and `tools/`:

* the rectangle **starts at the exact high/low** and reaches the far side of the
  **biggest opposite-colour candle** (green at a high, red at a low) —
  `test_high_block_starts_at_the_exact_high_and_marks_the_order_block_candle`,
  `test_low_block_mirrors_it_with_a_red_candle`;
* a block can never collapse to a line (`test_block_never_collapses_to_a_line`;
  found by `tools/ui/quality.mjs`, which caught a zero-height box on real data);
* the regime gauge separates a trend from a range
  (`test_regime_separates_a_trend_from_a_range`);
* every zone carries a grade, and a thicker block scores higher
  (`test_every_zone_carries_a_grade_and_its_reason`,
  `test_a_thin_block_scores_below_a_thick_one`);
* the API and the push carry the grade, the live regime, the order-block candle
  and the confluence, and **no zone is ever "active" while carrying a mitigation
  time** (`test_every_pushed_zone_carries_its_grade_and_live_regime`,
  `test_the_zone_api_serves_the_same_fields_and_never_hides_mitigation`);
* `tools/ui/quality.mjs` checks the page end to end: the grade chips, the live
  regime, the quality filter narrowing and widening, the rectangles appearing on
  the chart, the `OB` marker, **vertical drag**, keyboard panning and the
  full-screen button.
* **Round 6**, also pinned by tests: every real zone carries a plan whose entry
  is the block edge, whose stop is beyond the level and whose R:R is
  reward/risk on its own numbers (`test_every_real_zone_carries_a_tradeable_plan`);
  the plan fills on the retest and ends at its target
  (`test_a_plan_fills_at_the_block_edge_and_ends_at_its_target`); a close
  through the level stops it (`test_a_close_through_the_level_stops_the_plan`);
  a flat weak marker never gets a plan; futures and spot are separate books
  end to end (`test_futures_and_spot_are_separate_markets_end_to_end`,
  `test_zones_and_universe_can_be_filtered_by_market`); and
  `tools/ui/quality.mjs` asserts the two sections, the FUT/SPOT badges, the plan
  block, the sniper filters, the market filter and the two KPI cards on the
  running page.

Older coverage, unchanged:

* a **dead socket** (`connected: true`, nothing ever delivered) still produces
  closed candles **and** ticks, without duplicating candles in the chart
  (`test_server_feeds_itself_from_rest_when_the_socket_never_delivers`);
* a **healthy socket** keeps the poller quiet (no unnecessary tick polling);
* warm-up reports progress and never blocks the server;
* the chart's right margin, the countdown (formula **and** the ticking clock),
  the per-timeframe refresh rate and the live tick counter are asserted in
  `tools/ui/chart.mjs`.

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

* **The live feed is verified with fakes, not against MEXC.** MEXC's REST and
  websocket are blocked in the build sandbox, so the two-market feed was tested
  with two fake sub-feeds (`tests/test_feed.py`: merged stream, one market
  failing, both failing, universe tagging) and the *connectivity* path only up to
  "the server starts, serves the page, and reports the feed as reconnecting".
  Round 7 exists because a live-only bug survived exactly that gap — the
  composite feed was never exercised. What is still unverified against the real
  exchange: the futures websocket payload, the contract REST shapes and the
  per-market universe endpoint. `python run.py check` prints one line per market
  so the first run on your machine tells you which of them is answering. The behaviour after a socket
  connects but stays silent is covered by an integration test that reproduces
  exactly that state and then checks that candles and ticks still arrive. Everything else — deep history, the forming candle,
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
* **The plan is measured, not promised.** Round 6 gives every zone an entry, a
  stop and a target, but that does not make the app a signal service with a win
  rate: what it defends is **expectancy**, published with its data, method and
  limits in [`docs/plan-btcusdt-1m.md`](docs/plan-btcusdt-1m.md) (90 days of
  real 1m BTC, 9,541 trades, no fees or slippage). The headline is uncomfortable
  and stays on the page: **all plans together are −0.11 R**, the sniper subset
  (R:R ≥ 1.5) is +0.80 R, and **100 % accuracy does not exist** — a stop placed
  outside the noise gets hit. Re-run the command for another instrument before
  trusting the same numbers there.
* **The futures candles in the replay are spot candles under a contract name.**
  MEXC's contract API is unreachable from the build sandbox, so
  `data/fut/BTC_USDT-1m-2025-01.csv` is real *spot* BTC data renamed (see
  `data/fut/README.md`). It proves the plumbing — two markets, two feeds, two
  sections, two persisted lists — not that contract candles behave differently.
  On your machine the live futures feed uses `contract.mexc.com` and the
  contract websocket, and `python run.py check` reports both markets.
* **The grade is a ranking, not a guarantee.** A+ and A have the same 2-ATR hit
  rate (83%); A+ is kept for its larger average travel. C zones still work about
  half the time — they are ranked last, not declared wrong.
* **The "tradeable now" badge is the book's rule, not a measured edge.** With a
  fixed 1–2 ATR target, trend alignment at the touch showed no difference
  (77% vs 76%); with the *next liquidity pool* as the target an earlier version
  of the study showed 62% vs 35%, because in a trend price simply travels
  further. Both numbers are in the report; neither is a promise.
