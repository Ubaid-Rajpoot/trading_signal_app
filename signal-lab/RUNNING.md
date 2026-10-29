# Running SignalLab

SignalLab scans 100+ MEXC futures contracts across several timeframes and records
the setups its model expects to reach **+4 ATR before −1 ATR** (reward : risk 1 : 4).
Start it, open the page, and act on the cards in the first tab.

Two clocks, one page:

| | what it does | needs the internet |
|---|---|---|
| **live** (default) | polls MEXC futures every `poll_seconds`, scores every closed bar, keeps a record | yes |
| **replay** | walks the local 9.6 M-bar archive bar by bar at a speed you choose | no |

**All settings live in [`config.json`](config.json).** There is no long command line
any more: put as many symbols as you like in the file. Flags still exist, and they win
over the file.

---

## 0. Prerequisites

| | |
|---|---|
| Python | 3.11 or newer (`python3 --version`) |
| Disk | ~1.2 GB free for replay (700 MB download + 370 MB archive); ~200 MB for live only |
| Internet | always, for live mode; once, for the replay archive |
| OS | anything — Linux, macOS, Windows (PowerShell at the bottom) |

## 1. Install (once)

```bash
cd trading_signal_app/signal-lab
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.txt
```

`requirements.txt` is in `signal-lab/`. The venv lives one level up so this app and
`smc-radar` can share it.

## 2. Live — what you actually want

```bash
../.venv/bin/python app/server.py
```

Then open **http://localhost:8811**. No API key: these are MEXC's public kline endpoints.

On start-up the app

1. asks MEXC for the `auto_top` (120) most liquid `_USDT` contracts — or uses the
   `symbols` list you put in `config.json`;
2. fetches five native candle feeds per contract in parallel (`workers` at a time, with
   a timeout — a dead symbol is skipped and counted, it never hangs the app);
3. builds every configured timeframe from those feeds, including the ones MEXC does not
   serve (`3m` from `1m`, `10m` from `5m`, `45m` from `15m`, `2h`/`3h` from `1h`);
4. **walks back `scan_bars` (40) closed bars on every stream**, so setups that are still
   in play are on the page in the first seconds instead of after an hour;
5. then polls every `poll_seconds` (60) and appends whatever is new.

The header shows a countdown to the next poll; **Scan now** polls immediately.

### Timeframes — and why only three are on

Every timeframe was measured on 2024–2026 across BTCUSDT + ETHUSDT, non-overlapping,
with the 5 bp round trip priced in R for that clock's stop
(`results/timeframes.json`, `research/08_timeframes.py`):

| TF | stop / target | fee | break-even | measured win | **EV after fees** | per day / symbol |
|---|---|---|---|---|---|---|
| 45m | 1 / 4 ATR | 0.066 R | 21.3 % | 24.5 % (192) | **+0.13 R** | 0.19 |
| 1h | 1 / 4 ATR | 0.056 R | 21.1 % | 25.7 % (144) | **+0.21 R** | 0.14 |
| 2h | 1 / 4 ATR | 0.039 R | 20.8 % | 21.8 % (257) | **+0.04 R** | 0.25 |
| 3h | 2 / 8 ATR | 0.016 R | 20.3 % | 20.0 % (100) | −0.02 R | 0.10 |
| 4h | 1 / 4 ATR | 0.027 R | 20.5 % | 19.7 % (117) | −0.05 R | 0.11 |
| 30m | 1 / 4 ATR | 0.077 R | 21.5 % | 19.1 % (298) | −0.12 R | 0.29 |
| 15m | 2 / 8 ATR | 0.056 R | 21.1 % | 19.2 % (480) | −0.10 R | 0.53 |
| 10m | 2 / 8 ATR | 0.073 R | 21.5 % | 19.7 % (142) | −0.09 R | 0.65 |
| 5m | 2 / 8 ATR | 0.100 R | 22.0 % | 19.0 % (742) | −0.15 R | 1.17 |
| 3m | 2 / 8 ATR | 0.191 R | 23.8 % | 20.6 % (412) | −0.16 R | 0.34 |
| 1m | 4 / 16 ATR | 0.192 R | 23.8 % | 17.5 % (2524) | −0.32 R | 1.42 |

The fast clocks produce more signals — that is exactly why they lose: on 1m a 4 ATR
stop is ~0.19 % of price, so the 5 bp round trip costs 0.19 R before the trade even
starts, and no stop/target pair we measured overcame it. They are off by default.
**If you want them anyway**, add them to `timeframes` in `config.json` — models are
shipped for 45m–4h; the 1m–30m clocks have no model (the page marks them `off`).

## 3. How to follow a signal

The panel **Act now** is the only thing you need during the session.

1. **Which card.** Highest `p` first. Skip a card if price has already passed the
   target, or if the entry it fills at is worse than the card's stop distance allows.
2. **Entry.** The trade fills at the **open of the next bar** after the card appears.
   In practice: enter at market as soon as you see a card marked `pending`, or set a
   limit at the last close. Never chase a card older than one bar.
3. **Size.** The card prints quantity and notional for `risk_per_trade_pct` of
   `equity_usd` (default 1 % of $1 000 = $10 of risk):

   `quantity = (equity × risk %) ÷ |entry − stop|`
4. **Stop and target.** Exactly the two numbers on the card. They are set in ATR, off
   the real fill. Do not widen the stop and do not average down.
5. **Invalidation.** If the first bar after entry *closes* beyond the stop, exit even
   if the wick did not touch it. A signal whose bracket is broken is dead.
6. **Concurrency.** At most **3 open trades**. With ~1 win in 4, a run of five losses
   is routine; three concurrent positions keeps that survivable.
7. **When to stop.** Watch the live accuracy on the tape against the card's
   break-even (≈21 %). If the running 95 % confidence interval falls below it, stop
   and re-measure — do not "wait for the market to come back".
8. **Fees.** The app prices the 5 bp round trip into every number it prints. If your
   exchange charges more than 5 bp round trip, raise `fee_bp` so the break-even line
   matches your account.

### What it cannot promise

Nothing makes price move immediately after entry. At 1 : 4 the honest expectation is
**one win in four**: three trades out of four lose the stop, one pays four times the
risk. The measured edge is +0.13 to +0.21 R per trade on a few hundred trades, which
means a losing week is normal. Anyone selling "immediate movement to target" is
selling something this system does not have.

## 4. Settings — `config.json`

| key | default | what it does |
|---|---|---|
| `mode` | `live` | `live` polls the exchange, `replay` walks the archive |
| `symbols` | `[]` | explicit list (`BTCUSDT` or `BTC_USDT`); empty = auto by volume |
| `auto_top` | 120 | how many of the most liquid contracts to scan |
| `timeframes` | `45m,1h,2h` | any of `1m 3m 5m 10m 15m 30m 45m 1h 2h 3h 4h` |
| `scan_bars` | 40 | start-up look-back, in closed bars, per stream |
| `poll_seconds` | 60 | seconds between scans |
| `workers` | 12 | parallel exchange requests |
| `timeout_seconds` | 20 | per-request timeout |
| `fee_bp` | 5.0 | round-trip cost in basis points |
| `cut_override` | `null` | `null` = each timeframe's measured cut; a number forces one |
| `risk_per_trade_pct` / `equity_usd` | 1.0 / 1000 | what the size on each card is computed from |
| `max_candidates` | 40 | how many actionable setups the page shows |
| `db` | `app/data/signals.db` | where the record lives |

Edit the file by hand or in the page (**Settings** panel, saved back to the file).
`mode`, `symbols`, `timeframes`, `workers`, `port` need a restart; `poll_seconds`,
`risk`, `equity`, `scan_bars`, `cut_override` apply immediately.

Flags still work and override the file:

| flag | overrides |
|---|---|
| `--live` / `--replay` | `mode` |
| `--symbols A,B,C` | `symbols` |
| `--tfs 1h,4h` | `timeframes` |
| `--workers N` | `workers` |
| `--poll S` | `poll_seconds` |
| `--cut P` | `cut_override` |
| `--port`, `--host` | `port`, `host` |
| `--config path` | which config file to read |
| `--speed`, `--loop` | replay clock only |

## 5. Replay — the same pipeline, no exchange

```bash
../.venv/bin/python tools/build_dataset.py            # once: ~3 min, 700 MB download
../.venv/bin/python app/server.py --replay --loop --speed 150
```

The replay scores with the **validation model** (trained up to 2023-12-31) so it cannot
be graded on years it has seen. The tape shows how the recorded trades resolved.

The whole 2024 → 2026 window through the app's own engine:

```bash
../.venv/bin/python tools/replay_check.py      # ~100 s, headless
```

```
bars walked      139,808 in 100s
signals recorded 651
resolved         158 won / 491 lost / 0 expired
accuracy         24.3 %  (break-even 21.6 %)
expectancy       +0.137 R per trade, +88.9 R net
by timeframe
    1h  n= 229  win= 27.1 %  EV=+0.277 R
    2h  n= 123  win= 23.8 %  EV=+0.136 R
   45m  n= 299  win= 22.5 %  EV=+0.030 R
```

If the page shows those numbers in replay, it is running the model that was measured.

## 6. What you should see

Five tabs:

* **Act now** — every actionable setup, highest probability first, with entry, stop,
  target, quantity, notional, risk and reward; a per-timeframe table of the measured
  geometry and expectancy; and the settings panel.
* **Signals** — the tape: time, symbol, timeframe, band, p, levels, bars held, result, R.
* **Measured accuracy** — the reward : risk sweep and the probability band table.
* **The 100 % club** — the 480 rules that never lost in sample and what happened to them.
* **Method & limits** — data, splits, and what is not claimed.

Sanity checks without a browser:

```bash
curl -s localhost:8811/api/health
curl -s localhost:8811/api/state  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['stats']['all']);print(len(d['candidates']),'candidates')"
curl -s localhost:8811/api/config | python3 -m json.tool | head -30
curl -s -X POST localhost:8811/api/scan        # poll now
```

## 7. When something goes wrong

| symptom | cause / fix |
|---|---|
| `ModuleNotFoundError: numpy` | system python; use `../.venv/bin/python` |
| `FileNotFoundError: data/BTCUSDT_1m.npy` | replay without the archive — run `tools/build_dataset.py` |
| `[Errno 98] address already in use` | another copy is running: `ss -ltnp \| grep 8811`, or `--port 8812` |
| page loads, no cards | press **Scan now**; check the footer line in **Act now** — it prints the last scan time, the stream count and the last error |
| `/api/health` shows `error` | the last exchange problem (throttled, timeout, a symbol with no candles). It clears on the next successful poll |
| `N of 120 symbols failed` | MEXC throttling; lower `workers` to 6 or raise `poll_seconds` |
| tape resets on restart | replay only — it starts again at 2024-01-01. Live rows persist in SQLite |
| signal count stays 0 in live | expected for minutes at a time on 45m–2h: those clocks produce ~0.2 signals per symbol per day, and 120 symbols yield ~50–60 a day |

## 8. Windows (PowerShell)

```powershell
cd trading_signal_app\signal-lab
python -m venv ..\.venv
..\.venv\Scripts\pip install -r requirements.txt
..\.venv\Scripts\python app\server.py
```

Everything else is identical: `http://localhost:8811`, same `config.json`.

## 9. What it will not do

* It will not produce 98–100 % accuracy — that question was answered in round 14 and is
  on the third tab and in `REPORT.md` §6.
* It will not make price move to target right after entry. It measures the odds and
  sizes the trade.
* It will not trade 1m–30m profitably. Those sweeps are in `results/timeframes.json`;
  the fee beats the edge on every geometry tested.

The research scripts are heavier and separate — see `REPORT.md`.
