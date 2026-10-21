"""30-day win-rate study for the setup strategy.

Runs the strategy on long historical windows (paged downloads, cached) and
reports the hit rate per timeframe. No stop loss, no take profit: a trade opens
on the setup and closes on the strategy's own opposite signal - exactly how the
setup is meant to be read.

The report always says which data it used. ``demo`` means the exchange was not
reachable and the numbers are NOT real market results.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from ..config import SCAN_TIMEFRAMES, TIMEFRAMES
from .backtest import BacktestConfig, run_backtest
from .data.history import get_history
from .strategies import get_strategy

_CACHE: dict[tuple, tuple[float, dict]] = {}
_LOCK = threading.Lock()
_TTL = 600.0


def _caveat(sources: list[str]) -> str:
    if not sources:
        return (
            "NO DATA - the exchange was unreachable and no cached candles exist, so there is "
            "nothing to measure. Connect to the internet and run again."
        )
    if set(sources) <= {"live", "dataset", "cache"}:
        return "Real exchange data (Binance klines)."
    return (
        "DEMO DATA - the exchange was unreachable, so these candles are synthetic. "
        "Win rate here tests the code, not the market. Run the same report with "
        "internet access to get real numbers."
    )


def _run_one(
    symbol: str,
    timeframe: str,
    days: int,
    strategy_id: str,
    params: dict | None,
    allow_demo: bool = True,
) -> dict[str, Any]:
    strategy = get_strategy(strategy_id)
    resolved = strategy.resolve_params(params or {})
    candles, source = get_history(symbol, timeframe, days, allow_demo=allow_demo)
    prepared = strategy.prepare(candles, resolved)
    triggers = strategy.rules(prepared, resolved)

    # Drop the warm-up window so indicator NaNs cannot create phantom trades.
    warm = max(int(strategy.required_bars(resolved)) - 60, 20)
    if len(prepared) > warm + 50:
        prepared = prepared.iloc[warm:].reset_index(drop=True)
        triggers = type(triggers)(
            triggers.long_entry.iloc[warm:].reset_index(drop=True),
            triggers.long_exit.iloc[warm:].reset_index(drop=True),
            triggers.short_entry.iloc[warm:].reset_index(drop=True),
            triggers.short_exit.iloc[warm:].reset_index(drop=True),
        )

    # How many raw (v1) setups did this version's filters remove, and why?
    filters = None
    try:
        filters = strategy.filter_report(prepared, resolved)
    except Exception:
        filters = None

    config = BacktestConfig(initial_equity=10_000, fee_bps=5, slippage_bps=2, cash_fraction=1.0)
    result = run_backtest(
        prepared,
        triggers,
        strategy,
        resolved,
        config,
        timeframe_seconds=int(TIMEFRAMES[timeframe]["seconds"]),
        include_curve=False,
    )
    m = result.metrics
    trades = result.trades

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    avg_win = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0.0

    return {
        "timeframe": timeframe,
        "label": TIMEFRAMES[timeframe]["label"],
        "bars": int(m.get("bars", 0)),
        "source": source,
        "trades": int(m.get("trades", 0)),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else None,
        "profit_factor": m.get("profit_factor"),
        "total_return_pct": m.get("total_return_pct"),
        "buy_hold_return_pct": m.get("buy_hold_return_pct"),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "max_drawdown_pct": m.get("max_drawdown_pct"),
        "exposure_pct": m.get("exposure_pct"),
        "avg_bars_held": m.get("avg_bars_held"),
        "first_bar": m.get("start"),
        "last_bar": m.get("end"),
        "filters": filters,
    }


def report(
    symbol: str = "BTCUSDT",
    days: int = 30,
    timeframes: list[str] | None = None,
    strategy: str = "tdi_shift",
    params: dict | None = None,
    refresh: bool = False,
    allow_demo: bool = True,
) -> dict[str, Any]:
    tfs = [tf for tf in (timeframes or SCAN_TIMEFRAMES) if tf in TIMEFRAMES]
    key = (
        symbol.upper(),
        days,
        tuple(tfs),
        strategy,
        tuple(sorted((params or {}).items())),
        allow_demo,
    )
    now = time.time()
    if not refresh:
        with _LOCK:
            hit = _CACHE.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    rows: list[dict[str, Any]] = []
    for tf in tfs:
        try:
            rows.append(_run_one(symbol, tf, days, strategy, params, allow_demo))
        except Exception as exc:  # one timeframe must not kill the report
            rows.append({"timeframe": tf, "label": TIMEFRAMES[tf]["label"], "error": str(exc)})

    valid = [r for r in rows if not r.get("error") and r.get("trades")]
    total_trades = sum(r["trades"] for r in valid)
    total_wins = sum(r["wins"] for r in valid)
    sources = sorted({r.get("source") for r in valid if r.get("source")})

    payload = {
        "symbol": symbol.upper(),
        "days": days,
        "strategy": strategy,
        "strategy_id": get_strategy(strategy).id,
        "strategy_version": get_strategy(strategy).version,
        "params": get_strategy(strategy).resolve_params(params or {}),
        "generated_at": int(time.time() * 1000),
        "sources": sources,
        "is_real_data": bool(sources) and set(sources) <= {"live", "dataset", "cache"},
        "rows": rows,
        "summary": {
            "timeframes_tested": len(valid),
            "trades": total_trades,
            "wins": total_wins,
            "losses": total_trades - total_wins,
            "win_rate_pct": round(total_wins / total_trades * 100, 2) if total_trades else None,
            "best_timeframe": max(valid, key=lambda r: r["win_rate_pct"])["timeframe"] if valid else None,
            "low_sample": total_trades < 30,
            "raw_setups": sum((r.get("filters") or {}).get("raw", 0) for r in valid),
            "taken_setups": sum((r.get("filters") or {}).get("taken", 0) for r in valid),
        },
        "caveat": _caveat(sources),
    }
    with _LOCK:
        _CACHE[key] = (now, payload)
    return payload


def trades_detail(
    symbol: str = "BTCUSDT",
    days: int = 30,
    timeframe: str = "15m",
    strategy: str = "tdi_shift",
    params: dict | None = None,
    allow_demo: bool = True,
) -> dict[str, Any]:
    """Every trade of one timeframe, with real timestamps.

    This is the audit trail for the win rate: each row contains the entry and
    exit candle times, so any single trade can be checked by eye on a chart.
    """
    from datetime import datetime, timezone

    def iso(ms: int) -> str:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    strategy_obj = get_strategy(strategy)
    resolved = strategy_obj.resolve_params(params or {})
    candles, source = get_history(symbol, timeframe, days, allow_demo=allow_demo)
    prepared = strategy_obj.prepare(candles, resolved)
    triggers = strategy_obj.rules(prepared, resolved)

    warm = max(int(strategy_obj.required_bars(resolved)) - 60, 20)
    if len(prepared) > warm + 50:
        prepared = prepared.iloc[warm:].reset_index(drop=True)
        triggers = type(triggers)(
            triggers.long_entry.iloc[warm:].reset_index(drop=True),
            triggers.long_exit.iloc[warm:].reset_index(drop=True),
            triggers.short_entry.iloc[warm:].reset_index(drop=True),
            triggers.short_exit.iloc[warm:].reset_index(drop=True),
        )

    # How many raw (v1) setups did this version's filters remove, and why?
    filters = None
    try:
        filters = strategy.filter_report(prepared, resolved)
    except Exception:
        filters = None

    config = BacktestConfig(initial_equity=10_000, fee_bps=5, slippage_bps=2, cash_fraction=1.0)
    result = run_backtest(
        prepared,
        triggers,
        strategy_obj,
        resolved,
        config,
        timeframe_seconds=int(TIMEFRAMES[timeframe]["seconds"]),
        include_curve=False,
    )
    trades = [
        {
            "side": t.side,
            "entry_time_utc": iso(t.entry_time),
            "exit_time_utc": iso(t.exit_time),
            "entry_price": round(float(t.entry_price), 2),
            "exit_price": round(float(t.exit_price), 2),
            "bars_held": int(t.bars_held),
            "pnl": round(float(t.pnl), 2),
            "pnl_pct": round(float(t.pnl_pct), 2),
            "exit_reason": t.exit_reason,
        }
        for t in result.trades
    ]
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "days": days,
        "source": source,
        "bars": int(len(prepared)),
        "first_candle_utc": iso(prepared["time"].iloc[0]),
        "last_candle_utc": iso(prepared["time"].iloc[-1]),
        "trades": trades,
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else None,
    }
