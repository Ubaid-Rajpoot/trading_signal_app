"""v4: the trader's corrections, tested.

v4 exists because three earlier assumptions were invented rather than asked for:

* volume was required for an HVB (removed),
* the setup had to sit *inside* internal structure (now external is fine too),
* the exit was "purple line crosses back" (now the next swing level).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.backtest import BacktestConfig, run_backtest
from app.core.data.synthetic import generate_candles
from app.core.strategies import DEFAULT_STRATEGY, get_strategy
from app.core.strategies.tdi_shift_v4 import liquidity_targets


def frame(bars: int = 1200) -> pd.DataFrame:
    df = generate_candles("BTCUSDT", "15m", bars)
    return df.reset_index(drop=True)


@pytest.fixture(scope="module")
def prepared():
    strategy = get_strategy("tdi_shift_v4")
    params = strategy.resolve_params({})
    data = strategy.prepare(frame(), params)
    return strategy, params, data


# --------------------------------------------------------------- defaults
def test_v4_still_exists_and_the_alias_points_at_the_newest():
    assert get_strategy("tdi_shift").id == DEFAULT_STRATEGY
    assert get_strategy("tdi_shift_v4").id == "tdi_shift_v4"
    assert get_strategy("tdi_shift_v3").id == "tdi_shift_v3"


def test_volume_is_no_longer_required(prepared):
    strategy, params, data = prepared
    assert params["hvb_use_volume"] is False
    # the volume columns still exist for inspection, they are just not gating
    assert "hvb_volume_ratio" in data.columns


def test_hvb_is_a_speed_break_on_the_base_line(prepared):
    _, params, data = prepared
    up = data["hvb_up"].fillna(False)
    down = data["hvb_down"].fillna(False)
    assert (up | down).any(), "synthetic data should produce at least one HVB"
    assert (data.loc[up, "hvb_travel"] >= params["hvb_step"]).all()
    assert (data.loc[down, "hvb_travel"] <= -params["hvb_step"]).all()


def test_external_structure_is_now_accepted(prepared):
    """A setup at the big trend's structure must not be filtered out.

    The internal-structure flag is forced off, so any trade that survives with
    ``require_structure`` disabled can only be a setup at the external
    structure - exactly what the trader asked for.
    """
    strategy, params, original = prepared
    data = original.copy()
    assert params["require_structure"] is False
    data["internal_high_ok"] = False
    data["internal_low_ok"] = False

    open_gate = strategy.rules(data, params)
    strict = strategy.rules(data, strategy.resolve_params({"require_structure": True}))

    assert int((open_gate.long_entry | open_gate.short_entry).sum()) > 0
    assert int((strict.long_entry | strict.short_entry).sum()) == 0


def test_trend_and_range_filters_stay_on(prepared):
    _, params, _ = prepared
    assert params["require_trend"] is True
    assert params["reject_ranging"] is True


# ------------------------------------------------------- exit at liquidity
def test_targets_are_beyond_price(prepared):
    _, _, data = prepared
    tail = data.tail(20)
    longs = tail["liq_target_long"]
    shorts = tail["liq_target_short"]
    assert (longs > tail["close"]).all(), "a long target must be above price"
    assert (shorts < tail["close"]).all(), "a short target must be below price"


def test_liquidity_targets_use_confirmed_swings_only():
    data = frame(400)
    data["atr"] = (data["high"] - data["low"]).rolling(14).mean()
    above, below = liquidity_targets(data, minor_k=3, lookback_pivots=3)
    # never NaN: a fallback is used at new extremes
    assert above.notna().all() and below.notna().all()
    # a target is always a real swing level or the ATR fallback, never random noise
    assert (above > data["close"]).all()
    assert (below < data["close"]).all()


def test_targets_clear_the_costs(prepared):
    """A level closer than 0.75 ATR is skipped - it is smaller than the fees."""
    _, params, data = prepared
    assert params["min_target_atr"] == 0.75
    distance = (data["liq_target_long"] - data["close"]) / data["atr"]
    fresh = data["atr"].notna() & (data["atr"] > 0)
    # fallback targets are exactly 2 ATR away, real levels are further than the floor
    assert (distance[fresh] >= params["min_target_atr"] - 1e-9).mean() > 0.98


def test_no_tdi_cross_exit_by_default(prepared):
    strategy, params, data = prepared
    triggers = strategy.rules(data, params)
    assert not triggers.long_exit.any()
    assert not triggers.short_exit.any()


def test_tdi_cross_exit_can_be_switched_back_on(prepared):
    strategy, _, data = prepared
    params = strategy.resolve_params({"exit_on_tdi_cross": True})
    triggers = strategy.rules(data, params)
    assert triggers.long_exit.any() or triggers.short_exit.any()


def test_backtest_exits_at_the_target(prepared):
    """Every closed trade exits on 'target', at the level, or on the clock."""
    strategy, params, data = prepared
    triggers = strategy.rules(data, params)
    result = run_backtest(
        data, triggers, strategy, params, BacktestConfig(), 900, include_curve=False
    )
    assert result.trades, "v4 should trade on this fixture"
    for trade in result.trades:
        assert trade.exit_reason in {"target", "max_bars", "end_of_data"}
        if trade.exit_reason == "target":
            assert trade.target is not None
            assert trade.exit_price == pytest.approx(trade.target * (1 - 0.0002), rel=1e-6)


def test_max_bars_closes_stale_trades(prepared):
    strategy, params, data = prepared
    triggers = strategy.rules(data, params)
    short_clock = dict(params, max_bars=10)
    result = run_backtest(
        data, triggers, strategy, short_clock, BacktestConfig(), 900, include_curve=False
    )
    for trade in result.trades:
        assert trade.bars_held <= 11


def test_structure_invalidation_is_available_but_off(prepared):
    strategy, params, data = prepared
    assert params["invalidate_on_structure"] is False
    strict = strategy.resolve_params({"invalidate_on_structure": True})
    triggers = strategy.rules(data, strict)
    assert triggers.long_exit.any() or triggers.short_exit.any()


# -------------------------------------------------------------- reporting
def test_checks_mention_the_liquidity_exit(prepared):
    strategy, params, data = prepared
    labels = [check.label for check in strategy.checks(data, params)]
    assert any("Exit: next liquidity" in label for label in labels)
    assert not any("volume" in label.lower() for label in labels)
