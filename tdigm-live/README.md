# TDIGM · break + HVB bot

A small, standalone scanner. It watches a list of instruments on Binance spot in
**real time**, and tells you exactly one thing:

> **which instrument, on which timeframe, just closed a candle that broke a
> high/low AND printed an HVB on TDIGM (50, 50, 1, 9) on that same candle.**

Nothing else. No backtest, no account, no orders, no strategy suggestions.

```
instrument  TF     trigger                        fast   base
BTCUSDT     15m    high break + HVB  (LONG)       55.32  54.16
ETHUSDT     5m     low break + HVB   (SHORT)      43.34  44.79
```

---

## What it does, exactly

For every instrument and every timeframe (1m, 5m, 15m, 30m, 1h):

1. **Data** comes from Binance's public kline websocket (`@kline_1m`), one
   combined stream for all instruments. 3m/5m/15m/30m/1h candles are built by
   aggregating the 1m candles on clock boundaries - so a 15m candle closes at
   :00/:15/:30/:45 and its history never changes afterwards.
2. **TDIGM** is computed with your setting:

   ```
   r     = RSI(close, 50)
   fast  = SMA(r, 1)        # the purple line
   slow  = SMA(r, 9)        # the signal line
   base  = SMA(r, 50)       # the centre band
   band  = 1.6185 * stdev(r, 50)
   ```

   **HVB** = `fast` crosses `base` (up = bullish, down = bearish).
3. **Break** = the candle **closes** beyond the highest high (or lowest low) of
   the previous 20 candles. A wick alone does not count.
4. If both happen on the **same closed candle**, it is a **SETUP** and it is
   pushed to the page instantly (green/red cell + alert + optional beep).

Partial detections ("break only", "HVB only") are also shown, so you can see a
setup building. They are marked as partial and never counted as a setup.

**Only closed candles raise alerts.** The candle that is still forming is shown
as a live preview under the timeframe it will close on.

---

## Run it

```bash
cd tdigm-live
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1) is this machine actually able to reach the exchange?
python run.py check

# 2) live
python run.py serve
#    -> http://localhost:8700
```

`check` tests the REST API **and** the websocket. If it fails, the bot cannot
work: use a VPN / different network, or run the replay below to confirm the bot
itself is fine.

### Choose the instruments

Edit `config.json`:

```json
"symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
```

or pass them on the command line:

```bash
python run.py serve --symbols BTCUSDT,ETHUSDT,SOLUSDT,AVAXUSDT
```

Timeframes live in the same file; delete any you do not care about.

### Replay (no internet needed)

`s` = bars per second. This pushes **real recorded Binance candles** through the
same code path and labels the page `REPLAY`:

```bash
python run.py replay data/btc 150
```

`data/btc` in this repo contains BTCUSDT 1-minute candles for 2025-01-01 →
2025-06-29 (180 files). Point `replay` at any folder of Binance CSV files
(`SYMBOL-1m-YYYY-MM-DD.csv`).

---

## Configuration

| key | meaning |
|---|---|
| `symbols` | instruments to watch (`BTCUSDT`, …) |
| `timeframes` | any of `1m 3m 5m 15m 30m 1h` |
| `tdi.rsi_period` | RSI length (yours: 50) |
| `tdi.band_period` | centre band / stdev length (yours: 50) |
| `tdi.fast_period` | purple line (yours: 1) |
| `tdi.slow_period` | signal line (yours: 9) |
| `tdi.band_mult` | band width multiplier (1.6185, the TDI default) |
| `break_lookback` | a break must beat the last N candle highs/lows (default 20) |
| `hvb_min_points` | 0 = any cross counts; e.g. 2 = the break must be 2 RSI points aggressive |
| `server.port` | default 8700 |

---

## What "working" means here

```bash
python -m pytest tests -q     # 16 tests, no network needed
```

* the batch TDIGM and the incremental one produce identical numbers,
* the RSI is Wilder's (the same as TradingView's `rsi()`),
* the rollup builds correct 5m/15m candles (open/high/low/close/volume, clock
  aligned),
* a break requires a close beyond the window, not a wick,
* the engine raises a `signal` only when the HVB and the break agree on the same
  candle, never on a forming candle, and never twice for the same candle.

The page shows a **LIVE** badge only when a live websocket is connected; in
replay it says **REPLAY · real recorded data**. If no candle arrives for 150
seconds the badge turns red and says *stale* - silence is never reported as calm.

## Troubleshooting

| symptom | cause |
|---|---|
| `check` fails on REST and websocket | network/DNS blocks Binance. VPN, or another machine. |
| page says `disconnected` | the browser lost the websocket; it reconnects by itself, check the server window for the error. |
| cells say `warming 37` | normal at start: TDIGM needs ~105 candles before it can say anything. Live warm-up loads that history from REST; replay needs the first ~105 minutes. |
| no alerts for a long time | that is information, not a bug: a break + HVB on the same candle is rare. Untick the filter to see the partials. |
| `disconnected` after a while in replay | replay loop ended; it restarts automatically if `loop=True`. |

## Files

```
tdigm-live/
  run.py              check / serve / replay
  config.json         instruments, timeframes, TDIGM setting
  bot/indicators.py   RSI, SMA, stdev, TDIGM, HVB, break, incremental tracker
  bot/buckets.py      1m -> 3m/5m/15m/30m/1h aggregation
  bot/feed.py         Binance websocket + REST warm-up, and the replay feed
  bot/engine.py       per-instrument/timeframe state, alerts, dedupe
  bot/server.py       FastAPI + websocket push
  web/index.html      the page (no build step, no framework)
  tests/              indicator + aggregation + engine tests
```
