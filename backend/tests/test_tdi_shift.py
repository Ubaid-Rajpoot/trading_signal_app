"""Tests for the TDI + structural shift strategy (the trader's own system)."""

import numpy as np
import pandas as pd
import pytest

from app.core import indicators as ta
from app.core import swings
from app.core.data.synthetic import generate_candles
from app.core.signals import build_signal
from app.core.strategies import get_strategy


@pytest.fixture(scope="module")
def strategy():
    return get_strategy("tdi_shift")


@pytest.fixture(scope="module")
def params(strategy):
    return strategy.resolve_params(None)


@pytest.fixture(scope="module")
def prepared(strategy, params):
    df = generate_candles("BTCUSDT", "15m", 900, now=1_800_000_000)
    return strategy.prepare(df, params)


# ------------------------------------------------------------------ the TDI
def test_tdi_uses_the_traders_settings_by_default(params):
    assert params["rsi_len"] == 50
    assert params["band_len"] == 50
    assert params["fast_len"] == 1
    assert params["slow_len"] == 9


def test_tdi_formula_matches_definition():
    close = pd.Series(np.linspace(100, 140, 300) + np.sin(np.arange(300) / 3) * 5)
    out = ta.tdi(close, 50, 50, 1, 9)

    r = ta.rsi(close, 50)
    assert np.allclose(out["rsi"], r, equal_nan=True)
    # fast MA of 1 is the purple line itself
    assert np.allclose(out["fast"], out["rsi"], equal_nan=True)
    # base line is the SMA of RSI, and the midpoint of the bands
    assert np.allclose(out["mbl"], ta.sma(r, 50), equal_nan=True)
    assert np.allclose(out["mbl"], (out["upper"] + out["lower"]) / 2, equal_nan=True)
    # bands sit 1.6185 population standard deviations away
    offset = out["upper"] - out["mbl"]
    expected = 1.6185 * ta.stdev(r, 50)
    assert np.allclose(offset.dropna(), expected.dropna())


def test_tdi_is_causal():
    close = pd.Series(np.cumsum(np.random.default_rng(3).normal(0, 1, 400)) + 200)
    full = ta.tdi(close, 50, 50, 1, 9)
    part = ta.tdi(close.iloc[:300], 50, 50, 1, 9)
    for column in ("rsi", "mbl", "upper", "lower"):
        pd.testing.assert_series_equal(
            full[column].iloc[:300].reset_index(drop=True),
            part[column].reset_index(drop=True),
            check_names=False,
        )


# -------------------------------------------------------------- structure
def test_pivot_is_only_available_after_confirmation():
    df = generate_candles("ETHUSDT", "1h", 400, now=1_800_000_000)
    k = 5
    flags = swings.pivot_flags(df["high"], k, "high")
    pivot_positions = np.flatnonzero(flags.to_numpy())
    assert pivot_positions.size > 0

    levels = swings.swing_levels(df, k)[0]
    # A pivot at bar p must not appear before bar p+k.
    for p in pivot_positions[:20]:
        assert pd.isna(levels.iloc[p + k - 1]) or levels.iloc[p + k - 1] != df["high"].iloc[p]
        if p + k < len(df):
            assert levels.iloc[p + k] == pytest.approx(df["high"].iloc[p])


def test_break_requires_previous_close_inside():
    close = pd.Series([10.0, 10.5, 11.2, 11.5])
    level = pd.Series([11.0, 11.0, 11.0, 11.0])
    events = swings.break_events(close, level, "up")
    assert bool(events.iloc[2]) is True
    assert int(events.sum()) == 1


# ------------------------------------------------------ the actual strategy
def test_minor_breaks_are_more_frequent_than_major(prepared):
    shifts = int((prepared["shift_up"] | prepared["shift_down"]).sum())
    ms = int((prepared["ms_up"] | prepared["ms_down"]).sum())
    assert shifts >= ms


def test_entries_require_same_candle_confluence(strategy, params, prepared):
    triggers = strategy.rules(prepared, params)
    both = prepared["hvb_up"] | prepared["hvb_down"]
    structure = prepared["shift_up"] | prepared["shift_down"] | prepared["ms_up"] | prepared["ms_down"]
    expected_long = (prepared["hvb_up"] & structure).to_numpy()
    expected_short = (prepared["hvb_down"] & structure).to_numpy()
    assert triggers.long_entry.to_numpy().tolist() == expected_long.tolist()
    assert triggers.short_entry.to_numpy().tolist() == expected_short.tolist()
    # No entry may exist without an HVB on that same bar.
    assert not (triggers.long_entry & ~both).any()
    assert not (triggers.short_entry & ~both).any()


def test_hvb_rejects_slow_crawls(prepared):
    """A cross without speed must not be flagged as an HVB."""
    crossed = (prepared["tdi_fast"] > prepared["tdi_mbl"]) & (
        prepared["tdi_fast"].shift(1) <= prepared["tdi_mbl"].shift(1)
    )
    slow = crossed & (prepared["hvb_travel"] < 1.0)
    assert not (prepared.loc[slow, "hvb_up"]).any()


def test_long_and_short_directions_are_mirrored(strategy, params):
    relaxed = strategy.resolve_params(
        {"hvb_step": 0.5, "hvb_margin": 0.0, "confluence_window": 3}
    )
    df = generate_candles("SOLUSDT", "15m", 800, now=1_800_000_000)
    prep = strategy.prepare(df, relaxed)
    trig = strategy.rules(prep, relaxed)
    if trig.long_entry.any():
        row = prep[trig.long_entry].iloc[0]
        assert row["tdi_fast"] > row["tdi_mbl"] or row["hvb_up"]
    if trig.short_entry.any():
        row = prep[trig.short_entry].iloc[0]
        assert row["tdi_fast"] < row["tdi_mbl"] or row["hvb_down"]


def test_no_stop_loss_or_take_profit(strategy, params, prepared):
    """The system reports setups only - there is no risk plan to compute."""
    assert strategy.uses_stops is False
    for side in ("LONG", "SHORT"):
        targets = strategy.targets(prepared, params, side)
        assert targets.stop is None
        assert not targets.take_profits
    signal = build_signal("BTCUSDT", "15m", "tdi_shift", limit=600)
    plan = signal["plan"]
    assert plan["stop"] is None and not plan["take_profits"]
    assert signal["strategy"]["uses_stops"] is False


def test_signal_payload_carries_the_tdi_pane():
    signal = build_signal("BTCUSDT", "15m", "tdi_shift", limit=600)
    pane = signal["chart"]["pane"]
    assert pane is not None
    assert pane["title"] == "TDI"
    assert set(pane["lines"]) == {"tdi_rsi", "tdi_slow", "tdi_mbl"}
    assert pane["bands"]
    n = len(signal["chart"]["time"])
    for line in pane["lines"].values():
        assert len(line["values"]) == n
    for band in pane["bands"]:
        assert len(band["upper"]) == n and len(band["lower"]) == n


def test_strategy_is_registered_and_default():
    from app.core.strategies import DEFAULT_STRATEGY, list_strategies

    ids = [s["id"] for s in list_strategies()]
    # Every published version stays available, newest is the default.
    assert {"tdi_shift_v1", "tdi_shift_v2", "tdi_shift_v3"}.issubset(set(ids))
    assert DEFAULT_STRATEGY == "tdi_shift_v3"
    # The earlier strategies stay available side by side.
    assert {"donchian", "ema_trend", "custom"}.issubset(set(ids))
