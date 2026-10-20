"""Vectorised technical indicators built on pandas only (no native deps).

Every function takes and returns pandas objects aligned to the input index, and
uses Wilder's smoothing where the classic definition calls for it (RSI, ATR,
ADX). All indicators are causal - a value at bar *i* uses data up to bar *i*
only, which keeps backtests free of look-ahead bias.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def wilder(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing (RMA) = EMA with alpha = 1/length."""
    return series.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = wilder(gain, length)
    avg_loss = wilder(loss, length)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # If there were only gains in the window, RSI is 100 by definition.
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return wilder(true_range(df), length)


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line, signal_line, macd_line - signal_line


def bollinger(
    close: pd.Series, length: int = 20, mult: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, length)
    sd = close.rolling(length, min_periods=length).std(ddof=0)
    return mid + mult * sd, mid, mid - mult * sd


def stochastic(
    df: pd.DataFrame, k: int = 14, d: int = 3
) -> tuple[pd.Series, pd.Series]:
    low_min = df["low"].rolling(k, min_periods=k).min()
    high_max = df["high"].rolling(k, min_periods=k).max()
    pct_k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    return pct_k, pct_k.rolling(d, min_periods=d).mean()


def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = wilder(true_range(df), length).replace(0, np.nan)
    plus_di = 100 * wilder(plus_dm, length) / tr
    minus_di = 100 * wilder(minus_dm, length) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return wilder(dx, length)


def crossed_above(a: pd.Series, b: pd.Series) -> pd.Series:
    """True on the bar where series *a* crosses above series *b*."""
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossed_below(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def crossed_above_level(a: pd.Series, level: float) -> pd.Series:
    return (a > level) & (a.shift(1) <= level)


def crossed_below_level(a: pd.Series, level: float) -> pd.Series:
    return (a < level) & (a.shift(1) >= level)


def rolling_high(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).max()


def rolling_low(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).min()


def pct_change(series: pd.Series, periods: int) -> pd.Series:
    return series.pct_change(periods) * 100.0
