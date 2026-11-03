# TDIGM · liquidity scanner

A standalone real-time scanner. In the shipped live configuration it discovers
**every active MEXC USDT perpetual** automatically, shards the contracts across
live sockets, and scans 1m/3m/5m/15m/30m/1h/2h/4h/1d concurrently. It tells you
exactly one thing:

> **this instrument, on this timeframe, swept exactly one endpoint of a
> completed structural leg, then closed through that same leg's opposite
> endpoint with an HVB on TDIGM (50, 50, 1, 9) — here are the eligible
> mitigation zones, structural proof, and lifecycle state.**

No orders, no account, no backtest, no other strategy. It is a scanner.

The latest defect-by-defect review and the exact verification boundary are in
[`docs/audit-2026-11-01.md`](docs/audit-2026-11-01.md). The production 45-symbol /
zero-signal diagnosis is recorded separately in
[`docs/production-recovery-2026-11-03.md`](docs/production-recovery-2026-11-03.md).

```
BTCUSDT   15m   SHORT   pending mitigation   target —
          swept one high 103,412.00 → broke its paired low 102,780.00 → same-candle HVB down
```

---

## The setup, exactly (the drawing, in code)

```
   swing high  ────  liquidity          ← 1. a swing high is resting liquidity
        ▲
        │  poke through it
        ▼
   ──────────────    sweep              ← 2. price takes that liquidity
        │
        │  close through the OTHER side
        ▼
   swing low   ────  break              ← 3. the reversal is confirmed
        │
        ▼
   next liquidity / range extreme       ← structural context (not scored exit)
```

For a **short**: the high of a completed high→low leg is swept (its stops are
taken), then a candle **closes below that exact leg's low**. For a **long**:
mirrored — sweep the low of a completed low→high leg, then close above that
exact leg's high.

| term | definition (nothing hidden) |
|---|---|
| swing / level | a high that no other high in `pivot_len` bars on either side beats (equal highs count — double tops are liquidity). Only confirmed `pivot_len` bars later, so nothing looks ahead. |
| paired leg | SHORT is a completed `high → later low` leg; LONG is a completed `low → later high` leg |
| sweep | `high >` that leg's high (SHORT) or `low <` that leg's low (LONG), on a **closed** candle; the run must take exactly one structural high/low at the selected degree |
| nested leg | a lower high inside a clean outer `LH+LL` leg (or higher low inside `HL+HH`) is internal to that one leg, matching the tick drawings; it is not promoted to a second same-degree range level |
| extension | price may continue beyond the selected endpoint; reaching a second **independent/equal or newly printed** same-side structural level—on one candle or later before confirmation—invalidates the run |
| confirmation | a later body close through the **other endpoint of that exact simple or clean composite leg** within `sweep_max_wait` bars |
| order-block stop | the run's final extreme beyond the swept high / low is retained when the order block fills |
| breaker stop | if the aggressive paired-endpoint breaker fills, use its distal edge plus `breaker_stop_buffer_atr` (0.10 ATR shipped), not the distant sweep extreme |
| structural draw | `range` / `next` remains evidence about where liquidity sits; it is not the scored trade exit |
| mitigation entry | first proximal-edge touch of the order block, or of the paired-endpoint breaker after an aggressive (≥1 ATR body) shift |
| trade target | armed **only after mitigation**, fixed at `entry ± 4 × |entry − stop|`; reaching it is a win |
| invalidated | stop reached while still pending, before either eligible zone was mitigated; this is unfilled and not a loss |

Both sides are tracked at the same time — taking buy-side liquidity does not
switch off the sell-side watch. That matters, because reversals usually take
both sides first.

**HVB — the price line crossing the CENTRE band, with speed.**

```
r        = RSI(close, 50)
fast     = SMA(r, 1)              # the price line (purple)
slow     = SMA(r, 9)              # trade signal line (red)
mbl      = SMA(r, 50)             # market base line = YELLOW line
upper    = mbl + 1.6185*stdev(r, 50)
lower    = mbl - 1.6185*stdev(r, 50)
centre   = (upper + lower) / 2    # exactly equals mbl - this is the centre band

HVB up   = on this candle: fast was at/below the centre, closes above it,
           by at least `hvb_min_points` RSI points, with a step of at least
           `hvb_min_speed` x the line's own average step
HVB down = the mirror image
```

What is **not** an HVB, and never was meant to be:

| event | HVB? | why |
|---|---|---|
| price line touches / rides the **upper** band | no | outer bands are context, not the trigger |
| price line touches / rides the **lower** band | no | same |
| price line crosses the **slow** (red) line | no | that is the TDI's own signal-line cross |
| price line drifts back across the centre by 0.1 pts | no | no speed, no break |
| price line crosses the centre and closes 2 pts beyond it in one candle | **yes** | the HVB |

The bands are drawn on the page, but `bot/indicators.py` never reads `upper` /
`lower` in any rule - only `centre_of(point)` decides. There is a test that
proves a ride along an outer band produces no HVB.

Two settings control the "with speed" part, both live on the page and saved to
`config.json`:

| setting | default | meaning |
|---|---|---|
| `hvb_min_points` | **1.0** | how far past the centre the line must **close**, in RSI points |
| `hvb_min_speed` | **1.5** | this candle's step, in multiples of the line's average step over 50 candles |
| `tdi.hvb_reference` | `mbl` | which line is "the centre": `mbl` (middle of the bands) or `fifty` |

Why those numbers: on real 1m BTC the median centre break is only **0.60 RSI
points** (43% under 0.5, 70% under 1.0), and only **4%** of centre crossings
arrive slower than 1.5x the line's normal stride. So the points gate removes
the grazes, and the speed gate removes the slow drifts, without touching the
genuine breaks.

The HVB must happen **on the confirming candle** — same candle, not "recently".

**Setups only form in a trending market.**

A range prints the same sweep-then-break shape as a trend. That is exactly why
it is dangerous, so before a setup is allowed the bot has to see an actual
trend. For established structure, two lagging regime measures also have to agree:

| measure | what it sees | default |
|---|---|---|
| **ADX(14)** | strength of directional movement, blind to direction. < 20-25 is the textbook "no trend" | must be **≥ 22** |
| **ER(50)** | Kaufman efficiency ratio: net move ÷ distance travelled. A clean trend is 0.4-0.8, a range 0.1-0.3 | must be **≥ 0.30** |
| **EMA(20) vs EMA(50)** | which way that trend points | used only when `regime_direction = with` |

```
trend  = ADX >= regime_adx_min  AND  ER >= regime_er_min
signal = structure AND HVB AND trend      (plus every other filter you leave on)
```

If the market is ranging the setup is not published at all; the rejection is
counted and named on the page ("ranging market: ADX 14.2 < 22", "ranging
market: ER 0.05 < 0.30"), and the matrix dims those cells and prints the ADX
next to them, so you can see *why* it is quiet instead of guessing. The one
narrow exception is the structural rule below: a fresh major/macro **body close
beyond the full remembered range edge** establishes the new direction immediately.
ADX/ER lag that event by construction, so they may not veto it; an internal BOS,
EMA direction, or ordinary momentum candle receives no such exception.

Both thresholds are sliders in ⚙ and are saved to `config.json`. Set them low
to see more, high to see only the strongest trends.

### Where the setup sits: trend, depth, break of structure (round 17)

Liquidity taken on its own is not a trade. The setup now has to sit somewhere
sensible, and each part of that is a switch in ⚙ **Trend & structure**:

| what the rule demands | config key | default |
|---|---|---|
| structural context must be evaluated, not inferred from indicators | `trend_context` / `clear_structure` | **on / on** |
| major or macro highs **and** lows must progress repeatedly in the trade direction | `min_trend_steps` | **2 each** |
| remembered recent structural steps that must agree with that direction | `min_directional_ratio` | **100%** |
| mixed/ranging left context may pass only after a major/macro body close beyond the full remembered edge | `fresh_shift_max_bars` | **20** |
| a **break of structure** happened in the trade's direction | `require_bos` | **on** |
| …within the last N bars | `bos_max_age` | 240 |
| the setup is no deeper than **internal inside one trend** | `max_internal_depth` | 2 |

A valid continuation therefore has established major/macro structure (two or
more high-side and low-side steps) plus a clear internal directional leg (one
clean `LH+LL` / `HL+HH` step is enough inside that established structure) or a
local range release. A reversal exception
exists only for a fresh major/macro body close beyond the furthest remembered
range edge; a small BOS inside the old range does not qualify. Depth is counted
in degrees of structure: **1** the break is itself the external break · **2** the
first internal break inside an external trend (allowed) · **3+** internal within
internal (**skipped**). Read from three degrees of pivots — minor 5, major 20,
macro 80 — with a 0.10 ATR tolerance.

### Confirming the break: HVB, volume, or both

The HVB (price line through the centre band) and a **high-volume bar** (candle
volume ≥ N× the average of the previous bars) can be required separately or
together:

* `confirm_mode: "all"` — hard AND; every switch that is on must hold (default);
* `confirm_mode: "score"` — each piece of evidence contributes a weight and the
  total must reach `confirm_min_score`. The score and its parts are printed on
  every card (`score 85`), so you can see exactly what it was worth.

Every alert now shows its evidence: `external bullish (HH+HL) · last break of
structure up, 42 bars ago · depth 2 internal · volume 1.81× · score 85`.

**Earlier gate experiment (legacy 2R scoring, not the current lifecycle)** —
BTCUSDT 1m, 2024-07 → 2025-06, four timeframes: targeting 2R, the gates cut signals from 144 to **68** and moved R/trade from +0.20 to
+0.25. The honest breakdown, including the two gates this sample does *not*
confirm, the noise floor (SE ≈ 0.17 R → no comparison reaches 2σ) and the
reproduction commands are in [`docs/trend-rules.md`](docs/trend-rules.md).

### What the page shows (round 18)

* **Setups** on the left, newest first, each with entry / stop / confirmation-leg
  target / score and an explained **state**. While `pending`, the target preview
  follows the favorable extreme of the sweep→same-candle HVB/BOS displacement
  leg, but no trade exists yet. `active` means an order block or eligible
  aggressive-shift breaker was mitigated; the last fully closed pre-mitigation
  H/L is then frozen as the target. A later target revisit becomes `win`, while
  a post-fill stop becomes `stopped`. `invalidated` means the stop went first
  while still unfilled, and the stored
  `state_reason` says exactly which rule fired. With `setup_max_bars: 0`, age
  alone never expires an untouched setup.
* **Setup-centred chart** on the right, drawn from real exchange candles. When
  an old signal is selected the server fetches history around that signal—not
  merely the latest 180 candles—so the swept H/L and break candle cannot vanish
  off-screen. The sweep candle/level is amber, the entire setup window is
  shaded, and the opposite body-break + same-candle HVB is outlined blue.
  Order-block and breaker-block zones are separately labelled. Entry and the
  frozen confirmation-leg target begin at the mitigation candle—not the
  breakout—while the stop remains visible
  from confirmation. Time/price axes, crosshair OHLC, drag-pan, wheel/axis zoom,
  fullscreen and reset-to-setup match the SMC-Radar chart. Valid setups open in
  **LIVE** mode: the latest exchange kline is appended as a visibly forming
  candle and refreshed once per second. The strip above the chart shows live
  price, the frozen/projected plan R:R, current P/L in initial-risk units, and
  remaining reward-to-risk from market price to target/stop. **SETUP** switches
  back to immutable setup-centred evidence. Forming data is display-only and can
  never create a signal or rewrite a lifecycle outcome before candle close.
* **Filters**: symbol search, timeframe chips (1m … 1d), side, grade, state and
  sort. Browser preferences and the selected signal survive a reload via local
  storage; strategy/filter/structure settings remain server-side in the atomic
  `config.json`. The live default discovers all active MEXC USDT perpetual
  contracts; `max_symbols: 2000` is only a safety cap.
* **Futures only by default** — MEXC/Binance USDT-M perpetuals, not spot.
  `python run.py serve --market spot` if you ever want the other book.

Every instrument is warmed by **replaying its history through the live engine**,
so a setup that confirmed before you opened the page is found, not missed; and a
history request that fails is retried three times, reported, and swept again every
minute instead of leaving that instrument silently dead. How that works, and the
exact definitions of the order block and of valid/invalid, are in
[`docs/setups.md`](docs/setups.md).

### The archive (round 20)

The page used to be a window on the process: a setup that printed at 03:10 was
gone from the app after a restart. Everything is now written to SQLite
(`data/tdigm.sqlite3`, WAL) the moment it is found, and updated through its whole
life - `pending → active → win | stopped | expired`. A restart shows the archive
**before a single candle arrives** and rehydrates every valid current-policy row
into the live state machine, so later candles can mitigate or invalidate it. An
outcome is write-once within one lifecycle version. Lifecycle v2 introduced
mitigation-first fixed 4R; v3 added breaker-local stops and one selected
structural sweep; v4 added the tick/cross degree-aware outer-leg model and full
remembered trend consistency. **Lifecycle v5 replaces the fixed exit** with the
favorable H/L of the displacement leg that starts at the sweep and contains the
same-candle HVB/body break. Fully closed pending candles may extend that extreme;
the mitigation candle is excluded and the previous extreme freezes on fill.
Deep replay intentionally replaces outcomes graded under any older rule set. The
board computes win rate and R/trade from current-version mitigated rows, not
memory.

At startup the complete archive is integrity-checked for the mandatory chain
`exactly one paired-leg H/L sweep → later body close through that same leg's opposite endpoint → correctly directed same-candle HVB`.
Malformed legacy rows are retained for audit but changed to `invalidated` and
never rehydrated as tradeable. New signals also pass this check before they can
reach SQLite. `/api/integrity` reports the checked and quarantined totals:

> win rate = confirmation-leg target wins / (target wins + stopped trades), after mitigation only

`include archive` (on by default) merges the stored list into the setups card;
`GET /api/signals` and `GET /api/signals.csv` read the same rows. A winning row's
R is its frozen target distance divided by initial risk; a stop is −1R. Unfilled
invalidations are excluded. Because each setup has a different reward distance,
there is no universal fixed break-even percentage.
`tools/backfill_archive.py` fills the archive from recorded candles and walks each
setup forward to decide its outcome. Why it is built this way, and what it
guarantees, is in [`docs/archive.md`](docs/archive.md).

A deterministic lifecycle-v5 regression over 1,317,520 BTCUSDT one-minute bars
(2022-07-01 through 2024-12-31), rolled into all nine configured timeframes,
retained 109 setups: 24 target wins, 84 stops, and one still-pending unmitigated
setup. That sample's win rate was 22.22%, mean result −0.256R/trade, and 95%
interval −0.701R to +0.189R (**not proven**). It is a single-symbol regression,
not a claim about the live MEXC universe. Input hash, exact command, per-timeframe
counts, and limitations are in
[`docs/lifecycle-v5-regression-2026-11-03.md`](docs/lifecycle-v5-regression-2026-11-03.md).

### Concurrent history and live scanning

Warm-up and rescan use the bounded `workers` pool (shipped as **24**); the runtime
"+symbols" queue is drained by the same worker count from one FIFO, so no
instrument starves behind a slow contract. Tests exercise a 30-worker override
and prove 30 requests are actually in flight, a failing symbol does not stop the
queue, and live closes are buffered while history is being rebuilt
(`tests/test_parallel.py`). The bound is deliberate: unbounded requests would
trigger MEXC rate limits and make coverage worse, not faster.

### Current live policy (strict, not silently loosened)

The deployed strategy follows the supplied checklist, even though that is
sparser than accepting near misses:

* `strategy: "sweep"` — the separate book/mitigation research engine is not the
  live decision engine;
* `hvb_window: 0` — the directional TDIGM centre-band HVB must occur on the
  **same closed candle** as the opposite-side body break;
* exactly one structural same-side level may be swept at the selected degree;
  simultaneous/sequential independent or equal range highs/lows invalidate it,
  while directional internals contained by one clean outer leg do not;
* `trend_context: true`, `clear_structure: true`, two outer structural steps with
  100% remembered-step consistency, plus ADX ≥ 22 / ER ≥ 0.30 — a directional flicker inside
  a visible range cannot certify a trend;
* a fresh reversal is accepted only when major/macro structure closes beyond the
  complete remembered range edge (within 20 bars);
* `setup_max_bars: 0` — an untouched historical setup does not disappear just
  because time passed;
* optional book-only gates (FVG, volume bar, liquidity pool blocking) are off
  because they are not part of the stated setup.

Signal volume comes from scanning the whole futures universe, nine timeframes and
5,000 bars per timeframe—not by changing “same candle” into “within three bars”.

### The book's confluences (p.42, p.53, p.87)

Every alert says where it sits in the book, and each of these is a switch:

| reading on the card | book | default |
|---|---|---|
| `SHIFT (minor break)` / `MS (major break)` | p.53 | label only |
| `strong high → weak low` | p.42 — the swept level caused the manipulation and the break; the level that failed is weak | shown |
| `entry premium 78% · sweep premium` | p.42 — where the trade and the manipulation sit in the dealing range | shown, gate **off** (measured harmful) |
| `2 FVG` | p.87 — imbalances the displacement printed | shown, gate off |
| `5 equal lows ahead` | p.42/p.53 — a liquidity pool in front of the trade | **gate on**: 3+ equal extremes refuse the trade |
| `with external structure` | p.53 — the last major break must agree | **gate on** |
| `structure 1.2 ATR` | how tall the swept→broken structure is | shown, gate off |

The measured effect of **every** switch, page by page, is in
[`docs/book-coverage.md`](docs/book-coverage.md) and, for the round-17 trend /
structure / volume gates, [`docs/trend-rules.md`](docs/trend-rules.md) — including the two that
measured *worse* and are therefore off by default. Nothing is on by faith.

---

## Run it

```bash
cd tdigm-live
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python run.py check                 # does this machine reach the exchange? (REST + websocket)
python run.py serve                 # LIVE — exchange from config.json (default: MEXC)
#   -> http://localhost:8700
```

Switching exchange is one flag:

```bash
python run.py serve --exchange binance
python run.py serve --exchange mexc
python run.py universe --exchange mexc --top 30     # list pairs by 24h turnover
```

### Start-up: the page is up in a second, the history fills behind it

Warm-up runs **after** the server is accepting requests, in a 24-worker pool.
It is breadth-first in two passes: every contract/timeframe first gets 1,000 bars
(`startup_history_bars`), then every contract is deep-replayed over the configured
5,000 bars. Newly added batches use the same shallow-then-deep queue. This avoids
spending all workers on deep history for the first 24 contracts while hundreds
remain blind. The live socket pump starts immediately; closed candles arriving
while a symbol is rebuilt are buffered per symbol and replayed in timestamp
order. Thus a full-universe deep scan does not create a blind live window. MEXC's
2,000-row REST cap is paged rather than silently truncating the requested history.

`python run.py replay data/btc 60` starts 7,800 minutes into the files and serves
that prefix as history, so **all five timeframes are warm in the first seconds**
— including 1h, which used to need two hours of real-time playback before it
could produce anything. `--skip N` moves the start point.

### If the page will not load

* **The table is fed by a websocket, but it never needs one.** The page also
  reads `/api/state` over plain HTTP at boot and keeps polling it every few
  seconds whenever the socket has been quiet for more than 4 s. Behind an HTTPS
  proxy, a VPN or a corporate gateway that will not carry `wss:`, the badge
  reads `live · polling` and everything still works — matrix, setups, reasons.
* **`live` + a red banner naming the exchange** means the bot is fine and the
  exchange is not reachable from that machine. It retries on its own; reloading
  will not help. REST requests deliberately do not advertise Brotli: MEXC has
  returned malformed `br` bodies for contract detail. If any content decoder
  still fails, that request is immediately retried with `Accept-Encoding:
  identity` instead of trapping the scanner on the 45-symbol fallback.
* `curl -s localhost:8700/api/health` answers in milliseconds and separates
  feed status, automatic-universe mode/error/count, fully/partially/empty warm
  symbols, failed cells, queue depth, frames, and the setup decision funnel.
  A failed discovery is also a persistent red banner: the page explicitly says
  it is scanning the configured fallback rather than pretending 45 is "all".

### Instruments — from the page, written to `config.json`

Open **＋ Instruments**:

* type a symbol (`BTCUSDT, ETHUSDT`) → added, warmed with history, saved;
* remove with the ✕ on any chip;
* **exchange universe**: every enabled USDT perpetual from MEXC contract detail,
  biggest 24h turnover first, searchable — add one, the **top 25**, or **all** of
  them. Ticker data ranks contracts but never removes an enabled contract; a
  partial ticker response can no longer collapse hundreds of listings to 45;
* every change is written to `config.json` immediately and survives a restart.

Or edit `config.json` / pass `--symbols BTCUSDT,ETHUSDT` on the command line.

`max_symbols` is a guard rail (shipped value **2000**, above the current MEXC
futures universe): each instrument costs roughly `timeframes × 180 candles` of
resident chart state. Above `preview_max_symbols` instruments the expensive
per-tick forming-candle preview is switched off; closed-candle scanning and
instant signals remain on.

### Replay — no internet needed

Real recorded Binance 1m candles pushed through the same code, labelled `REPLAY`
everywhere:

```bash
python run.py replay data/btc 150     # 150 bars per second
```

Replay never rewrites `config.json` — it only has files for a few symbols, so
changes made during a replay apply to that session only (the page says so).

`data/btc` is not committed; recreate it from this repo's cache or any folder of
`SYMBOL-1m-YYYY-MM-DD.csv` files:

```bash
mkdir -p data/btc && cp ../backend/data/cache/datasets/BTCUSDT-1m-*.csv data/btc/
```

---

## Filters (⚙ in the page, saved to `config.json`)

| filter | default | what it does |
|---|---|---|
| **structure** | on | the sweep → opposite break must be present. This is the setup itself. |
| **HVB** | on | the price line crosses the **centre band** on the signal candle |
| **HVB break** | 1.0 pts | the cross must close at least this far past the centre (0 = every graze counts) |
| **HVB speed** | 1.5× | this candle's step against the line's average step (0 = no speed requirement) |
| **trending markets only** | on | ADX(14) and the efficiency ratio must both clear their lines |
| **ADX at least / ER at least** | 22 / 0.30 | how much indicator trend strength is enough; never substitutes for swing structure |
| **clear left structure** | on; 2 outer steps / 100% | every remembered recent major/macro step must agree, plus at least one clean directional local step; otherwise only a fresh full-range release qualifies |
| **fresh major shift** | 20 bars | how recently a body close beyond the remembered major/macro range edge may establish direction |
| **regime direction** | any | `any`, or `with` the EMA20/EMA50 direction |
| **external structure** | on | the last MAJOR break (MS) must agree with the trade (p.53) |
| **liquidity pool** | on, 3 levels | refuse a trade aimed into 3+ equal highs/lows (p.42) |
| **FVG required** | off | the displacement must leave an imbalance (p.87) |
| **MS required** | off | the confirming break must be major (it never is — see the doc) |
| **premium / discount** | off, 0.5 | the sweep (or the entry) must sit in premium for shorts / discount for longs |
| **centre band** | middle of bands | `mbl` (yellow line, middle of upper/lower) or `fifty` (the 50 axis) |
| **20-bar break** | off | the older rule (close beyond the last 20 highs/lows) as an extra condition |
| **min R:R** | 0 | legacy/provisional confirmation-close gate; keep at 0 because the final entry, stop and variable target R are not all frozen until mitigation |
| **max bars after sweep** | 0 | skip reversals that took too long to arrive |
| **sides** | both | long only / short only |
| **trend** | off | with / against an EMA (length configurable) |
| **sessions (UTC)** | empty | only shout inside these windows |
| **cooldown** | 0 | quiet period per instrument+timeframe after a signal |
| **swing length** | 5 | how big a swing must be to count as liquidity |
| **max wait / level memory** | 60 / 180 | how long a sweep may take to confirm, how long a level stays valid |
| **post-mitigation target** | confirmation-leg H/L | favorable sweep→HVB/BOS leg extreme extends on fully closed pending candles, then freezes immediately before an order-block or eligible-breaker touch |
| **breaker aggression** | 1.0 ATR body | paired endpoint candle is a breaker only when the directed break body meets this threshold |
| **breaker stop buffer** | 0.10 ATR | after breaker mitigation, stop just beyond that block's distal edge; order blocks retain the sweep stop |
| **structural draw** | range extreme | `range` / `next` remains context, not the scored exit |

Every rejection is counted and shown ("no HVB on this candle 623"), so a quiet
screen is explained instead of looking broken.

### Legacy regime experiment (not current v5 results)

The tables below are retained as an audit of an earlier detector and its
range-extreme/2R research scoring. They predate exactly-one-sweep validation,
strict left-context structure, breaker-local stops, mitigation-first activation,
the confirmation-leg H/L exit, and the current ADX 22 / ER 0.30 policy. They
**must not** be quoted as the current win rate. No compatible local candle set is
present in this checkout and MEXC is unreachable from the development sandbox,
so there is no honest lifecycle-v5 historical result to report yet.

The earlier experiment used two years of BTCUSDT 1-minute candles. One year was
the exploratory sample and the other was held out.

**In-sample: 2024-07-01 → 2025-06-29 (364 days)**

| timeframes | signals | per day | median R:R |
|---|---|---|---|
| 1m | 159 | 0.44 | 1.29 |
| 5m | 37 | 0.10 | 1.15 |
| 15m | 14 | 0.04 | 0.51 |
| 30m | 11 | 0.03 | 0.28 |
| 1h | 4 | 0.01 | 0.79 |
| **all five** | **225** | **0.62** | – |

**What the two new rules remove** (same candles, same code, one switch at a time):

| filters | signals / 364 days | per day |
|---|---|---|
| structure + HVB, no trend gate | 923 | 2.54 |
| + trending markets only (default) | **225** | **0.62** |
| + trending only, tighter (ADX ≥ 22, ER ≥ 0.15) | 125 | 0.34 |
| + trending only, looser (ADX ≥ 20, ER ≥ 0.05) | 334 | 0.92 |
| + ADX alone, no ER (ADX ≥ 20) | 452 | 1.24 |

In that legacy sample, **76% of the detected shapes were classified as ranging**.
That observation motivated stricter filtering; it is not a v4 performance result.

**Held-out year: 2023-07-01 → 2024-06-30 (366 days)**

| | signals | per day |
|---|---|---|
| no trend gate | 976 | 2.67 |
| default trend gate | 283 | 0.77 |

**What this does *not* prove.** Each legacy signal was walked forward on its own
timeframe under the older synthetic scoring horizon. It was roughly break-even
with and without that trend gate:

| year | no gate (R per trade) | trend gate (R per trade) |
|---|---|---|
| 2024-07 → 2025-06 | −0.01 | −0.04 |
| 2023-07 → 2024-06 | −0.03 | −0.09 |

Read that honestly: the legacy trend gate was a discipline rule, not proven
alpha. Current lifecycle-v5 results must instead count only setups that meet the
new structure rules, are actually mitigated, and then reach either their
post-fill stop or their frozen confirmation-leg H/L target.

Re-run the current policy on a compatible local dataset (the counts will not
match the superseded tables above):

```bash
# the data (one command, straight from the public archive)
curl -sL -o /tmp/tradinbot.tgz https://codeload.github.com/Thaum-blip/TradinBot/tar.gz/refs/heads/main
tar xzf /tmp/tradinbot.tgz -C /tmp
python tools/import_csv.py /tmp/TradinBot-main/historical_data_BTCUSDT_1m_20220701_to_20250630.csv \
  /tmp/TradinBot-main/temp_data --symbol BTCUSDT --start 2024-07-01 --end 2025-06-29 --out data/btc
python tools/import_csv.py /tmp/TradinBot-main/historical_data_BTCUSDT_1m_20220701_to_20250630.csv \
  --symbol BTCUSDT --start 2023-07-01 --end 2024-06-30 --out data/btc_oos

# the defaults measured above
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,5m,15m,30m,1h --target range

# what each rule removes / the held-out year
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,5m,15m,30m,1h --target range --no-regime
python tools/scan_history.py data/btc_oos --symbol BTCUSDT --timeframes 1m,5m,15m,30m,1h --target range
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,5m,15m,30m,1h --target range --adx-min 22 --er-min 0.15
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,5m,15m,30m,1h --target range --no-regime --hvb-speed 0

# the tolerance sweep, and the two reference lines
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,3m,5m,15m,30m,1h --target range --hvb-points 0
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,3m,5m,15m,30m,1h --target range --hvb-points 0.5
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,3m,5m,15m,30m,1h --target range --hvb-points 2
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,3m,5m,15m,30m,1h --target range --centre fifty

# the comparisons
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,3m,5m,15m,30m,1h --target range --no-hvb
python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 1m,3m,5m,15m,30m,1h --target range --min-rr 1.0
```

This measures **how often the setup appears**, not a win rate — no exit
simulation is claimed here.

---

## The page

* **KPI strip** — complete setups, armed setups, sweeps seen, filtered out (with reasons).
* **Setup alerts** — entry, stop, target, R:R, which level was swept, which level broke,
  how many bars after the sweep, in the browser's local time. Untick "setups only"
  to also see sweeps and single-condition events.
* **Armed** — liquidity already taken, the level being watched for the break.
* **Matrix** — instrument × timeframe; green/red = EMA direction, amber = armed,
  "warming" = not enough candles yet. Hover for the numbers.
* **Badges** — `LIVE` only when a live socket is connected, `REPLAY · real recorded
  data` for a replay, `disconnected` otherwise. No candle for 180s turns the age
  red and says the stream is silent — silence is never shown as calm.
* sound on signals (toggle), everything inline — no CDN, no build step, no fonts.

---

## Configuration reference

```jsonc
{
  "symbols": ["BTCUSDT", "ETHUSDT"],     // fallback if universe discovery is unavailable
  "exchange": "mexc", "market": "futures",
  "auto_universe": true, "max_symbols": 2000, "workers": 24,
  "universe_min_expected": 100, "universe_retry_seconds": 60,
  "universe_refresh_seconds": 3600,
  "strategy": "sweep", "startup_history_bars": 1000, "history_bars": 5000,
  "timeframes": ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"],
  "tdi":   { "rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9, "band_mult": 1.6185 },
  "structure": { "pivot_len": 5, "sweep_max_wait": 60, "level_max_age": 180, "max_levels": 40, "target_mode": "range" },
  "filters": { "structure": true, "hvb": true, "hvb_window": 0, "regime": true,
               "trend_context": true, "setup_max_bars": 0, "sides": ["long", "short"], ... },
  "break_lookback": 20,
  "server": { "host": "0.0.0.0", "port": 8700 }
}
```

MEXC futures note: native contract history is used for 1m/5m/15m/30m/1h/4h/1d.
MEXC has no native 3m or 2h contract candles, so 3m is aggregated from 1m and 2h
from clock-aligned 1h bars. Live candles are always rolled up from the 1m stream.

---

## What "works" means here

```bash
python -m pytest tests -q      # full deterministic suite, no network needed
```

Covered: batch TDIGM equals the incremental one; Wilder's RSI; the 5m/15m rollup
(open/high/low/close/volume, clock aligned); a break needs a close, not a wick;
a completed simple or clean composite leg's H/L sweep → exact opposite endpoint
break produces one setup; the tick-shaped directional internals are accepted,
while equal/contracting X ranges and unrelated newer pivots are rejected; the
structural stop follows the run's final extreme, breaker mitigation switches to
its buffered distal-edge stop, and no target exists before mitigation; filters
reject for the stated reason and count it; alerts
never fire on a forming candle or twice on the same one; adding/removing
instruments writes `config.json`; MEXC kline frames parse in both encodings;
feed sharding splits 250 symbols into 100/100/50 sockets.

The deterministic replay, server, persistence, and browser smoke paths are
covered offline. Earlier legacy studies used recorded BTC data, but no compatible
candle archive is present in this checkout, so no lifecycle-v4 performance number
is inferred from those old runs.

**Not verified here:** the live websocket to MEXC/Binance. Every exchange host is
blocked from this sandbox (`python run.py check` shows the exact TLS failure), so
the live path was written to the documented protocols but must be confirmed on
your machine — `python run.py check` does that in one command, and the page says
`disconnected`/`stale` rather than pretending.

---

## Files

```
tdigm-live/
  run.py                 check / serve / replay / universe
  config.json            instruments, timeframes, TDIGM, structure, filters
  bot/structure.py       pivots, levels, sweeps, opposite break, stop & target
  bot/filters.py         the gate + rejection counters
  bot/indicators.py      RSI, SMA, stdev, TDIGM, HVB, incremental tracker
  bot/buckets.py         1m -> 3m/5m/15m/30m/1h, clock aligned
  bot/feed.py            MEXC + Binance websockets (sharded), REST warm-up, replay
  bot/engine.py          per instrument/timeframe state, alerts, dedupe, preview
  bot/server.py          HTTP + websocket + the controls that write config.json
  bot/config_store.py    atomic config read/write + validation
  web/index.html         the page (no framework, no build)
  tools/scan_history.py  the measurement tool used for the numbers above
  bot/store.py           the SQLite archive: every setup, for good
  tools/backfill_archive.py  fill the archive from recorded candles
  tests/                 deterministic strategy, feed, archive and concurrency tests
```

## Troubleshooting

| symptom | cause |
|---|---|
| `check` fails on REST and websocket | network/DNS/VPN blocks the exchange. Try the other exchange, a VPN, or another machine. |
| page says `disconnected` | browser lost the websocket; it reconnects by itself. Check the server terminal. |
| instruments stay `warming` | TDIGM needs ~110 candles per timeframe. Live warms from REST at start; replay needs those candles to play. |
| very few alerts | inspect `/api/health` coverage and the rejection funnel first; untick "setups only" to see sweeps. Do not loosen the mandatory same-candle HVB/structure chain to manufacture signals. |
| "add all" stops early | `max_symbols` safety cap (shipped as 2000). Raise it only if an exchange lists more active contracts. |
| MEXC stream silent but REST fine | MEXC occasionally rate-limits new sockets; the feed reconnects with backoff. Try `--exchange binance` to compare. |
