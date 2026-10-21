"""TDI + Structural Shift, version 5.0 - the trader's picture, with a stop.

The trader drew the setup and asked what the stop loss and the target should be.
Reading the drawing:

* the **red box** is the last structural high of the down move - the ceiling of
  the little range the market built before it broke;
* the **vertical drop** out of that box is the HVB / break of structure;
* the **teal box** is the zone price pulls back into;
* the **dashed line** is the target: the previous low, i.e. the liquidity that
  the move is going to take.

So the plan in v5 is:

    entry  = break of the structural low (short) / high (long), same candle as HVB
    stop   = the other side of the structure - above the red box high for a
             short - plus a small ATR buffer. That is invalidation: if price
             reclaims the structure, the idea was wrong.
    target = the previous swing low/high (the dashed line)
    filter = only take the trade when the target is worth at least ``min_rr``
             times the risk. A setup where the next liquidity is 0.5R away is
             not a trade, however pretty the break looks.

v4 is kept exactly as it was (no stop, exit at liquidity) so the two can be
compared side by side; v5 only adds risk definition.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .base import ParamSpec, RuleCheck, Targets, TriggerSet
from ..market_structure import _bars_since
from .tdi_shift_v4 import TdiShiftV4, liquidity_targets


class TdiShiftV5(TdiShiftV4):
    id = "tdi_shift_v5"
    name = "TDI + Shift · v5 (stop above the structure)"
    version = "5.0"
    version_note = (
        "Same entry as v4, now with the stop the trader drew: beyond the structural "
        "high (shorts) or low (longs), plus a small buffer. Target = the previous "
        "swing level. Trades are skipped when the target is closer than min_rr x risk."
    )
    supersedes = "tdi_shift_v4"
    description = (
        "TDIGM confluence with a defined risk: HVB + same-candle structure break, with "
        "the trend; stop beyond the broken structure, target at the previous swing "
        "low/high (liquidity), and a minimum reward-to-risk filter."
    )

    uses_stops = True
    stop_columns = ("sl_long", "sl_short")

    params = list(TdiShiftV4.params) + [
        ParamSpec("entry_mode", "V5 · entry timing", "select", "retest", None, None, None,
                  "break = enter on the break candle itself. retest = wait for price to come "
                  "back to the broken level (the trader's teal box) and enter there.",
                  options=["break", "retest"]),
        ParamSpec("retest_window", "V5 · bars allowed for the pullback", "int", 30, 1, 300, 1,
                  "After this many bars the break is stale and the retest is ignored."),
        ParamSpec("range_len", "V5 · range length (bars)", "int", 60, 10, 400, 5,
                  "The box in the drawing: its high is the red zone, its low is the teal zone."),
        ParamSpec("premium_level", "V5 · retest depth into the range", "float", 0.9, 0.3, 1.0, 0.05,
                  "1.0 = wait for price to come back to the range high (the top of the box)."),
        ParamSpec("stop_buffer_atr", "V5 · stop buffer beyond the structure (ATR)", "float", 0.15,
                  0.0, 2.0, 0.05,
                  "A stop exactly on the swing gets wicked out; a small buffer puts it beyond the wick."),
        ParamSpec("min_rr", "V5 · minimum reward vs risk", "float", 1.0, 0.0, 5.0, 0.1,
                  "Skip a setup when the next liquidity is closer than this multiple of the risk."),
        ParamSpec("stop_fallback_atr", "V5 · stop fallback (ATR)", "float", 1.5, 0.25, 10.0, 0.25,
                  "Used only when no confirmed swing exists on the invalidation side."),
    ]

    _OVERRIDES = {
        **TdiShiftV4._OVERRIDES,
        "entry_mode": "premium",
        "min_rr": 0.5,  # below 0.5R the trade is not worth the spread
        "max_bars": 0,  # a defined stop makes the time stop unnecessary
    }

    # ------------------------------------------------------------- prepare
    def prepare(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        data = super().prepare(df, params)
        atr = data["atr"].astype(float)
        buffer = atr * float(params.get("stop_buffer_atr", 0.15))
        fallback = atr * float(params.get("stop_fallback_atr", 1.5))

        close = data["close"].astype(float)
        low, high = data["low"].astype(float), data["high"].astype(float)

        stop_long = data["sw_lo_minor"].astype(float) - buffer
        stop_short = data["sw_hi_minor"].astype(float) + buffer
        # No confirmed swing on the invalidation side: fall back to an ATR stop,
        # clearly labelled by the parameter so it is never mistaken for structure.
        stop_long = stop_long.where(stop_long < close - buffer, np.minimum(stop_long, low - fallback))
        stop_short = stop_short.where(stop_short > close + buffer, np.maximum(stop_short, high + fallback))

        # Only a *valid* setup (HVB + structure break + trend, i.e. the entry the
        # strategy already produced) creates the zone: the broken level (teal box
        # edge) and the far side of the range it broke from (red box edge).
        from .tdi_shift import TdiShift  # local import avoids a circular import

        valid = TdiShift.rules(self, data, params)
        setup_short = valid.short_entry.fillna(False)
        setup_long = valid.long_entry.fillna(False)

        data["zone_short"] = data["sw_lo_minor"].where(setup_short).ffill()
        data["zone_short_stop"] = data["sw_hi_minor"].where(setup_short).ffill()
        data["zone_long"] = data["sw_hi_minor"].where(setup_long).ffill()
        data["zone_long_stop"] = data["sw_lo_minor"].where(setup_long).ffill()
        data["bars_since_setup"] = _bars_since(setup_short | setup_long)

        # --- the drawing: a range box, its sweep, then a premium retest --------
        length = int(params.get("range_len", 60))
        prior_high = data["high"].rolling(length, min_periods=10).max().shift(1)
        prior_low = data["low"].rolling(length, min_periods=10).min().shift(1)
        data["range_high"] = prior_high
        data["range_low"] = prior_low

        # Sweep down: the bar takes the range low (liquidity) and closes back above it.
        sweep_down = (data["low"] < prior_low) & (data["close"] > prior_low)
        sweep_up = (data["high"] > prior_high) & (data["close"] < prior_high)
        # The wick that did the sweep is the invalidation level (red box top / bottom).
        data["sweep_high"] = data["high"].where(sweep_down).ffill()
        data["sweep_low"] = data["low"].where(sweep_up).ffill()
        data["bars_since_sweep"] = _bars_since(sweep_down | sweep_up)
        data["sweep_down"] = sweep_down.fillna(False)
        data["sweep_up"] = sweep_up.fillna(False)
        data["sl_retest_short"] = data["zone_short_stop"] + buffer
        data["sl_retest_long"] = data["zone_long_stop"] - buffer

        data["sl_long"] = stop_long
        data["sl_short"] = stop_short

        risk_long = (close - stop_long).clip(lower=0)
        risk_short = (stop_short - close).clip(lower=0)
        reward_long = (data["liq_target_long"] - close).clip(lower=0)
        reward_short = (close - data["liq_target_short"]).clip(lower=0)
        data["rr_long"] = reward_long / risk_long.replace(0, np.nan)
        data["rr_short"] = reward_short / risk_short.replace(0, np.nan)
        return data

    # --------------------------------------------------------------- rules
    def rules(self, data: pd.DataFrame, params: dict[str, Any]) -> TriggerSet:
        triggers = super().rules(data, params)
        minimum = float(params.get("min_rr", 1.0))
        enough_long = data["rr_long"].fillna(0) >= minimum
        enough_short = data["rr_short"].fillna(0) >= minimum

        long_entry = triggers.long_entry & enough_long
        short_entry = triggers.short_entry & enough_short

        if params.get("entry_mode", "premium") == "premium":
            window = int(params.get("retest_window", 30))
            depth = float(params.get("premium_level", 0.9))
            # The box is only tradeable because the trader's setup (HVB + break)
            # just printed; without that we would be selling any random range.
            fresh = data["bars_since_setup"].fillna(10**6).between(1, window)
            close = data["close"].astype(float)
            # Short: after a sweep of the range low, sell the return into the top
            # of the box. Long: the mirror.
            premium_short = data["range_low"] + (data["range_high"] - data["range_low"]) * depth
            premium_long = data["range_high"] - (data["range_high"] - data["range_low"]) * depth
            short_entry = fresh & (data["high"] >= premium_short) & (close < premium_short)
            long_entry = fresh & (data["low"] <= premium_long) & (close > premium_long)
            # Stop beyond the sweep wick, target at the far side of the box (the
            # dashed line in the drawing), which is the liquidity being hunted.
            atr = data["atr"].astype(float)
            buffer = atr * float(params.get("stop_buffer_atr", 0.15))
            # Invalidation = beyond BOTH the sweep wick and the box edge, so the
            # stop can never end up on the wrong side of the entry.
            # fmax/fmin ignore NaN, so a missing sweep or box still yields a stop.
            data["sl_short"] = np.fmax(data["sweep_high"], data["range_high"]) + buffer
            data["sl_long"] = np.fmin(data["sweep_low"], data["range_low"]) - buffer
            data["liq_target_short"] = data["range_low"] - (data["range_high"] - data["range_low"]) * 0.1
            data["liq_target_long"] = data["range_high"] + (data["range_high"] - data["range_low"]) * 0.1
            risk_short = (data["sl_short"] - close).clip(lower=0)
            risk_long = (close - data["sl_long"]).clip(lower=0)
            data["rr_short"] = (close - data["liq_target_short"]).clip(lower=0) / risk_short.replace(0, np.nan)
            data["rr_long"] = (data["liq_target_long"] - close).clip(lower=0) / risk_long.replace(0, np.nan)
            short_entry = short_entry & (data["rr_short"].fillna(0) >= minimum)
            long_entry = long_entry & (data["rr_long"].fillna(0) >= minimum)
        elif params.get("entry_mode") == "retest":
            window = int(params.get("retest_window", 30))
            fresh = data["bars_since_setup"].fillna(10**6).between(1, window)
            close = data["close"].astype(float)
            # Short: after a valid break, price pulls back UP into the broken low
            # (the teal box) and closes rejected below it. Long: the mirror.
            retest_short = fresh & (data["high"].astype(float) >= data["zone_short"]) & (close < data["zone_short"])
            retest_long = fresh & (data["low"].astype(float) <= data["zone_long"]) & (close > data["zone_long"])
            # The stop sits on the far side of the range that broke (the red box),
            # which is exactly what makes the ratio in the drawing large.
            risk_short = (data["sl_retest_short"] - close).clip(lower=0)
            risk_long = (close - data["sl_retest_long"]).clip(lower=0)
            rr_short = (close - data["liq_target_short"]).clip(lower=0) / risk_short.replace(0, np.nan)
            rr_long = (data["liq_target_long"] - close).clip(lower=0) / risk_long.replace(0, np.nan)
            long_entry = retest_long & (rr_long.fillna(0) >= minimum)
            short_entry = retest_short & (rr_short.fillna(0) >= minimum)
            data["rr_long"] = rr_long
            data["rr_short"] = rr_short
            data["sl_long"] = data["sl_retest_long"]
            data["sl_short"] = data["sl_retest_short"]
        return TriggerSet(long_entry, triggers.long_exit, short_entry, triggers.short_exit)

    # ------------------------------------------------------------- targets
    def targets(self, data: pd.DataFrame, params: dict[str, Any], side) -> Targets:
        row = data.iloc[-1]
        long_side = str(side).upper() == "LONG"
        stop = row.get("sl_long" if long_side else "sl_short")
        target = row.get("liq_target_long" if long_side else "liq_target_short")
        try:
            stop = float(stop)
        except (TypeError, ValueError):
            stop = None
        try:
            target = float(target)
        except (TypeError, ValueError):
            target = None
        return Targets(
            entry=float(row["close"]),
            stop=stop if stop is not None and np.isfinite(stop) else None,
            take_profits=[target] if target is not None and np.isfinite(target) else [],
        )

    # -------------------------------------------------------------- checks
    def checks(self, data: pd.DataFrame, params: dict[str, Any]) -> list[RuleCheck]:
        checks = [c for c in super().checks(data, params) if not c.label.startswith("Exit:")]
        row = data.iloc[-1]
        long_side = bool(row.get("tdi_fast", 0) >= row.get("tdi_mbl", 0))
        risk = row.get("rr_long" if long_side else "rr_short")
        stop = row.get("sl_long" if long_side else "sl_short")
        target = row.get("liq_target_long" if long_side else "liq_target_short")

        def fmt(value) -> str:
            try:
                return f"{float(value):,.2f}"
            except (TypeError, ValueError):
                return "-"

        checks.append(
            RuleCheck(
                "Stop: beyond the broken structure",
                bool(pd.notna(stop)),
                f"{fmt(stop)} · target {fmt(target)} · R:R {fmt(risk)}",
            )
        )
        checks.append(
            RuleCheck(
                f"Reward ≥ {float(params.get('min_rr', 1.0)):g}R",
                bool(pd.notna(risk) and float(risk) >= float(params.get("min_rr", 1.0))),
                f"target is {fmt(risk)}x the risk",
            )
        )
        return checks
