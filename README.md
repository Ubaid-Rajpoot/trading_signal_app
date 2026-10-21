# SignalForge — Trading Signal App

A self-hosted crypto signal platform built around **your** strategy. It pulls market
data, evaluates your rules on every closed candle, shows an actionable plan
(entry / stop / targets), backtests the same rules honestly, and pushes alerts to
Telegram or any webhook.

The strategy engine is deliberately pluggable: your edge lives in one small Python
file, and everything else — charting, scanning, backtesting, alerting — reuses it.

```
 Binance candles ──▶ Indicators ──▶ YOUR STRATEGY ──▶ Signal + plan ──▶ Dashboard
                                        │                              ├─ Alerts (Telegram/webhook)
                                        └──────────────▶ Backtester ────┘
```

---

## What you get

| Area | Details |
|---|---|
| **Setup radar** | The main screen: a symbol × timeframe grid (1m / 3m / 5m / 15m / 30m) showing whether a setup is **triggered / armed / forming / absent** on each instrument right now |
| **Chart view** | Candles with swing levels, the TDI sub-pane (purple RSI, base line, bands, HVB markers) and a rule-by-rule explanation of the current state |
| **Win-rate study** | 30 days of history across the five scalping timeframes, with hit rate, profit factor, returns and drawdown per timeframe |
| **Signal engine** | Setup state (triggered / armed / forming / blocked / idle), which swing level still has to break, how far price is from it, and the TDI values behind it |
| **Structure filter** | v2 only trades setups that form inside external or internal structure, with the external trend, outside ranges, and away from liquidity build-ups |
| **Backtester** | Next-bar fills, intrabar stop/target checks, fees + slippage, fixed-fractional risk, full trade log, equity curve vs buy & hold, Sharpe/Sortino/PF/expectancy |
| **Alerts** | Background scanner persists each actionable signal once per bar, delivers to Telegram and/or a JSON webhook, logs every attempt |
| **Persistence** | SQLite for settings, signal history and alert log — no external services |
| **Data** | Binance public REST (no API key), disk cache, and a deterministic demo feed so the app is fully usable offline |

Four strategies ship in the box:

1. **TDI + Shift (HVB)** (default) — the TDIGM system: the purple RSI line breaks the
   Market Base Line *with speed* on the same candle that price breaks a minor swing
   (SHIFT) or a major swing (MS). Full spec in [`docs/strategy-spec.md`](docs/strategy-spec.md).
2. **Donchian Breakout** — turtle-style trend following with an EMA trend filter and ADX chop filter.
3. **EMA Trend Cross** — fast/slow EMA crossover with an RSI over-extension filter.
4. **My Strategy (template)** — a documented, editable file meant to be replaced with your own rules.

`tdi_shift` also renders its own **TDI sub-pane** under the price chart — purple RSI
line, Market Base Line, volatility bands and yellow HVB markers — with the swing
levels drawn on price.

---

## Quick start

```bash
# one-shot: installs backend + frontend deps and runs both servers
./scripts/dev.sh
```

Then open:

* **http://localhost:5173** — dashboard (Vite dev server, proxies `/api` to the backend)
* **http://localhost:8000/docs** — interactive API docs

Manual setup if you prefer:

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt
cd frontend && npm install && cd ..

# terminal 1
cd backend && ../.venv/bin/uvicorn app.main:app --reload --port 8000

# terminal 2
cd frontend && npm run dev
```

Single-process deployment (API + UI on one port):

```bash
./scripts/build.sh          # builds frontend into backend/static
cd backend && ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Tests:

```bash
./.venv/bin/python -m pytest backend/tests -q      # 30 tests
```

---

## Setup radar & win-rate study

**Setup radar** is where you look first. Each cell answers one question: is the
setup building on this instrument and timeframe right now?

| Status | Meaning |
|---|---|
| **SETUP** | HVB crossed the base line **and** the swing broke, on the same candle |
| **ARMED** | The HVB printed, price still has to break a swing high/low |
| **FORMING** | Price is near a swing level and/or the purple line is heading for the base line |
| **BLOCKED** | A setup is forming but the v2 rules forbid it — the market is ranging, the setup is against the trend, there is no internal structure, or liquidity is stacked in front of it |
| **—** | Nothing on this instrument right now |

Click any cell to open that chart. The radar rescans every 60 seconds.

**Win rate** runs the strategy over the last N days (default 30) on
1m / 3m / 5m / 15m / 30m. There is **no stop loss and no take profit** — a trade
opens on the setup and closes on the opposite TDI cross, which is how the setup
is meant to be read.

How it is computed, in five steps (all in this repo, no external dataset):

1. **Get candles** — `backend/app/core/data/history.py::get_history()`.
   Cache first, then live Binance `/api/v3/klines` (paged 1000 at a time),
   then a labelled demo feed.
2. **Prepare** — `strategy.prepare()` adds RSI, the TDI lines and the swings.
3. **Detect** — `strategy.rules()` returns buy/sell/sell-exit booleans per candle.
   Every boolean only looks at candles up to that one (checked by
   `tests/test_no_lookahead.py`).
4. **Simulate** — `backtest.run_backtest()` walks the candles once, in order,
   entering on a signal and leaving on the exit signal.
5. **Count** — `wins = trades with pnl > 0`, `win_rate = wins / trades`.

### Checking the data for yourself

```bash
# host-by-host data trail: did the exchange answer? is there a cache?
python scripts/winrate.py --symbol BTCUSDT --days 30 --explain

# list every trade with entry/exit timestamps, to spot-check on a chart
python scripts/winrate.py --symbol BTCUSDT --days 30 --trades

# refuse to run without real exchange data (exit code 1, never prints demo numbers)
python scripts/winrate.py --symbol BTCUSDT --days 30 --real-only
```

The same trail is available over HTTP:

```bash
curl "http://localhost:8000/api/data-source?symbol=BTCUSDT&timeframe=15m&days=30"
```

It returns each host with `ok` true/false and the reason, the cache file and its
recorded source, and what `get_history` would actually use.

```bash
# from the command line, real Binance data (needs internet)
python scripts/winrate.py --symbol BTCUSDT --days 30

# write a markdown report as well
python scripts/winrate.py --symbol BTCUSDT --days 30 --out docs/reports/winrate-btcusdt.md

# try different thresholds
python scripts/winrate.py --symbol ETHUSDT --params '{"hvb_step": 2.0, "confluence_window": 1}'
```

Downloads are paged (a 30-day 1m window is 43,200 candles = 44 requests) and
cached under `backend/data/cache/history/`, so the second run is instant and
works offline.

> **Data source matters.** Every report states where its candles came from
> (`live`, `cache` or `demo`). If the exchange is unreachable the app falls back
> to a deterministic demo feed; the report then says **DEMO DATA** in red,
> because those numbers say nothing about the real market.

## Strategy versions

The trader's system ships in versions, and every version stays available in the
strategy dropdown:

| Version | id | Status |
|---|---|---|
| v1.0 | `tdi_shift_v1` | frozen — HVB + same-candle structure break, no filters |
| v2.0 | `tdi_shift_v2` | **default** — v1 + structure / trend / range / liquidity filters |

`tdi_shift` resolves to the latest version, so old links keep working. v1 is
frozen so its results stay reproducible; the commit is tagged `strategy-v1`.
Details, thresholds and how to compare them:
[`docs/strategy-versions.md`](docs/strategy-versions.md).

## How the signal engine works

For each symbol and timeframe:

1. **Data** — `get_candles()` fetches Binance klines, drops the still-forming candle, and falls back to disk cache then to the deterministic demo feed. The API labels the source (`live` / `cache` / `demo`) and the UI badges it.
2. **Prepare** — the strategy adds indicator columns to the OHLCV frame.
3. **Rules** — the strategy returns four boolean series: `long_entry`, `long_exit`, `short_entry`, `short_exit`.
4. **State** — the engine replays those triggers to know whether a position is currently open, since when, and at what price.
5. **Signal** — the last closed bar decides the action:

   | Situation | Action |
   |---|---|
   | Entry trigger on the latest bar | `BUY` / `SELL` |
   | Position open, no exit yet | `HOLD LONG` / `HOLD SHORT` |
   | Exit trigger while flat | `EXIT` |
   | Nothing | `WAIT` (with setup readiness 0–100) |

6. **Plan** — entry at the close, stop at `ATR × multiple`, targets at R-multiples. When there is no live setup the same levels are shown as *illustrative* from the current bias.

Everything is computed on **closed bars only**, so what the dashboard shows is what the backtester would have seen at that moment.

---

## Writing your own strategy

This is the part that matters. Everything else already works.

### 1. Open the template

`backend/app/core/strategies/custom.py` is a complete, working strategy. Replace
`prepare()` and `rules()` — nothing else in the app changes.

```python
class CustomStrategy(Strategy):
    id = "custom"                       # stable id used by the API/UI
    name = "My Strategy"
    description = "..."
    timeframe_hint = "1h"
    allows_short = False

    params = [                          # rendered as controls in the UI
        ParamSpec("fast_len", "Fast EMA", "int", 20, 5, 100, 1),
        ParamSpec("rsi_buy", "RSI buy zone max", "float", 55.0, 30, 80, 1),
    ]

    def prepare(self, df, params):
        data = df.copy()
        data["fast_ema"] = ta.ema(data["close"], int(params["fast_len"]))
        data["rsi"] = ta.rsi(data["close"], 14)
        data["atr"] = ta.atr(data, 14)   # used by the stop/target model
        return data

    def rules(self, data, params):
        long_entry = (
            (data["close"] > data["fast_ema"])
            & ta.crossed_above(data["rsi"], 40)
        )
        long_exit = data["close"] < data["fast_ema"]
        return TriggerSet(long_entry, long_exit, None, None)   # None = never
```

Rules of the framework:

* `prepare()` gets `time` (ms), `open`, `high`, `low`, `close`, `volume`; return the same frame plus your columns.
* `rules()` must return four boolean `pd.Series` aligned to the same index. Pass `None` for a side you do not trade.
* **Never** use the current bar's close to decide the current bar's signal — shift levels with `.shift(1)`. The test suite enforces this (`test_no_lookahead.py`).

### 2. Register it

In `backend/app/core/strategies/__init__.py`:

```python
from .my_strategy import MyStrategy
_REGISTRY = { ..., MyStrategy.id: MyStrategy }
```

Restart the API. Your strategy appears in the dashboard selector, the backtester,
and the scanner — no frontend changes needed.

### 3. Optional niceties

* `checks()` — the rule-by-rule list shown on the signal card.
* `plot_lines` — any prepared column you want drawn over the chart.
* `targets()` — override the default ATR stop / R-ladder with your own exit model.

### Available indicators (`backend/app/core/indicators.py`)

`SMA`, `EMA`, Wilder smoothing, `RSI`, `ATR`, `MACD`, `Bollinger`, `Stochastic`, `ADX`,
`crossed_above`, `crossed_below`, `crossed_above_level`, `crossed_below_level`,
`rolling_high`, `rolling_low`, `pct_change`. They are plain pandas and vectorised,
so evaluating a 1,000-bar history is instant.

---

## Backtest assumptions (why the numbers are believable)

* Triggers are evaluated on a closed bar; the fill happens at the **next bar's open**.
* Stops and targets are checked intrabar against high/low. If both are touched in the same bar, **the stop wins**.
* Fees and slippage are charged per side (defaults: 5 bps + 2 bps).
* Position size = `equity × risk% / stop distance`, capped by `equity × max leverage`.
* One position at a time; the last position is closed at the final close.

Defaults are conservative on purpose. If a strategy only works with zero fees, it
does not work.

---

## Configuration

Settings live in SQLite (`backend/data/signals.db`) and are editable in the UI:

| Setting | Meaning |
|---|---|
| `watchlist` | Up to 25 symbols scanned by the dashboard and the alert scanner |
| `strategy` / `timeframe` | Defaults for the dashboard and scanner |
| `refresh_seconds` | Scanner cadence (min 20s) and dashboard polling |
| `alerts.enabled` / `min_confidence` | Only actionable signals above this confidence alert |
| `alerts.telegram_bot_token` / `telegram_chat_id` | Telegram delivery (token is never returned by the API) |
| `alerts.webhook_url` | JSON POST target for any other system |

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `TSA_DATA_DIR` | `backend/data` | Where the DB and candle cache live |
| `TSA_HTTP_TIMEOUT` | `8` | Timeout for exchange/webhook calls |
| `BACKEND_URL` | `http://127.0.0.1:8000` | Vite dev proxy target |

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness, data provider mode, signal counts |
| GET | `/api/meta` | Strategies (with parameter schema), timeframes, symbols, settings |
| GET | `/api/signal` | Full signal for one symbol (`symbol`, `timeframe`, `strategy`, `params` JSON) |
| GET | `/api/scan` | Watchlist scan, ranked by actionability |
| GET | `/api/radar` | Symbol × timeframe setup grid (status, side, level to break, distance) |
| GET | `/api/winrate` | 30-day win-rate study per timeframe (`days`, `timeframes`, `refresh`) |
| GET | `/api/data-source` | Where the candles come from: host-by-host check, cache state, resolved source |
| POST | `/api/backtest` | Run a backtest |
| GET | `/api/signals` | Stored signal history |
| GET/PUT | `/api/settings` | Read/update settings |
| GET | `/api/alerts` | Alert delivery log + scanner status |
| POST | `/api/alerts/test` | Send a test alert (`channel=telegram|webhook`) |

Example:

```bash
curl "http://localhost:8000/api/signal?symbol=ETHUSDT&timeframe=4h&strategy=donchian"
curl -X POST http://localhost:8000/api/backtest \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"BTCUSDT","timeframe":"1h","strategy":"donchian","bars":800,"risk_pct":1}'
```

---

## Project layout

Everything lives in one place, nothing is loose in the root.

```
README.md · Makefile · requirements*.txt · .gitignore
docs/
  README.md                 documentation index
  strategy-spec.md          the strategy, rule by rule
  reference/
    README.md               what each reference file shows
    charts/*.png            the trader's own charts (TDI, HVB, SHIFT)
    book/*.pdf              the ICT/SMC book the concepts come from
  reports/                  saved win-rate studies
scripts/
  dev.sh                    run API + UI together
  build.sh                  build the UI into backend/static
  winrate.py                30-day win-rate study from the CLI
backend/
  app/
    config.py               timeframes (1m..1d), symbols, paths
    main.py                 FastAPI app + static hosting
    api/routes.py           HTTP endpoints
    utils.py                JSON sanitising
    core/
      indicators.py         pure-pandas TA including TDI
      swings.py             swing pivots, SHIFT and MS detection
      market_structure.py   trend / range / structure / liquidity gates
      signals.py            signal + setup state
      winrate.py            win-rate study
      backtest.py           event-driven backtester (stops optional)
      backtest_service.py   data + strategy + backtest glue
      store.py              SQLite settings / signals / notifications
      utils_paths.py        cache paths
      strategies/
        base.py             Strategy / TriggerSet / Targets framework
        tdi_shift_v1.py     the trader's system, v1 (frozen)
        tdi_shift.py        the trader's system, v2 (default)
        donchian.py · ema_trend.py · custom.py
      data/
        history.py          paged downloads + CSV cache
        providers.py        live -> cache -> demo for the dashboard
        synthetic.py        deterministic demo feed
      alerts/               notifier + background scanner
    data/                   runtime DB + cache (gitignored)
    tests/                  58 tests incl. no-look-ahead checks
frontend/
  src/
    App.tsx                 shell, polling, tab routing
    components/             SetupRadar, WinRateView, SignalPanel, CandleChart,
                            OscillatorChart, BacktestView, SettingsView ...
    styles.css              dark terminal theme (no UI framework)
```

## Notes & limitations

* **Demo data.** If Binance is unreachable (offline, geo-blocked), the app serves a
  deterministic synthetic feed and clearly labels it. Every feature works, but the
  numbers are not the real market.
* Signals are computed on candle close. Intraday alerts therefore arrive when a bar
  closes, not tick-by-tick.
* The win-rate study uses paged downloads (30 days of 1m = 43,200 candles), so
  long windows take a few seconds the first time and are cached afterwards.
* This is a research and automation tool, **not financial advice**. Paper-trade a
  strategy before risking capital.

## Roadmap ideas

- Paged historical download for multi-year backtests
- Walk-forward / parameter optimisation with the same strategy contract
- Portfolio-level sizing across correlated symbols
- Paper-trading ledger with realised P&L tracking
- More alert channels (Discord, email)
