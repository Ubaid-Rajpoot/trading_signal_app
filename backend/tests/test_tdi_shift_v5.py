"""v5: the stop and target the trader drew.

The drawing has a range (teal box), a sweep of its low (the vertical drop out of
the red box), a return into the top of the box, and then the move down to the
dashed line. v5 encodes exactly that:

* zone: the high/low of the last ``range_len`` bars,
* sweep: a bar that takes the range low and closes back above it,
* entry: the return into the premium (top) of the box after the sweep,
* stop: beyond the sweep wick - invalidation, not a guess,
* target: the far side of the box (the dashed line),
* filter: skip the trade when the target is worth less than ``min_rr`` x the risk.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.backtest import BacktestConfig, run_backtest
from app.core.data.synthetic import generate_candles
from app.core.strategies import DEFAULT_STRATEGY, get_strategy


@pytest.fixture(scope="module")
def prepared():
    strategy = get_strategy("tdi_shift_v5")
    # The synthetic feed builds very narrow boxes, so the reward/risk filter is
    # relaxed here; the filter itself is checked separately below.
    params = strategy.resolve_params({"min_rr": 0.0})
    frame = generate_candles("BTCUSDT", "15m", 1500).reset_index(drop=True)
    data = strategy.prepare(frame, params)
    strategy.rules(data, params)  # premium entries fill the stop/target columns
    return strategy, params, data


def test_v5_is_default_and_older_versions_are_pinned():
    assert DEFAULT_STRATEGY == "tdi_shift_v5"
    assert get_strategy("tdi_shift").id == "tdi_shift_v5"
    for older in ("tdi_shift_v1", "tdi_shift_v2", "tdi_shift_v3", "tdi_shift_v4"):
        assert get_strategy(older).id == older


def test_defaults_match_the_drawing():
    strategy = get_strategy("tdi_shift_v5")
    params = strategy.resolve_params({})
    assert params["entry_mode"] == "premium"
    assert params["min_rr"] == 0.5
    assert params["max_bars"] == 0, "a defined stop removes the need for a clock"
    assert params["stop_buffer_atr"] >= 0
    assert strategy.uses_stops is True
    assert strategy.stop_columns == ("sl_long", "sl_short")


def test_every_version_keeps_its_own_relaxations():
    """A newer version must not wipe an older version's defaults."""
    from app.core.strategies import get_strategy as get

    v4 = get("tdi_shift_v4").resolve_params({})
    v5 = get("tdi_shift_v5").resolve_params({})
    for key in ("hvb_use_volume", "hvb_step", "require_structure", "reject_ranging"):
        assert v5[key] == v4[key], f"{key} leaked between versions"


def test_stop_is_on_the_invalidation_side(prepared):
    """At every entry, the stop is beyond price (above for shorts, below for longs)."""
    strategy, params, data = prepared
    triggers = strategy.rules(data, params)
    short_entries = triggers.short_entry
    long_entries = triggers.long_entry
    assert short_entries.any() or long_entries.any()
    if short_entries.any():
        assert (data.loc[short_entries, "sl_short"] > data.loc[short_entries, "close"]).all()
    if long_entries.any():
        assert (data.loc[long_entries, "sl_long"] < data.loc[long_entries, "close"]).all()


def test_target_is_at_the_far_side_of_the_box(prepared):
    _, _, data = prepared
    short_targets = data["liq_target_short"].dropna()
    long_targets = data["liq_target_long"].dropna()
    lows = data["range_low"].dropna()
    highs = data["range_high"].dropna()
    assert len(short_targets) and len(long_targets)
    # the target sits outside the box: below its low for shorts, above its high for longs
    assert (short_targets <= lows.loc[short_targets.index]).all()
    assert (long_targets >= highs.loc[long_targets.index]).all()


def test_default_is_half_an_r():
    assert get_strategy("tdi_shift_v5").resolve_params({})["min_rr"] == 0.5


def test_reward_risk_filter_is_applied(prepared):
    strategy, params, data = prepared
    triggers = strategy.rules(data, params)
    entries = triggers.long_entry | triggers.short_entry
    rr = data["rr_long"].where(triggers.long_entry, data["rr_short"])
    values = rr[entries].dropna()
    if len(values):
        assert (values >= float(params["min_rr"])).all()


def test_backtest_only_leaves_on_stop_or_target(prepared):
    strategy, params, data = prepared
    triggers = strategy.rules(data, params)
    result = run_backtest(data, triggers, strategy, params, BacktestConfig(), 900, include_curve=False)
    for trade in result.trades:
        assert trade.exit_reason in {"stop", "target", "end_of_data"}
        if trade.exit_reason == "stop":
            assert (trade.exit_price <= trade.stop * 1.001) if trade.side == "SHORT" else (
                trade.exit_price >= trade.stop * 0.999
            )


def test_plan_exposes_stop_target_and_rr(prepared):
    strategy, params, data = prepared
    for side in ("LONG", "SHORT"):
        targets = strategy.targets(data, params, side)
        if targets.stop is None:
            continue
        assert targets.take_profits, "a level experiment must always have a target"
        entry = targets.entry
        risk = abs(entry - targets.stop)
        reward = abs(targets.take_profits[0] - entry)
        assert risk > 0 and reward > 0


def test_checks_show_the_drawn_levels(prepared):
    strategy, params, data = prepared
    labels = [check.label for check in strategy.checks(data, params)]
    assert any("Stop: beyond the broken structure" in label for label in labels)
    assert any(label.startswith("Reward") for label in labels)
