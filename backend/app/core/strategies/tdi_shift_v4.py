"""TDI + Structural Shift, version 4.0 - the trader's corrections applied.

Three things were wrong before, and they were wrong because they were invented
rather than asked for:

1. **Volume.** v3 required a 1.5x volume spike to call something an HVB. The
   trader never said that: HVB is the purple line breaking the centre band
   *aggressively*, and the break is what matters. v4 drops the volume test
   (``hvb_use_volume = False``); the toggle is still there if it is ever wanted.

2. **Location.** v2 forced the setup to sit *inside* internal structure, which
   silenced setups forming at the big trend's own structure. The trader's
   clarification: "the setup can form in the big trend as well as inside
   internal structure". So v4 accepts both - a minor (internal) break or a
   major (external) break - and only insists that the market is trending, the
   trade goes with the trend, and price is not sitting in a liquidity cluster.

3. **Exit.** The old exit - "close when the purple line crosses back" - was the
   agent's invention and it cuts winners. The trader's answer: **exit at the
   next swing low/high, i.e. the next liquidity**. v4 trades to the nearest
   swing high above (long) or swing low below (short) and, because there is
   still no stop, closes after ``max_bars`` so a loser cannot stay open forever.

v1-v3 stay available and untouched.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .. import indicators as ta
from .. import swings
from .base import ParamSpec, RuleCheck, Targets, TriggerSet
from .tdi_shift import TdiShift
from .tdi_shift_v1 import TdiShiftV1
from .tdi_shift_v3 import TdiShiftV3


def liquidity_targets(
    data: pd.DataFrame,
    *,
    minor_k: int = 3,
    lookback_pivots: int = 4,
    fallback_atr: float = 2.0,
    min_distance_atr: float = 0.75,
) -> tuple[pd.Series, pd.Series]:
    """Nearest confirmed swing high above / swing low below each bar's close.

    Pivots are only used ``minor_k`` bars after they form (the same
    confirmation delay the structure code uses), so a target can never be read
    from a swing that was not yet visible at the signal bar.
    """
    n = len(data)
    high = data["high"].to_numpy(dtype=float)
    low = data["low"].to_numpy(dtype=float)
    close = data["close"].to_numpy(dtype=float)
    atr = data["atr"].to_numpy(dtype=float) if "atr" in data.columns else np.full(n, np.nan)

    def levels(flags: pd.Series, prices: np.ndarray, is_high: bool) -> np.ndarray:
        confirmed = flags.fillna(False).to_numpy().astype(bool)
        confirmed[:minor_k] = False
        index = np.flatnonzero(confirmed)
        if index.size == 0:
            return np.full(n, np.nan)
        values = prices[index]
        # A level a few ticks away is not a target - the round trip costs more
        # than the move. Skip levels inside ``min_distance_atr`` of price.
        min_distance = np.where(np.isfinite(atr), atr * min_distance_atr, close * 0.001)
        count = np.searchsorted(index, np.arange(n), side="right")
        chosen = np.full(n, np.nan)
        for step in range(1, lookback_pivots + 1):
            slot = count - step
            valid = slot >= 0
            candidate = np.where(valid, values[np.clip(slot, 0, values.size - 1)], np.nan)
            # nearest = smallest high above close / largest low below close
            if is_high:
                usable = candidate > close + min_distance
                better = np.isfinite(candidate) & usable & (
                    ~np.isfinite(chosen) | (candidate < chosen)
                )
            else:
                usable = candidate < close - min_distance
                better = np.isfinite(candidate) & usable & (
                    ~np.isfinite(chosen) | (candidate > chosen)
                )
            chosen = np.where(better, candidate, chosen)
        return chosen

    pivot_high = swings.pivot_flags(data["high"], minor_k, "high").shift(minor_k)
    pivot_low = swings.pivot_flags(data["low"], minor_k, "low").shift(minor_k)

    target_above = levels(pivot_high, high, True)
    target_below = levels(pivot_low, low, False)

    fallback = np.where(np.isfinite(atr), atr * fallback_atr, close * 0.01)
    target_above = np.where(np.isfinite(target_above), target_above, close + fallback)
    target_below = np.where(np.isfinite(target_below), target_below, close - fallback)

    return (
        pd.Series(target_above, index=data.index, name="liq_target_long"),
        pd.Series(target_below, index=data.index, name="liq_target_short"),
    )


class TdiShiftV4(TdiShiftV3):
    id = "tdi_shift_v4"
    name = "TDI + Shift · v4 (liquidity target exit)"
    version = "4.0"
    version_note = (
        "HVB = aggressive centre-band break, volume test removed. Setups allowed at "
        "the big trend's structure AND inside internal structure. Exit at the next "
        "swing high/low (liquidity), with a max_bars clock because there is no stop."
    )
    supersedes = "tdi_shift_v3"
    description = (
        "TDIGM confluence: purple line breaks the centre band aggressively on the same "
        "candle that price breaks a swing (SHIFT minor or MS major), with the trend, "
        "not in a range and not into resting liquidity. Exit at the next swing level."
    )
    uses_stops = False
    level_columns = ("liq_target_long", "liq_target_short")

    params = list(TdiShiftV3.params) + [
        ParamSpec("exit_on_tdi_cross", "V4 · also exit when the purple line crosses back", "bool", False,
                  None, None, None,
                  "Off by default: the trade runs to the next swing level, not to an indicator wobble."),
        ParamSpec("invalidate_on_structure", "V4 · exit if the structure fails", "bool", False,
                  None, None, None,
                  "Off by default (you asked for no stop). On = close when price closes back through "
                  "the swing the setup broke, which is invalidation, not a fixed stop."),
        ParamSpec("max_bars", "V4 · close a trade after this many bars", "int", 200, 5, 5000, 5,
                  "No stop loss means a trade needs a clock; without it a loser can stay open forever."),
        ParamSpec("target_pivots", "V4 · swing levels to look at", "int", 4, 1, 12, 1,
                  "How many recent swing highs/lows are considered when picking the next target."),
        ParamSpec("target_fallback_atr", "V4 · target fallback (ATR)", "float", 2.0, 0.5, 10.0, 0.5,
                  "Used only when price is at a new extreme and no swing level is left ahead."),
        ParamSpec("min_target_atr", "V4 · ignore swing levels closer than (ATR)", "float", 0.75, 0.0, 5.0, 0.25,
                  "A level a few ticks away is smaller than the round-trip cost, so it is skipped."),
    ]

    # The trader's corrections, as defaults rather than as a hidden opinion.
    _OVERRIDES = {
        "hvb_use_volume": False,
        "require_structure": False,  # at external structure is equally valid
        # With the volume test gone, the speed floor is what defines "aggressive".
        # 2.0 RSI points in 2 bars measured better than 4.0 on real BTC data
        # (24 vs 6 trades over 6 months on 5m, same win rate) without adding noise.
        "hvb_step": 2.0,
        "max_bars": 200,
    }

    # ------------------------------------------------------------- prepare
    def prepare(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        data = super().prepare(df, params)
        above, below = liquidity_targets(
            data,
            minor_k=int(params.get("swing_minor", 3)),
            lookback_pivots=int(params.get("target_pivots", 4)),
            fallback_atr=float(params.get("target_fallback_atr", 2.0)),
            min_distance_atr=float(params.get("min_target_atr", 0.75)),
        )
        data["liq_target_long"] = above
        data["liq_target_short"] = below
        return data

    # --------------------------------------------------------------- rules
    def rules(self, data: pd.DataFrame, params: dict[str, Any]) -> TriggerSet:
        triggers = TdiShift.rules(self, data, params)
        # The exit is the liquidity target (handled by the engine via
        # ``level_columns``) plus the max_bars clock. The old "purple line
        # crosses back" exit is off unless explicitly switched on, because it
        # was cutting winners before the swing level was reached.
        if params.get("exit_on_tdi_cross", False):
            long_exit, short_exit = triggers.long_exit, triggers.short_exit
        else:
            long_exit = pd.Series(False, index=data.index)
            short_exit = pd.Series(False, index=data.index)
        if params.get("invalidate_on_structure", False):
            long_exit = long_exit | (data["close"] < data["sw_lo_minor"]).fillna(False)
            short_exit = short_exit | (data["close"] > data["sw_hi_minor"]).fillna(False)
        return TriggerSet(triggers.long_entry, long_exit, triggers.short_entry, short_exit)

    # ------------------------------------------------------------- checks
    def checks(self, data: pd.DataFrame, params: dict[str, Any]) -> list[RuleCheck]:
        checks = [c for c in TdiShift.checks(self, data, params) if "volume" not in c.label.lower()]
        row = data.iloc[-1]
        for index, check in enumerate(checks):
            if check.label.startswith("Setup inside internal structure"):
                ok = bool(
                    row.get("internal_high_ok")
                    or row.get("internal_low_ok")
                    or row.get("shift_up")
                    or row.get("shift_down")
                    or row.get("ms_up")
                    or row.get("ms_down")
                )
                checks[index] = RuleCheck(
                    "Location: external or internal structure",
                    ok,
                    "the break is at the big trend's structure or inside the internal one",
                )
                break
        target_long = row.get("liq_target_long")
        target_short = row.get("liq_target_short")
        try:
            checks.append(
                RuleCheck(
                    "Exit: next liquidity (swing level)",
                    True,
                    f"long target {float(target_long):,.2f} · short target {float(target_short):,.2f}",
                )
            )
        except (TypeError, ValueError):
            pass
        return checks

    def targets(self, data: pd.DataFrame, params: dict[str, Any], side) -> Targets:
        row = data.iloc[-1]
        value = row.get("liq_target_long" if side.upper() == "LONG" else "liq_target_short")
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = None
        return Targets(
            entry=float(row["close"]),
            stop=None,
            take_profits=[value] if value is not None and np.isfinite(value) else [],
        )
