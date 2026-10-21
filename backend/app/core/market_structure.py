"""Market structure context: trend, range, structure scope and liquidity.

The TDI + shift rule on its own fires anywhere, including inside chop. The
trader's refinement (v2) is that a setup only counts when it forms **inside
external or internal structure** - and never inside a range, and never where
liquidity is building up.

Definitions used here, all computed from confirmed swings only:

**External structure** - the direction of the last *major* break. ``trend_dir``
is +1 for an up break, -1 for a down break, and holds until the opposite break
happens. Before the first major break there is no trend, so nothing is valid.

**Internal structure** - the minor swings that form *inside the current external
leg*, i.e. after that break. In the trader's drawings the impulse leg breaks
external structure, then a compact corrective structure builds (a minor high and
a minor low), and the entry is the break of that structure in the trend
direction. So a setup is "inside structure" when both minor pivots are newer
than the last major break and the structure is compact
(``max_structure_atr`` ATR tall) rather than a wide, overlapping mess.

**Range** - no fresh major break for ``max_bos_age`` bars, ADX below ``adx_min``,
or an efficiency ratio (net travel / total travel over ``er_len`` bars) below
``er_min``. A wide range still breaks major swings on both sides, so the last
break alone is not enough - the efficiency ratio is what separates a trend from
a range of the same height.

**Liquidity build-up** - ``liq_touches`` or more swing highs (or lows) inside
``liq_tolerance_atr`` ATR of each other within ``liq_lookback`` bars. Price tends
to sweep such a pool instead of continuing, so a setup aimed straight at it is
blocked.

Everything is lag-safe: a swing level only becomes usable ``k`` bars after the
pivot, exactly like :mod:`swings`, so no column here peeks into the future.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ta
from . import swings

TRENDING = "TRENDING"
RANGING = "RANGING"
UNKNOWN = "UNKNOWN"


def _cluster_events(
    bars: pd.DataFrame,
    side: str,
    k: int,
    lookback: int,
    tolerance_atr: float,
    atr: pd.Series,
    touches: int,
) -> pd.Series:
    """Bars where ``touches`` swing points sit on top of each other.

    ``side="high"`` finds equal highs (liquidity above), ``"low"`` equal lows
    (liquidity below). The flag is placed on the bar where the *last* of the
    cluster becomes confirmable, never earlier.
    """
    column = "high" if side == "high" else "low"
    pivots = swings.pivot_flags(bars[column], k, side)

    positions = np.flatnonzero(pivots.to_numpy())
    levels = bars[column].to_numpy()
    strides = atr.to_numpy()
    out = np.zeros(len(bars), dtype=bool)

    recent: list[tuple[int, float]] = []
    for pos in positions:
        level = float(levels[pos])
        tol = float(strides[pos]) * tolerance_atr if np.isfinite(strides[pos]) else 0.0
        recent = [(p, lv) for p, lv in recent if pos - p <= lookback]
        same = [lv for p, lv in recent if tol > 0 and abs(lv - level) <= tol]
        if len(same) + 1 >= touches:
            confirm = pos + k
            if confirm < len(bars):
                out[confirm] = True
        recent.append((pos, level))

    return pd.Series(out, index=bars.index)


def efficiency_ratio(close: pd.Series, length: int = 40) -> pd.Series:
    """Kaufman efficiency ratio: 1 = straight line, 0 = pure chop.

    ``|close - close[n]| / sum(|close_i - close_{i-1}|)`` over ``n`` bars.
    Scale free, so a wide range and a tight range score the same.
    """
    net = (close - close.shift(length)).abs()
    travelled = close.diff().abs().rolling(length, min_periods=length).sum()
    return (net / travelled.replace(0, np.nan)).clip(0, 1)


def _bars_since(events: pd.Series) -> pd.Series:
    """Bars elapsed since the last True in ``events`` (NaN before the first)."""
    groups = events.cumsum()
    age = events.groupby(groups).cumcount().astype(float)
    age[groups == 0] = np.nan
    return age


def regime_frame(
    df: pd.DataFrame,
    *,
    minor_k: int = 3,
    major_k: int = 15,
    atr_len: int = 14,
    adx_len: int = 14,
    adx_min: float = 18.0,
    er_len: int = 40,
    er_min: float = 0.2,
    max_bos_age: int = 80,
    min_structure_atr: float = 0.4,
    max_structure_atr: float = 6.0,
    liq_lookback: int = 30,
    liq_tolerance_atr: float = 0.25,
    liq_touches: int = 3,
) -> pd.DataFrame:
    """Add trend / regime / structure / liquidity columns to a copy of ``df``.

    Expects (or computes) the swing columns from :func:`swings.structure_frame`
    and an ``atr`` column.
    """
    out = df.copy()
    if "sw_hi_major" not in out.columns:
        out = swings.structure_frame(out, minor_k=minor_k, major_k=major_k)
    if "atr" not in out.columns:
        out["atr"] = ta.atr(out, atr_len)

    out["adx"] = ta.adx(out, adx_len)
    atr = out["atr"].replace(0, np.nan)

    # --- external structure: direction of the last major break --------------
    breaks = pd.Series(np.nan, index=out.index, dtype=float)
    breaks[out["ms_up"].fillna(False)] = 1.0
    breaks[out["ms_down"].fillna(False)] = -1.0
    out["trend_dir"] = breaks.ffill()
    out["bars_since_bos"] = _bars_since(breaks.notna())

    # --- internal structure: the minor swings built after that break --------
    hi_pivots = swings.pivot_flags(out["high"], minor_k, "high").shift(minor_k)
    lo_pivots = swings.pivot_flags(out["low"], minor_k, "low").shift(minor_k)
    out["minor_high_age"] = _bars_since(hi_pivots.fillna(False).astype(bool))
    out["minor_low_age"] = _bars_since(lo_pivots.fillna(False).astype(bool))

    out["ext_high"] = out["sw_hi_major"]
    out["ext_low"] = out["sw_lo_major"]
    out["int_high"] = out["sw_hi_minor"]
    out["int_low"] = out["sw_lo_minor"]

    structure_height = out["sw_hi_minor"] - out["sw_lo_minor"]
    out["structure_atr"] = structure_height / atr

    bos_age = out["bars_since_bos"]
    newer_high = out["minor_high_age"] < bos_age
    newer_low = out["minor_low_age"] < bos_age
    compact = out["structure_atr"].between(min_structure_atr, max_structure_atr)

    out["internal_structure"] = (newer_high & newer_low & compact).fillna(False)
    out["internal_high_ok"] = (newer_high & compact).fillna(False)
    out["internal_low_ok"] = (newer_low & compact).fillna(False)

    # --- trending or ranging? ----------------------------------------------
    out["efficiency"] = efficiency_ratio(out["close"], er_len)

    has_trend = out["trend_dir"].notna()
    fresh = bos_age <= max_bos_age
    directional = out["adx"] >= adx_min
    efficient = out["efficiency"] >= er_min

    trending = has_trend & fresh & directional & efficient
    out["regime"] = np.where(trending, TRENDING, RANGING)
    out.loc[~has_trend, "regime"] = UNKNOWN

    out["regime_reason"] = np.select(
        [~has_trend, ~fresh, ~directional, ~efficient],
        [
            "no major structure break yet",
            f"no fresh major break for more than {max_bos_age} bars",
            f"ADX below {adx_min:g}",
            f"choppy: efficiency ratio below {er_min:g}",
        ],
        default="trending",
    )

    # --- liquidity build-up -------------------------------------------------
    out["liq_above"] = _cluster_events(
        out, "high", minor_k, liq_lookback, liq_tolerance_atr, out["atr"], liq_touches
    )
    out["liq_below"] = _cluster_events(
        out, "low", minor_k, liq_lookback, liq_tolerance_atr, out["atr"], liq_touches
    )
    out["liq_above"] = out["liq_above"].rolling(liq_lookback, min_periods=1).max().astype(bool)
    out["liq_below"] = out["liq_below"].rolling(liq_lookback, min_periods=1).max().astype(bool)

    # --- the gate -----------------------------------------------------------
    trending = out["regime"].eq(TRENDING).fillna(False)
    up = (out["trend_dir"] > 0).fillna(False)
    down = (out["trend_dir"] < 0).fillna(False)

    out["setup_ok_long"] = trending & up & out["internal_high_ok"] & ~out["liq_above"]
    out["setup_ok_short"] = trending & down & out["internal_low_ok"] & ~out["liq_below"]
    return out


def describe(row: pd.Series) -> dict:
    """Human-readable context for the signal payload / UI."""
    def num(value, digits=2):
        try:
            value = float(value)
            return round(value, digits) if np.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    try:
        trend_dir = float(row.get("trend_dir"))
    except (TypeError, ValueError):
        trend_dir = 0.0
    trend = "UP" if trend_dir > 0 else ("DOWN" if trend_dir < 0 else "NONE")

    regime = row.get("regime") or UNKNOWN
    long_ok = bool(row.get("setup_ok_long"))
    short_ok = bool(row.get("setup_ok_short"))

    blocked = None
    if regime != TRENDING:
        blocked = "ranging"
    elif not (long_ok or short_ok):
        if bool(row.get("liq_above")) or bool(row.get("liq_below")):
            blocked = "liquidity"
        else:
            blocked = "no-structure"

    return {
        "state": f"{regime} {trend}" if regime == TRENDING else regime,
        "trend": trend,
        "regime": regime,
        "reason": row.get("regime_reason"),
        "adx": num(row.get("adx"), 1),
        "efficiency": num(row.get("efficiency"), 2),
        "bars_since_bos": num(row.get("bars_since_bos"), 0),
        "structure_atr": num(row.get("structure_atr"), 1),
        "structure_ready": bool(row.get("internal_structure")),
        "ext_high": num(row.get("ext_high"), 8),
        "ext_low": num(row.get("ext_low"), 8),
        "int_high": num(row.get("int_high"), 8),
        "int_low": num(row.get("int_low"), 8),
        "liquidity_above": bool(row.get("liq_above")),
        "liquidity_below": bool(row.get("liq_below")),
        "long_ok": long_ok,
        "short_ok": short_ok,
        "blocked": blocked,
    }
