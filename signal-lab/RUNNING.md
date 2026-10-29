# Running SignalLab

Two ways to run it. **Replay** needs no exchange and no API key — it walks the
9.6 M-bar archive bar by bar. **Live** polls MEXC futures every 45 seconds.

Both serve the same page on the same port.

---

## 0. Prerequisites

| | |
|---|---|
| Python | 3.11 or newer (`python3 --version`) |
| Disk | ~1.2 GB free (700 MB download + 370 MB archive + deps) |
| Internet | once, for the archive download; always, for live mode |
| OS | anything — Linux, macOS, Windows (PowerShell commands at the bottom) |

## 1. Install (once)

```bash
cd trading_signal_app/signal-lab
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.txt
```

`requirements.txt` is in `signal-lab/`. The venv lives one level up so both this app
and `smc-radar` can share it.

## 2. Replay — the fastest way to see it work

```bash
# once: download and pack the archive (BTC + ETH, 2017-08 → 2026-10). ~3 minutes.
../.venv/bin/python tools/build_dataset.py

# start the app
../.venv/bin/python app/server.py --replay --loop --speed 150 --port 8811
```

Open **http://localhost:8811**.

`--speed 150` means "150 bars per second across both symbols", roughly ten days of
market per second of wall clock. The whole 2024 → 2026 window takes about six minutes;
`--loop` starts it again when it reaches the end. Drop `--loop` to stop at the last bar.

The tape speeds up or slows down while it runs — buttons in the top-right, no restart.

## 3. Live — real MEXC futures

```bash
../.venv/bin/python app/server.py --live --port 8811
```

No API key is needed: these are the public kline endpoints. The page is the same; the
tape now fills in real time.

Default universe is `BTCUSDT,ETHUSDT` and timeframe `1h`. `BTCUSDT` is translated to
MEXC's `BTC_USDT` automatically, so either spelling works:

```bash
../.venv/bin/python app/server.py --live --symbols BTCUSDT,ETHUSDT,SOLUSDT --tfs 1h,4h
```

A symbol MEXC has no candles for is reported in `/api/health` and skipped — it will not
stop the app.

**Where signals are stored:** `app/data/signals.db` (SQLite). Rows are written when a
signal is created and again when it resolves, so refreshing the page never loses
anything, and stopping the app does not lose anything either. A live trade that is still
open at shutdown is adopted on the next start: its barriers are replayed over the
candles that arrived while it was down, and whatever is still running goes back under
supervision.

## 4. Flags

| flag | default | what it does |
|---|---|---|
| `--replay` / `--live` | replay | which clock to run on |
| `--speed N` | 25 | replay bars per second (live ignores it) |
| `--loop` | off | replay: start over at the end of the archive |
| `--cut P` | from `app/data/bands.json` (0.35) | probability cut; `0` = use the shipped one |
| `--symbols` | `BTCUSDT,ETHUSDT` | comma-separated |
| `--tfs` | `1h` | comma-separated; `4h` is available and less tested |
| `--port` | 8811 | |
| `--host` | 0.0.0.0 | `127.0.0.1` to keep it on this machine only |

## 5. What you should see

Four tabs:

* **Signals** — the tape. Every row: time, symbol, timeframe, band, probability,
  entry / stop / target, bars held, result, R. Header shows the ratio (1 : 4), the fee
  as a share of the reward, and which model is scoring.
* **Measured accuracy** — the reward : risk sweep (why 1 : 4) and the band table with
  confidence intervals and per-period records.
* **The 100 % club** — the 480 rules that never lost in sample, and what happened to
  them out of sample.
* **Method & limits** — data, splits, and what is explicitly not claimed.

Sanity check, no browser needed:

```bash
curl -s localhost:8811/api/health
curl -s localhost:8811/api/state | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['stats']['all'])"
```

In replay the whole window produces **144 signals at the shipped cut** — 23.7 %
accuracy against a 20.9 % break-even, +19.9 R. If you see that, the app is running the
model that was measured.

## 6. When something goes wrong

| symptom | cause / fix |
|---|---|
| `ModuleNotFoundError: numpy` | you ran system python; use `../.venv/bin/python` |
| `FileNotFoundError: data/BTCUSDT_1m.npy` | the archive is not built — run `tools/build_dataset.py` |
| `[Errno 98] address already in use` | another copy is running: `ss -ltnp \| grep 8811`, kill it, or use `--port 8812` |
| page loads, tape stays empty | normal at first: the recorded cut fires ~0.15 times per symbol per day, so live can take hours. In replay, signals appear within seconds of `--speed 150` |
| `/api/health` shows `error` | in live mode this is the last exchange problem (a symbol with no candles, a timeout). It clears on the next successful poll |
| tape resets when you restart | expected in replay — a simulation starts from 2024-01-01 again. Live rows persist |
| nothing at all on the page | check the terminal: the server prints a traceback on startup if a dependency is missing |

The research scripts are heavier and separate — see `REPORT.md` §8.

## 7. Windows (PowerShell)

```powershell
cd trading_signal_app\signal-lab
python -m venv ..\.venv
..\.venv\Scripts\pip install -r requirements.txt
..\.venv\Scripts\python tools\build_dataset.py
..\.venv\Scripts\python app\server.py --replay --loop --speed 150 --port 8811
```

Everything else is identical: `http://localhost:8811`, same flags.

## 8. What it will not do

* It will not promise 98–100 % accuracy. That was the previous question, and the answer
  — with 480 examples — is in the third tab and in `REPORT.md` §6.
* It will not trade the short side. Long only until the mirrored pipeline passes the
  same three periods.
* It will not guarantee anything. The band the app records at won 24.6 % of 260
  out-of-sample trades against a 20.6 % break-even; the 95 % interval is 19.8–30.2 %.
  Funding costs on multi-day holds are not modelled. Read `REPORT.md` §9 before risking
  money on it.
