import numpy as np
import pandas as pd
import pytest

from app.core import indicators as ta


def test_sma_matches_manual_average():
    series = pd.Series([1, 2, 3, 4, 5], dtype=float)
    result = ta.sma(series, 3)
    assert np.isnan(result.iloc[1])
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[4] == pytest.approx(4.0)


def test_ema_reacts_faster_than_sma():
    series = pd.Series(np.arange(50, dtype=float))
    fast = ta.ema(series, 5)
    slow = ta.sma(series, 5)
    # After a steady uptrend both converge to the same level, but EMA must not
    # lag as far behind the latest price as the SMA does.
    assert fast.iloc[-1] < series.iloc[-1]
    assert fast.iloc[-1] > slow.iloc[-1]


def test_rsi_bounds_and_extremes():
    rising = pd.Series(np.arange(1, 60, dtype=float))
    falling = pd.Series(np.arange(60, 1, -1, dtype=float))
    rsi_up = ta.rsi(rising, 14).dropna()
    rsi_down = ta.rsi(falling, 14).dropna()
    assert (rsi_up >= 0).all() and (rsi_up <= 100).all()
    assert rsi_up.iloc[-1] > 99
    assert rsi_down.iloc[-1] < 1


def test_atr_is_positive_and_matches_range(candles):
    atr = ta.atr(candles, 14).dropna()
    assert (atr > 0).all()
    manual_tr = (candles["high"] - candles["low"]).iloc[1:]
    assert atr.mean() < manual_tr.max() * 3


def test_adx_is_bounded(candles):
    adx = ta.adx(candles, 14).dropna()
    assert (adx >= 0).all() and (adx <= 100).all()


def test_crosses_are_single_bar_events():
    a = pd.Series([1, 2, 3, 4, 3, 2], dtype=float)
    b = pd.Series([2.5, 2.5, 2.5, 2.5, 2.5, 2.5], dtype=float)
    above = ta.crossed_above(a, b)
    assert int(above.sum()) == 1
    assert bool(above.iloc[2])
    below = ta.crossed_below(a, b)
    assert int(below.sum()) == 1
    assert bool(below.iloc[5])
