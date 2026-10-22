# Where the candles come from

Nothing in this app makes prices up. There are exactly two sources.

## Live — MEXC

`bot/feed.py` (copied from `tdigm-live`, unchanged behaviour):

* REST `GET https://api.mexc.com/api/v3/klines?symbol=<SYM>&interval=<tf>&limit=…`
  for the warm-up and for the one-shot scan;
* websocket `spot@public.kline.v3.api@<SYM>@Min1` for the stream — MEXC only
  offers 1m klines, so 3m/5m/15m/30m/1h are built from the 1m candles by
  `bot/buckets.py`, clock-aligned, closed candles only.

The websocket is tried on **both known MEXC hosts** (`wbs.mexc.com` and
`wbs-api.mexc.com`), and a socket that connects but sends no kline for 20 seconds
is dropped for the next one - "connected" is only reported when a candle has
actually arrived.

Independently of the socket, `bot/poller.py` re-reads the same REST kline
endpoint every few seconds and feeds the engine: the still-open candle as a
**tick**, each candle once as **closed**. That is why `candles_seen` and
`ticks_seen` keep climbing even if a proxy silently swallows the websocket; it
is the same real exchange data, just fetched instead of pushed.

`python run.py check` prints whether this machine can reach both. If it cannot,
the server still starts and says so on the page.

Warm-up asks the REST endpoint for **1000 candles per timeframe** (the most a
kline endpoint returns in one call), so the chart opens filled instead of with a
dozen candles. Symbol added later? The server warms that one in the background
while the stream keeps running.

The **candle that is still forming** comes from the websocket: MEXC re-sends the
open bar on every trade, and those updates go to the chart (`closed: false`)
while the detection map waits for the close.

## Recorded — replay

`python run.py replay <folder> <bars-per-second> [--preroll N]` plays CSV files named
`<SYMBOL>-....csv` with columns
`open_time, open, high, low, close, volume` (extra columns are ignored; the
timestamp may be seconds, milliseconds or microseconds).

`--ticks N` (default 4) shows each recorded candle as N partial updates, so the
candle can be watched forming offline exactly as it forms live. The partial
prices are interpolated **between that candle's own open, high, low and close** —
a recorded file has no ticks and the app does not invent any. `--ticks 1` turns
the effect off and replays closed candles only.

Before streaming starts, the replay loads the **first 2400 candles** of each file
through the same code path the live feed uses (`--preroll` changes it, `0`
disables it), so the replay opens with a deep chart and a warm map and then
carries on from that point. When the replay loops back to the start, the engine
notices the time going backwards and rebuilds that instrument instead of
stacking duplicate candles.

The verification in `README.md` uses real Binance 1-minute data for BTCUSDT,
1–7 January 2025 (10,080 bars) at:

```
smc-radar/data/btc/BTCUSDT-1m-2025-01.csv
```

That folder is **git-ignored** (see the root `.gitignore`): market data is
downloaded, never committed. To rebuild the file, concatenate daily
`BTCUSDT-1m-YYYY-MM-DD.csv` files from any real provider (the public
`Thaum-blip/TradinBot` dataset was used while building this) — the app does not
care where the file came from, only that the candles are real.
