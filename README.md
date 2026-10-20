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
| **Live dashboard** | Watchlist scanner, candlestick chart with strategy overlays and trigger markers, rule-by-rule breakdown, ATR-based trade plan |
| **Signal engine** | Current state (flat / long / short), fresh-trigger detection, confidence score, setup readiness, "last trigger N bars ago" |
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

```
backend/
  app/
    config.py               constants, timeframes, default symbols
    main.py                 FastAPI app + static hosting
    api/routes.py           HTTP endpoints
    core/
      indicators.py         pure-pandas TA library
      data/providers.py     Binance → cache → demo provider chain
      data/synthetic.py     deterministic demo candles
      strategies/           ← YOUR STRATEGY LIVES HERE
        base.py             Strategy/TriggerSet/Targets framework
        donchian.py         default breakout strategy
        ema_trend.py        EMA crossover strategy
        custom.py           editable template
      signals.py            signal engine (state, confidence, plan, chart payload)
      backtest.py           event-driven backtester
      backtest_service.py   data + strategy + backtest glue
      store.py              SQLite settings / signals / notifications
      alerts/               notifier + background scanner
    tests/                  30 tests incl. no-look-ahead checks
frontend/
  src/
    App.tsx                 shell, polling, tab routing
    components/             SignalPanel, CandleChart, BacktestView, SettingsView…
    styles.css              dark terminal theme (no UI framework)
scripts/dev.sh · scripts/build.sh · Makefile
```

---

## Notes & limitations

* **Demo data.** If Binance is unreachable (offline, geo-blocked), the app serves a
  deterministic synthetic feed and clearly labels it. Every feature works, but the
  numbers are not the real market.
* Signals are computed on candle close. Intraday alerts therefore arrive when a bar
  closes, not tick-by-tick.
* Backtests use up to 1,000 candles per run (the exchange's per-request limit).
  Deeper history needs paging — a good next addition.
* This is a research and automation tool, **not financial advice**. Paper-trade a
  strategy before risking capital.

## Roadmap ideas

- Paged historical download for multi-year backtests
- Walk-forward / parameter optimisation with the same strategy contract
- Portfolio-level sizing across correlated symbols
- Paper-trading ledger with realised P&L tracking
- More alert channels (Discord, email)
