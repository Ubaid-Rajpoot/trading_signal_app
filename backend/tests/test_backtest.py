import pytest

from app.core import backtest_service
from app.core.backtest import BacktestConfig, run_backtest
from app.core.strategies import get_strategy


def test_backtest_is_deterministic():
    a = backtest_service.run("BTCUSDT", "1h", "donchian", bars=600)
    b = backtest_service.run("BTCUSDT", "1h", "donchian", bars=600)
    assert a["metrics"]["total_return_pct"] == b["metrics"]["total_return_pct"]
    assert len(a["trades"]) == len(b["trades"])


def test_trades_are_chronological_and_sized_by_risk():
    result = backtest_service.run("BTCUSDT", "1h", "donchian", bars=800)
    trades = result["trades"]
    assert trades, "expected at least one trade on this window"
    for earlier, later in zip(trades, trades[1:]):
        assert earlier["entry_time"] <= later["entry_time"]
    # Risk per trade is 1% of equity: realised loss should be close to that.
    losses = [t["pnl_pct"] for t in trades if t["pnl"] < 0]
    assert losses, "expected losing trades in the sample"
    assert all(loss > -3.5 for loss in losses)


def test_costs_reduce_returns(candles):
    strategy = get_strategy("donchian")
    params = strategy.resolve_params(None)
    prepared = strategy.prepare(candles, params)
    triggers = strategy.rules(prepared, params)

    free = run_backtest(prepared, triggers, strategy, params, BacktestConfig(fee_bps=0, slippage_bps=0))
    costly = run_backtest(prepared, triggers, strategy, params, BacktestConfig(fee_bps=40, slippage_bps=20))
    assert costly.metrics["total_return_pct"] < free.metrics["total_return_pct"]


def test_stop_loss_caps_single_trade_loss(candles):
    strategy = get_strategy("donchian")
    params = strategy.resolve_params(None)
    prepared = strategy.prepare(candles, params)
    triggers = strategy.rules(prepared, params)
    result = run_backtest(prepared, triggers, strategy, params, BacktestConfig(risk_pct=1.0))
    for trade in result.trades:
        # 1% risk + fees/slippage; a gap can cost a little more but never 10x.
        assert trade.pnl_pct > -8, trade


def test_positions_never_overlap(candles):
    strategy = get_strategy("ema_trend")
    params = strategy.resolve_params(None)
    prepared = strategy.prepare(candles, params)
    triggers = strategy.rules(prepared, params)
    result = run_backtest(prepared, triggers, strategy, params)
    for earlier, later in zip(result.trades, result.trades[1:]):
        assert earlier.exit_time <= later.entry_time


@pytest.mark.parametrize("strategy_id", ["donchian", "ema_trend", "custom"])
def test_every_strategy_produces_valid_metrics(strategy_id):
    result = backtest_service.run("ETHUSDT", "4h", strategy_id, bars=700)
    m = result["metrics"]
    for key in ("total_return_pct", "max_drawdown_pct", "win_rate_pct", "trades", "sharpe", "profit_factor"):
        assert key in m
    assert m["max_drawdown_pct"] <= 0
    assert 0 <= m["win_rate_pct"] <= 100
    if m["trades"]:
        assert len(result["equity_curve"]) > 100
