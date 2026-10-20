"""The most important property of the whole app: no future data leaks into signals.

If a strategy peeked at bar i+1 while deciding bar i, live signals would look
brilliant in backtest and fail in production. These tests truncate the future
and assert the past is unchanged.
"""

import pandas as pd
import pytest

from app.core.data.synthetic import generate_candles
from app.core.strategies import _REGISTRY


@pytest.mark.parametrize("strategy_id", list(_REGISTRY))
def test_prepared_indicators_are_causal(strategy_id):
    strategy = _REGISTRY[strategy_id]()
    params = strategy.resolve_params(None)
    full = generate_candles("ETHUSDT", "1h", 400, now=1_800_000_000)
    truncated = full.iloc[:-60].reset_index(drop=True)

    full_prepared = strategy.prepare(full, params)
    trunc_prepared = strategy.prepare(truncated, params)

    shared = trunc_prepared.index
    for column in strategy.plot_lines:
        col = column["column"]
        a = full_prepared[col].iloc[shared]
        b = trunc_prepared[col]
        pd.testing.assert_series_equal(a.reset_index(drop=True), b.reset_index(drop=True), check_names=False)


@pytest.mark.parametrize("strategy_id", list(_REGISTRY))
def test_triggers_do_not_change_when_future_is_appended(strategy_id):
    strategy = _REGISTRY[strategy_id]()
    params = strategy.resolve_params(None)
    full = generate_candles("SOLUSDT", "4h", 400, now=1_800_000_000)

    head = full.iloc[:300].reset_index(drop=True)
    a = strategy.rules(strategy.prepare(head, params), params)
    b = strategy.rules(strategy.prepare(full, params), params)

    for field in ("long_entry", "long_exit", "short_entry", "short_exit"):
        assert getattr(a, field).tolist() == getattr(b, field).iloc[:300].tolist(), field
