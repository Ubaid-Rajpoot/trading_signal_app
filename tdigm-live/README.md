# TDIGM · liquidity scanner

A standalone real-time scanner. It watches a list of instruments on **MEXC** (or
Binance) that **you** choose, builds **every timeframe** from the live 1-minute
stream, and tells you exactly one thing:

> **this instrument, on this timeframe, just closed a candle that took a
> high/low liquidity level, then broke the opposite side, with an HVB on the
> TDIGM (50, 50, 1, 9) — here is the entry, the stop beyond the sweep, and the
> liquidity it is aiming at.**

No orders, no account, no backtest, no other strategy. It is a scanner.

```
BTCUSDT   15m   SHORT   entry 102,431.50   stop 103,988.20   target 100,902.00   R:R 0.98
          swept high 103,412.00 → broke 102,780.00 → HVB down (price line 46.12 crossed the centre band 48.40)
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
   next liquidity / range extreme       ← target
```

For a **short**: a swing high is swept (its stops are taken), then a candle
**closes below the most recent swing low**. For a **long**: mirrored — a swing
low is swept, then a candle closes above the most recent swing high.

| term | definition (nothing hidden) |
|---|---|
| swing / level | a high that no other high in `pivot_len` bars on either side beats (equal highs count — double tops are liquidity). Only confirmed `pivot_len` bars later, so nothing looks ahead. |
| sweep | `high > level` (buy-side) or `low < level` (sell-side) on a **closed** candle |
| sweep killed | a later **close** beyond the sweep extreme — price accepted the new level, so there is nothing to reverse |
| confirmation | close beyond the most recent intact swing on the other side, within `sweep_max_wait` bars |
| stop | the sweep extreme (just beyond the swept high / low) |
| target | `range`: the far extreme of the range that existed **before** the sweep (the dashed line / arrow in the drawing). `next`: the nearest resting liquidity beyond the entry. Small swings printed *inside* the reversal leg are never used as a target. |
| entry | the close of the confirming candle |

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
trend. Two independent measures have to agree:

| measure | what it sees | default |
|---|---|---|
| **ADX(14)** | strength of directional movement, blind to direction. < 20-25 is the textbook "no trend" | must be **≥ 20** |
| **ER(50)** | Kaufman efficiency ratio: net move ÷ distance travelled. A clean trend is 0.4-0.8, a range 0.1-0.3 | must be **≥ 0.10** |
| **EMA(20) vs EMA(50)** | which way that trend points | used only when `regime_direction = with` |

```
trend  = ADX >= regime_adx_min  AND  ER >= regime_er_min
signal = structure AND HVB AND trend      (plus every other filter you leave on)
```

If the market is ranging the setup is not published at all; the rejection is
counted and named on the page ("ranging market: ADX 14.2 < 20", "ranging
market: ER 0.05 < 0.10"), and the matrix dims those cells and prints the ADX
next to them, so you can see *why* it is quiet instead of guessing.

Both thresholds are sliders in ⚙ and are saved to `config.json`. Set them low
to see more, high to see only the strongest trends.

### Where the setup sits: trend, depth, break of structure (round 17)

Liquidity taken on its own is not a trade. The setup now has to sit somewhere
sensible, and each part of that is a switch in ⚙ **Trend & structure**:

| what the rule demands | config key | default |
|---|---|---|
| the trade agrees with the **external** trend (HH+HL for a long, LH+LL for a short) | `trend_context` | **on** |
| the whole visible swing sequence is clean, not just the last leg | `require_hh_hl` | **on** |
| a **break of structure** happened in the trade's direction | `require_bos` | **on** |
| …within the last N bars | `bos_max_age` | 240 |
| the setup is no deeper than **internal inside one trend** | `max_internal_depth` | 2 |

Depth is counted in degrees of structure: **1** the break is itself the external
break · **2** the first internal break inside an external trend (allowed) ·
**3+** internal within internal (**skipped**). Read from three degrees of
pivots — minor 5, major 20, macro 80 — with a 0.10 ATR tolerance so one flat
swing does not flip the label.

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

**What it measured** — BTCUSDT 1m, 2024-07 → 2025-06, four timeframes, 2R
target: the gates cut signals from 144 to **68** and moved R/trade from +0.20 to
+0.25. The honest breakdown, including the two gates this sample does *not*
confirm, the noise floor (SE ≈ 0.17 R → no comparison reaches 2σ) and the
reproduction commands are in [`docs/trend-rules.md`](docs/trend-rules.md).

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

### Instruments — from the page, written to `config.json`

Open **＋ Instruments**:

* type a symbol (`BTCUSDT, ETHUSDT`) → added, warmed with history, saved;
* remove with the ✕ on any chip;
* **exchange universe**: every USDT pair on the exchange, biggest 24h turnover
  first, searchable — add one, the **top 25**, or **all** of them;
* every change is written to `config.json` immediately and survives a restart.

Or edit `config.json` / pass `--symbols BTCUSDT,ETHUSDT` on the command line.

`max_symbols` (default 500) is a guard rail: each instrument costs roughly
`timeframes × 180 candles` of memory. Raising it is fine; the page tells you when
the limit stopped an add. Above 200 instruments the per-tick live preview is
switched off (the table still updates; only the "forming candle" preview goes).

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
| **ADX at least / ER at least** | 20 / 0.10 | how much trend is enough |
| **regime direction** | any | `any`, or `with` the EMA20/EMA50 direction |
| **external structure** | on | the last MAJOR break (MS) must agree with the trade (p.53) |
| **liquidity pool** | on, 3 levels | refuse a trade aimed into 3+ equal highs/lows (p.42) |
| **FVG required** | off | the displacement must leave an imbalance (p.87) |
| **MS required** | off | the confirming break must be major (it never is — see the doc) |
| **premium / discount** | off, 0.5 | the sweep (or the entry) must sit in premium for shorts / discount for longs |
| **centre band** | middle of bands | `mbl` (yellow line, middle of upper/lower) or `fifty` (the 50 axis) |
| **20-bar break** | off | the older rule (close beyond the last 20 highs/lows) as an extra condition |
| **min R:R** | 0 | skip setups whose target pays less than this |
| **max bars after sweep** | 0 | skip reversals that took too long to arrive |
| **sides** | both | long only / short only |
| **trend** | off | with / against an EMA (length configurable) |
| **sessions (UTC)** | empty | only shout inside these windows |
| **cooldown** | 0 | quiet period per instrument+timeframe after a signal |
| **swing length** | 5 | how big a swing must be to count as liquidity |
| **max wait / level memory** | 60 / 180 | how long a sweep may take to confirm, how long a level stays valid |
| **target** | range extreme | `range` (drawing) or `next` liquidity |

Every rejection is counted and shown ("no HVB on this candle 623"), so a quiet
screen is explained instead of looking broken.

### What the defaults actually do — measured

Two years of real BTCUSDT 1-minute candles, imported with `tools/import_csv.py`
and replayed through this exact engine (`tools/scan_history.py`). One year picks
nothing - the other is a held-out check.

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

So **76% of the setups that formed were forming in a ranging market** and are
now not shown at all. Rejection counters on the page show the last few of them
live.

**Held-out year: 2023-07-01 → 2024-06-30 (366 days)**

| | signals | per day |
|---|---|---|
| no trend gate | 976 | 2.67 |
| default trend gate | 283 | 0.77 |

**What this does *not* prove.** Each signal was walked forward on its own
timeframe (stop vs the range-extreme target, 300 bars, stop wins ties). On this
data the fixed-target walk-forward is roughly break-even both with and without
the trend gate:

| year | no gate (R per trade) | trend gate (R per trade) |
|---|---|---|
| 2024-07 → 2025-06 | −0.01 | −0.04 |
| 2023-07 → 2024-06 | −0.03 | −0.09 |

Read that honestly: the trend gate does the job it was asked to do - it deletes
range setups - but it is **not** a proven profit filter on one year of BTC, and
neither is the entry by itself. The exit (stop at the sweep extreme, target at
the range extreme) is what decides the P&L, so treat the gate as a discipline
rule, not as alpha. If you want the numbers to be different, the things to test
are the target mode and `min_rr`, which `scan_history.py` measures the same way.

Reproduce:

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
  "symbols": ["BTCUSDT", "ETHUSDT"],     // editable from the page
  "exchange": "mexc",                    // "mexc" or "binance"
  "max_symbols": 500,
  "timeframes": ["1m", "5m", "15m", "30m", "1h"],   // add "3m" if you want it
  "tdi":   { "rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9, "band_mult": 1.6185 },
  "structure": { "pivot_len": 5, "sweep_max_wait": 60, "level_max_age": 180, "max_levels": 40, "target_mode": "range" },
  "filters": { "structure": true, "hvb": true, "break": false, "min_rr": 0, "sides": ["long", "short"], "trend": "off", ... },
  "break_lookback": 20,
  "server": { "host": "0.0.0.0", "port": 8700 }
}
```

MEXC note: MEXC spot has 1m/5m/15m/30m/60m candles (no 3m), so a 3m timeframe is
built locally from 1m — the code always builds every timeframe from 1m anyway.

---

## What "works" means here

```bash
python -m pytest tests -q      # 48 tests, no network needed
```

Covered: batch TDIGM equals the incremental one; Wilder's RSI; the 5m/15m rollup
(open/high/low/close/volume, clock aligned); a break needs a close, not a wick;
sweep → opposite break produces exactly one setup with the stop beyond the sweep
extreme and no target from levels printed inside the reversal leg; a sweep that
keeps going is killed; filters reject for the stated reason and count it; alerts
never fire on a forming candle or twice on the same one; adding/removing
instruments writes `config.json`; MEXC kline frames parse in both encodings;
feed sharding splits 250 symbols into 100/100/50 sockets.

Verified end-to-end in this sandbox on real recorded data (replay): 5 timeframes
warm, sweeps and setups arrive over the websocket, arm state renders, config
writes round-trip, page renders with zero JS errors.

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
  tests/                 48 tests
```

## Troubleshooting

| symptom | cause |
|---|---|
| `check` fails on REST and websocket | network/DNS/VPN blocks the exchange. Try the other exchange, a VPN, or another machine. |
| page says `disconnected` | browser lost the websocket; it reconnects by itself. Check the server terminal. |
| instruments stay `warming` | TDIGM needs ~110 candles per timeframe. Live warms from REST at start; replay needs those candles to play. |
| very few alerts | that is the strategy. Untick "setups only" to see the sweeps; loosen HVB or `min_rr` in ⚙. |
| "add all" stops early | `max_symbols` (500). Raise it in `config.json` if your machine has the RAM. |
| MEXC stream silent but REST fine | MEXC occasionally rate-limits new sockets; the feed reconnects with backoff. Try `--exchange binance` to compare. |
