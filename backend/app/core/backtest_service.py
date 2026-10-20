"""Glue between market data, strategies and the backtester."""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..config import TIMEFRAMES
from .backtest import BacktestConfig, run_backtest
from .data.providers import get_candles
from .strategies import get_strategy


def _to_binance_interval(timeframe: str) -> str:
    return str(TIMEFRAMES[timeframe]["interval"])


def _resample(df: pd.DataFrame, source_tf: str, target_tf: str) -> pd.DataFrame:
    """Aggregate finer candles into a coarser timeframe when needed."""
    if source_tf == target_tf:
        return df
    src = int(TIMEFRAMES[source_tf]["seconds"])
    dst = int(TIMEFRAMES[target_tf]["seconds"])
    if dst <= src or dst % src != 0:
        raise ValueError(f"Cannot resample {source_tf} -> {target_tf}")
    out = df.set_index(pd.to_datetime(df["time"], unit="ms", utc=True)).resample(
        f"{dst // 60}min", label="left", closed="left"
    ).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    out = out.reset_index().rename(columns={"index": "time"})
    out["time"] = (out["time"].astype("int64") // 10**6).astype("int64")
    return out[["time", "open", "high", "low", "close", "volume"]]


def run(
    symbol: str,
    timeframe: str,
    strategy_id: str,
    params: dict[str, Any] | None = None,
    bars: int = 800,
    initial_equity: float = 10_000.0,
    risk_pct: float = 1.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    leverage: float = 1.0,
) -> dict[str, Any]:
    strategy = get_strategy(strategy_id)
    resolved = strategy.resolve_params(params)
    needed = max(int(strategy.required_bars(resolved)) + 50, 120)

    # Binance returns at most 1000 candles per call. For deeper history on
    # coarse timeframes we fetch the native interval directly; the demo
    # generator has no such limit.
    fetch_tf = timeframe
    candles, source = get_candles(symbol, fetch_tf, limit=min(max(bars, needed), 1000))

    prepared = strategy.prepare(candles, resolved)
    triggers = strategy.rules(prepared, resolved)

    # Warm-up: drop rows where the strategy's indicators are not ready yet.
    atr_col = prepared["atr"] if "atr" in prepared else prepared["close"]
    first_valid = atr_col.first_valid_index()
    if first_valid is not None:
        start = prepared.index.get_loc(first_valid)
        prepared = prepared.iloc[max(0, start - 1):].reset_index(drop=True)
        triggers = type(triggers)(
            triggers.long_entry.iloc[max(0, start - 1):].reset_index(drop=True),
            triggers.long_exit.iloc[max(0, start - 1):].reset_index(drop=True),
            triggers.short_entry.iloc[max(0, start - 1):].reset_index(drop=True),
            triggers.short_exit.iloc[max(0, start - 1):].reset_index(drop=True),
        )

    config = BacktestConfig(
        initial_equity=initial_equity,
        risk_pct=risk_pct,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        leverage=leverage,
    )
    result = run_backtest(
        prepared,
        triggers,
        strategy,
        resolved,
        config,
        timeframe_seconds=int(TIMEFRAMES[timeframe]["seconds"]),
    )
    payload = result.as_dict()
    payload.update(
        {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "source": source,
            "strategy": {"id": strategy.id, "name": strategy.name},
            "params": resolved,
        }
    )
    return payload
