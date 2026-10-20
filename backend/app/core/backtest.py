"""Event-driven backtester.

Deliberately conservative and honest:

* Triggers are evaluated on **closed** bars and filled at the **next bar open**.
* Stops and targets are checked intrabar against high/low; if both are touched
  in the same bar the stop wins (worst-case assumption).
* Fees, slippage and optional leverage are modelled on both sides.
* Only one position at a time, sized by fixed fractional risk.

The output includes every trade and the equity curve so results can be audited
rather than trusted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .strategies import Strategy


@dataclass
class BacktestConfig:
    initial_equity: float = 10_000.0
    risk_pct: float = 1.0          # % of equity risked per trade (stop distance)
    fee_bps: float = 5.0           # per side, 5 bps = 0.05%
    slippage_bps: float = 2.0      # per side
    leverage: float = 1.0          # max notional / equity
    bars_per_year: float | None = None


@dataclass
class Trade:
    side: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    qty: float
    stop: float
    target: float | None
    pnl: float
    pnl_pct: float
    r_multiple: float
    bars_held: int
    exit_reason: str
    equity_after: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "entry_time": int(self.entry_time),
            "exit_time": int(self.exit_time),
            "entry_price": round(self.entry_price, 6),
            "exit_price": round(self.exit_price, 6),
            "qty": round(self.qty, 8),
            "stop": round(self.stop, 6),
            "target": round(self.target, 6) if self.target is not None else None,
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 3),
            "r_multiple": round(self.r_multiple, 3),
            "bars_held": int(self.bars_held),
            "exit_reason": self.exit_reason,
            "equity_after": round(self.equity_after, 2),
        }


@dataclass
class OpenPosition:
    side: str
    entry_index: int
    entry_time: int
    entry_price: float
    qty: float
    stop: float
    target: float | None
    risk_per_unit: float
    equity_before: float


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[dict[str, float]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "trades": [t.as_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
        }


def _bars_per_year(timeframe_seconds: int) -> float:
    return 365 * 24 * 3600 / max(int(timeframe_seconds), 1)


def _max_drawdown(equity: np.ndarray) -> float:
    if equity.size == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    return float(dd.min() * 100)


def _sharpe(returns: np.ndarray, periods: float) -> float:
    if returns.size < 2:
        return 0.0
    std = returns.std(ddof=1)
    if std == 0 or not np.isfinite(std):
        return 0.0
    return float(returns.mean() / std * math.sqrt(periods))


def _sortino(returns: np.ndarray, periods: float) -> float:
    if returns.size < 2:
        return 0.0
    downside = returns[returns < 0]
    if downside.size == 0:
        return 0.0
    dd = downside.std(ddof=1)
    if dd == 0 or not np.isfinite(dd):
        return 0.0
    return float(returns.mean() / dd * math.sqrt(periods))


def run_backtest(
    prepared: pd.DataFrame,
    triggers,
    strategy: Strategy,
    params: dict[str, Any],
    config: BacktestConfig | None = None,
    timeframe_seconds: int = 3600,
) -> BacktestResult:
    cfg = config or BacktestConfig()
    result = BacktestResult()
    if prepared.empty:
        result.metrics = {"trades": 0}
        return result

    n = len(prepared)
    t = prepared["time"].to_numpy(dtype=np.int64)
    o = prepared["open"].to_numpy(dtype=float)
    h = prepared["high"].to_numpy(dtype=float)
    lo = prepared["low"].to_numpy(dtype=float)
    c = prepared["close"].to_numpy(dtype=float)
    atr = prepared["atr"].to_numpy(dtype=float) if "atr" in prepared else np.full(n, np.nan)

    long_entry = triggers.long_entry.to_numpy()
    long_exit = triggers.long_exit.to_numpy()
    short_entry = triggers.short_entry.to_numpy()
    short_exit = triggers.short_exit.to_numpy()

    fee = cfg.fee_bps / 10_000.0
    slip = cfg.slippage_bps / 10_000.0
    risk_frac = max(cfg.risk_pct, 0.01) / 100.0

    equity = float(cfg.initial_equity)
    equity_series = np.full(n, np.nan)
    position: OpenPosition | None = None
    pending: str | None = None  # 'LONG' | 'SHORT' | 'EXIT' queued from previous close
    bars_in_market = 0

    def close_position(idx: int, raw_price: float, reason: str) -> None:
        nonlocal equity, position
        assert position is not None
        fill = raw_price * (1 - slip) if position.side == "LONG" else raw_price * (1 + slip)
        pnl = (fill - position.entry_price) * position.qty * (1 if position.side == "LONG" else -1)
        pnl -= abs(fill * position.qty) * fee
        equity += pnl
        risk_amount = abs(position.entry_price - position.stop) * position.qty
        r_mult = pnl / risk_amount if risk_amount > 0 else 0.0
        result.trades.append(
            Trade(
                side=position.side,
                entry_time=int(position.entry_time),
                exit_time=int(t[idx]),
                entry_price=position.entry_price,
                exit_price=fill,
                qty=position.qty,
                stop=position.stop,
                target=position.target,
                pnl=pnl,
                pnl_pct=pnl / max(position.equity_before, 1e-9) * 100,
                r_multiple=r_mult,
                bars_held=idx - position.entry_index,
                exit_reason=reason,
                equity_after=equity,
            )
        )
        position = None

    for i in range(1, n):
        # ---------------------------------------------------- 1) fill orders
        if pending and position is None and i < n:
            price = o[i] * (1 + slip) if pending == "LONG" else o[i] * (1 - slip)
            a = atr[i - 1] if np.isfinite(atr[i - 1]) else c[i - 1] * 0.005
            mult = float(params.get("atr_mult", 2.0))
            stop_distance = a * mult
            if stop_distance <= 0 or price <= 0:
                pending = None
            else:
                risk_amount = equity * risk_frac
                qty = risk_amount / stop_distance
                max_notional = equity * max(cfg.leverage, 1.0)
                qty = min(qty, max_notional / price)
                if qty <= 0:
                    pending = None
                else:
                    side = pending
                    stop = price - stop_distance if side == "LONG" else price + stop_distance
                    rr = float(params.get("target_r", 2.0))
                    target = price + rr * stop_distance if side == "LONG" else price - rr * stop_distance
                    equity_before = equity
                    equity -= abs(price * qty) * fee
                    position = OpenPosition(
                        side=side,
                        entry_index=i,
                        entry_time=int(t[i]),
                        entry_price=price,
                        qty=qty,
                        stop=stop,
                        target=target,
                        risk_per_unit=stop_distance,
                        equity_before=equity_before,
                    )
                    pending = None

        # ------------------------------------------- 2) manage open position
        if position is not None:
            bars_in_market += 1
            side = position.side
            hit_stop = lo[i] <= position.stop if side == "LONG" else h[i] >= position.stop
            hit_target = position.target is not None and (
                h[i] >= position.target if side == "LONG" else lo[i] <= position.target
            )
            if hit_stop:
                close_position(i, position.stop, "stop")
            elif hit_target:
                assert position.target is not None
                close_position(i, position.target, "target")
            elif (side == "LONG" and long_exit[i]) or (side == "SHORT" and short_exit[i]):
                close_position(i, c[i], "signal")

        # -------------------------------------------------- 3) new signals
        if position is None and i < n - 1:
            can_long = bool(long_entry[i])
            can_short = bool(short_entry[i])
            if can_long and not can_short:
                pending = "LONG"
            elif can_short and not can_long:
                pending = "SHORT"

        equity_series[i] = equity

    # Close any open position at the last close so metrics are complete.
    if position is not None:
        close_position(n - 1, c[-1], "end_of_data")
        equity_series[n - 1] = equity

    equity_series[0] = cfg.initial_equity
    valid = ~np.isnan(equity_series)

    result.equity_curve = [
        {"time": int(t[i]), "equity": round(float(equity_series[i]), 2), "price": round(float(c[i]), 6)}
        for i in range(n)
        if valid[i]
    ]

    # ------------------------------------------------------------- metrics
    trades = result.trades
    final_equity = equity
    total_return = (final_equity / cfg.initial_equity - 1) * 100 if cfg.initial_equity else 0.0
    periods = cfg.bars_per_year or _bars_per_year(timeframe_seconds)
    span_bars = max(n - 1, 1)
    years = span_bars / periods if periods else 0
    cagr = ((final_equity / cfg.initial_equity) ** (1 / years) - 1) * 100 if years > 0.01 and final_equity > 0 else 0.0

    rets = pd.Series(equity_series[valid]).pct_change().dropna().to_numpy()
    wins = [tr for tr in trades if tr.pnl > 0]
    losses = [tr for tr in trades if tr.pnl <= 0]
    gross_win = sum(tr.pnl for tr in wins)
    gross_loss = abs(sum(tr.pnl for tr in losses))
    r_values = np.array([tr.r_multiple for tr in trades]) if trades else np.array([])

    # Buy & hold benchmark over the same window.
    bh_return = (c[-1] / c[0] - 1) * 100 if len(c) else 0.0

    def f(x: float, nd: int = 2) -> float:
        return round(float(x), nd)

    result.metrics = {
        "start": int(t[0]),
        "end": int(t[-1]),
        "bars": int(n),
        "initial_equity": round(cfg.initial_equity, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return, 2),
        "buy_hold_return_pct": round(bh_return, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(_max_drawdown(pd.Series(equity_series[valid]).to_numpy()), 2),
        "sharpe": round(_sharpe(rets, periods), 2),
        "sortino": round(_sortino(rets, periods), 2),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not wins else 99.0),
        "expectancy_r": round(float(r_values.mean()), 3) if r_values.size else 0.0,
        "avg_win_pct": round(float(np.mean([tr.pnl_pct for tr in wins])), 2) if wins else 0.0,
        "avg_loss_pct": round(float(np.mean([tr.pnl_pct for tr in losses])), 2) if losses else 0.0,
        "best_trade_pct": round(max((tr.pnl_pct for tr in trades), default=0.0), 2),
        "worst_trade_pct": round(min((tr.pnl_pct for tr in trades), default=0.0), 2),
        "avg_bars_held": round(float(np.mean([tr.bars_held for tr in trades])), 1) if trades else 0.0,
        "exposure_pct": round(bars_in_market / span_bars * 100, 1) if span_bars else 0.0,
        "config": {
            "initial_equity": cfg.initial_equity,
            "risk_pct": cfg.risk_pct,
            "fee_bps": cfg.fee_bps,
            "slippage_bps": cfg.slippage_bps,
            "leverage": cfg.leverage,
        },
    }
    return result
