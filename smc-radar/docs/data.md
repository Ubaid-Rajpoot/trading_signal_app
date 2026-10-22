# Where the candles come from

Nothing in this app makes prices up. There are exactly two sources.

## Live — MEXC

`bot/feed.py` (copied from `tdigm-live`, unchanged behaviour):

* REST `GET https://api.mexc.com/api/v3/klines?symbol=<SYM>&interval=<tf>&limit=…`
  for the warm-up and for the one-shot scan;
* websocket `spot@public.kline.v3.api@<SYM>@Min1` for the stream — MEXC only
  offers 1m klines, so 3m/5m/15m/30m/1h are built from the 1m candles by
  `bot/buckets.py`, clock-aligned, closed candles only.

`python run.py check` prints whether this machine can reach both. If it cannot,
the server still starts and says so on the page.

## Recorded — replay

`python run.py replay <folder> <bars-per-second>` plays CSV files named
`<SYMBOL>-....csv` with columns
`open_time, open, high, low, close, volume` (extra columns are ignored; the
timestamp may be seconds, milliseconds or microseconds).

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
