"""Swing and market-structure detection.

Implements the price half of the strategy:

* A **swing high** is a fractal pivot - the highest high of ``2k+1`` bars.
  It only becomes *usable* ``k`` bars after it forms, because that is when the
  right side of the fractal is complete. Everything here respects that, so no
  signal ever "sees" a pivot before it was confirmable.
* A break of a **minor** swing (small ``k``) is a **SHIFT** - local structure
  turning.
* A break of a **major** swing (large ``k``) is an **MS** - market structure /
  trend change.

A break fires on the bar where price closes beyond the level while the previous
close was still inside it, which is the standard, non-repainting definition.
"""

from __future__ import annotations

import pandas as pd


def pivot_flags(values: pd.Series, k: int, kind: str) -> pd.Series:
    """True where ``values`` is the extreme of the surrounding ``2k+1`` bars."""
    window = 2 * k + 1
    rolled = values.rolling(window, center=True, min_periods=window)
    extreme = rolled.max() if kind == "high" else rolled.min()
    return values.eq(extreme) & extreme.notna()


def swing_levels(df: pd.DataFrame, k: int) -> tuple[pd.Series, pd.Series]:
    """Forward-filled swing high / low levels, available ``k`` bars after the pivot.

    At bar *i* the returned values are the most recent swing levels that were
    already confirmable at that time (NaN until the first confirmed pivot).
    """
    highs = pivot_flags(df["high"], k, "high")
    lows = pivot_flags(df["low"], k, "low")
    high_level = df["high"].where(highs).shift(k).ffill()
    low_level = df["low"].where(lows).shift(k).ffill()
    return high_level, low_level


def break_events(close: pd.Series, level: pd.Series, direction: str) -> pd.Series:
    """Bars where close crosses the level, using the same level for both comparisons.

    Using ``level`` (not ``level.shift(1)``) on both sides prevents false breaks
    when a new pivot replaces the old one on the same bar.
    """
    if direction == "up":
        return (close > level) & (close.shift(1) <= level) & level.notna()
    return (close < level) & (close.shift(1) >= level) & level.notna()


def structure_frame(
    df: pd.DataFrame,
    minor_k: int = 3,
    major_k: int = 15,
) -> pd.DataFrame:
    """Add swing levels and break flags to a copy of ``df``.

    Columns added:
      sw_hi_minor, sw_lo_minor, sw_hi_major, sw_lo_major
      shift_up, shift_down   (minor break  -> "SHIFT")
      ms_up, ms_down         (major break  -> "MS")
      struct_up, struct_down (either of the two, for convenience)
    """
    out = df.copy()
    close = out["close"]

    hi_minor, lo_minor = swing_levels(out, minor_k)
    hi_major, lo_major = swing_levels(out, major_k)

    out["sw_hi_minor"], out["sw_lo_minor"] = hi_minor, lo_minor
    out["sw_hi_major"], out["sw_lo_major"] = hi_major, lo_major

    out["shift_up"] = break_events(close, hi_minor, "up")
    out["shift_down"] = break_events(close, lo_minor, "down")
    out["ms_up"] = break_events(close, hi_major, "up")
    out["ms_down"] = break_events(close, lo_major, "down")

    out["struct_up"] = out["shift_up"] | out["ms_up"]
    out["struct_down"] = out["shift_down"] | out["ms_down"]
    return out


def broken_levels(df: pd.DataFrame, row: int) -> dict[str, float | None]:
    """Which levels were broken on ``row`` - used for the signal explanation."""
    def value(column: str) -> float | None:
        try:
            v = float(df[column].iloc[row])
            return None if pd.isna(v) else v
        except (KeyError, IndexError):
            return None

    return {
        "minor_high": value("sw_hi_minor"),
        "minor_low": value("sw_lo_minor"),
        "major_high": value("sw_hi_major"),
        "major_low": value("sw_lo_major"),
    }
