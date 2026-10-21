# Running it locally, with real Binance data

This sandbox has **no access to Binance** (the connection is closed before TLS
finishes), so everything it shows comes from a synthetic feed. On your own
machine the same code talks to Binance directly. This page is the whole path from
a fresh clone to a real scan.

## 0. What you need

* Python **3.11+**
* Node **18+** (only for the UI)
* Internet access to `api.binance.com` — no API key, no account, nothing to pay

## 1. Install

```bash
git clone https://github.com/Ubaid-Rajpoot/trading_signal_app.git
cd trading_signal_app

python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt

cd frontend && npm install && cd ..
```

## 2. Prove the data is real *before* looking at any signal

This is the step that matters. It asks the exchange directly and tells you what
would be used:

```bash
python scripts/winrate.py --symbol BTCUSDT --days 30 --timeframes 15m --explain
```

You want to see:

```
OK   https://api.binance.com            220 ms  HTTP 200, last close 67,412.55 ...
     => source: LIVE  (REAL exchange data)
```

If it says `FAIL ... TLS/SSL connection has been closed`, Binance is blocked for
your network — see [troubleshooting](#7-if-binance-is-blocked). **Do not trust
any number until this line says LIVE.**

## 3. Scan the market

```bash
# top 100 USDT pairs by turnover, the 5 scalping timeframes
python scripts/scan.py --top 100 --real-only

# just your own list, including higher timeframes
python scripts/scan.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --timeframes 5m,15m,1h,4h --real-only
```

Output is one row per instrument **and timeframe** that has something to say:

```
STATUS   INSTRUMENT  TF   SIDE        PRICE        LEVEL    DIST  MARKET / REASON
SETUP    SOLUSDT     15m  SHORT     143.2200     142.9000  -0.22%  TRENDING DOWN
ARMED    ETHUSDT     5m   LONG    2,866.4900   2,875.1700  +0.30%  TRENDING UP
FORMING  BTCUSDT     1m   LONG   60,877.2500  60,936.8400  +0.10%  TRENDING DOWN
BLOCKED  XRPUSDT     15m  LONG        0.5800       0.5800  +0.13%  ranging - choppy: efficiency ratio 0.11
```

* `--real-only` makes it **exit with code 1** instead of quietly reporting demo
  numbers. Always use it when the numbers matter.
* `--all` also shows the IDLE rows.
* `--json` for a machine-readable dump, `--out docs/reports/scan.md` for markdown.
* `--top 100` needs ~500 requests (100 pairs × 5 timeframes); with `--workers 6`
  that is a couple of minutes the first time, then it is cached.

Where the symbol list comes from:

```bash
python scripts/scan.py --list-symbols --top 100
# 100 symbols (source: live)
```

`live` = Binance `exchangeInfo` + 24h turnover. `curated` = the built-in list,
which is what you get offline.

## 4. Start the app

```bash
./scripts/dev.sh          # API on :8000, UI on :5173 (Vite proxies /api)
```

Open <http://localhost:5173>. The badge in the header says **Live** or **Demo** —
that badge is wired to the same probe as step 2, not a guess.

The radar has a scan scope selector:

* **My watchlist** — the pairs you saved in Settings.
* **Top 25 by volume** — pulls the universe and scans 25 pairs × 5 timeframes.

For anything bigger than that, use `scripts/scan.py`; it is the same code path
without the browser in the middle.

## 5. Measure it

```bash
# the actual win rate of each version, 30 days, 5 timeframes
python scripts/winrate.py --symbol BTCUSDT --days 30 --strategy tdi_shift_v3 --real-only
python scripts/winrate.py --symbol BTCUSDT --days 30 --strategy tdi_shift_v1 --real-only

# inspect one timeframe trade by trade, then check a trade on TradingView
python scripts/winrate.py --symbol BTCUSDT --days 30 --timeframes 15m --trades --real-only

# the filter's effect: how many v1 setups v3 kept, and why it dropped the rest
python scripts/winrate.py --symbol BTCUSDT --days 30 --strategy tdi_shift_v3
```

Downloads are paged and cached under `backend/data/cache/history/`, so the second
run is instant and works offline afterwards.

## 6. Day-to-day

| Task | Command |
|---|---|
| Everything at once | `./scripts/dev.sh` |
| Tests | `make test` or `./.venv/bin/python -m pytest backend/tests -q` |
| Build the UI into the API | `./scripts/build.sh` |
| Run the nightly scanner on a timer (Linux/macOS) | `crontab -e` → `*/15 * * * * cd /path/to/trading_signal_app && .venv/bin/python scripts/scan.py --top 100 --real-only --out /tmp/scan.md` |

## 7. If Binance is blocked

Some networks (and some countries) block `api.binance.com`. In order:

1. **The mirror is already configured.** `data-api.binance.vision` is tried
   automatically; it is Binance's own public market-data host and usually works
   where the main API does not.
2. **A VPN** — Binance blocks by IP, so a normal consumer VPN is enough.
3. **Any other exchange** — the download layer is one function
   (`backend/app/core/data/history.py::download_range`). Point it at another
   exchange's klines endpoint that speaks the same JSON shape and everything
   downstream keeps working. Ask me and I will wire one in.

Until one of those works, the app will fall back to demo candles and **say so** in
every place a number appears — the header badge, the win-rate row, the report
footer, and `--real-only` will refuse to print anything.

## 8. Two things to understand about the numbers

* **HVB needs real volume.** `HVB` (High Volume Breakout) means the purple line
  crosses the centre band *on a volume spike* — 1.5× the 20-bar average by
  default. Synthetic candles have no real tape, so HVB counts there are
  meaningless. Real candles have it.
* **v3 is selective on purpose.** Outside structure, against the trend, in a
  range, or into resting liquidity → no trade. A scan that returns two setups out
  of a hundred pairs is the system working, not the system broken.
