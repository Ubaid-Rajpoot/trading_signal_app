"""TDI + Structural Shift  (a.k.a. TDIGM / HVB system).

The rule, in the trader's own words:

    "TDI set karo: RSI period 50, Band length 50, Fast MA on RSI 1, Slow MA on RSI 9.
     Bich wala band (Market Base Line) ko purple line jab TEZI se break kare - HVB.
     Usi waqt candle upar se koi high ya low break kare - SHIFT.
     Phir entry plan."

So one candle must do two things at the same time:

  1. **HVB** - the purple line (RSI Price Line) crosses the Market Base Line
     *aggressively*: it travels at least ``hvb_step`` RSI points in
     ``hvb_step_bars`` bars and lands at least ``hvb_margin`` points beyond the
     band. A slow, flat wobble through 50 is not an HVB.
  2. **SHIFT / MS** - price closes through a swing level. Breaking a *minor*
     swing is a SHIFT, breaking a *major* swing is an MS.

Direction must agree: RSI up + a high broken = long, RSI down + a low broken = short.

Exit when the purple line crosses back through the base line (momentum lost),
or when the stop / target is hit.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .. import indicators as ta
from .. import swings
from .base import ParamSpec, RuleCheck, Strategy, Targets, TriggerSet


class TdiShift(Strategy):
    id = "tdi_shift"
    name = "TDI + Shift (HVB)"
    description = (
        "TDIGM confluence: the purple RSI line breaks the Market Base Line with speed "
        "(HVB) on the same candle that price breaks a minor swing (SHIFT) or a major "
        "swing (MS). Stop beyond the swing, targets at R multiples."
    )
    market = "Crypto spot / perpetual"
    timeframe_hint = "15m"
    allows_short = True

    params = [
        # ---- TDI settings (his exact settings as defaults) ------------------
        ParamSpec("rsi_len", "TDI · RSI period", "int", 50, 5, 100, 1),
        ParamSpec("band_len", "TDI · Band length", "int", 50, 5, 100, 1),
        ParamSpec("fast_len", "TDI · Fast MA on RSI", "int", 1, 1, 20, 1),
        ParamSpec("slow_len", "TDI · Slow MA on RSI", "int", 9, 1, 50, 1),
        ParamSpec("band_mult", "TDI · Band deviation", "float", 1.6185, 0.5, 4.0, 0.1),
        # ---- HVB: what counts as "tezi" (speed) ----------------------------
        ParamSpec("hvb_step", "HVB · min RSI travel", "float", 4.0, 0.5, 30.0, 0.5,
                  "How many RSI points the purple line must cover to count as an aggressive break."),
        ParamSpec("hvb_step_bars", "HVB · travel window (bars)", "int", 2, 1, 10, 1,
                  "The travel above is measured over this many bars."),
        ParamSpec("hvb_margin", "HVB · min close-through margin", "float", 1.0, 0.0, 15.0, 0.5,
                  "How far past the base line the purple line must finish."),
        # ---- structure ------------------------------------------------------
        ParamSpec("swing_minor", "Structure · minor swing (bars)", "int", 3, 1, 15, 1,
                  "Fractal size for a SHIFT break."),
        ParamSpec("swing_major", "Structure · major swing (bars)", "int", 15, 5, 60, 1,
                  "Fractal size for an MS break."),
        ParamSpec("allow_shift", "Structure · accept SHIFT breaks", "bool", True, None, None, None),
        ParamSpec("allow_ms", "Structure · accept MS breaks", "bool", True, None, None, None),
        ParamSpec("confluence_window", "Confluence window (bars)", "int", 0, 0, 5, 1,
                  "0 = the swing break must happen on the exact same candle as the HVB. "
                  "1-3 allow the break to come this many bars later."),
        # ---- risk -----------------------------------------------------------
        ParamSpec("stop_buffer_atr", "Stop buffer (ATR)", "float", 0.25, 0.0, 3.0, 0.05,
                  "Extra room beyond the swing that was broken."),
        ParamSpec("atr_len", "ATR length", "int", 14, 5, 50, 1),
        ParamSpec("target_r", "Second target (R)", "float", 2.0, 1.0, 8.0, 0.5),
        ParamSpec("long_only", "Long only", "bool", False, None, None, None),
    ]

    plot_lines = [
        {"column": "sw_hi_minor", "label": "Minor swing high", "colour": "#f59e0b"},
        {"column": "sw_lo_minor", "label": "Minor swing low", "colour": "#f59e0b"},
        {"column": "sw_hi_major", "label": "Major swing high", "colour": "#8b5cf6"},
        {"column": "sw_lo_major", "label": "Major swing low", "colour": "#8b5cf6"},
    ]

    # The oscillator pane the UI draws underneath the price chart.
    pane = {
        "title": "TDI",
        "levels": [32, 50, 68],
        "lines": [
            {"column": "tdi_rsi", "label": "RSI (purple)", "colour": "#a855f7", "width": 1.8},
            {"column": "tdi_slow", "label": "Signal MA", "colour": "#64748b", "width": 1.1},
            {"column": "tdi_mbl", "label": "Market Base Line", "colour": "#e2e8f0", "width": 1.4},
        ],
        "bands": [{"upper": "tdi_upper", "lower": "tdi_lower", "colour": "#3b82f6"}],
        "markers": [
            {"column": "hvb_up", "side": "LONG"},
            {"column": "hvb_down", "side": "SHORT"},
        ],
    }

    # ------------------------------------------------------------- prepare
    def prepare(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        data = df.copy()
        tdi = ta.tdi(
            data["close"],
            int(params["rsi_len"]),
            int(params["band_len"]),
            int(params["fast_len"]),
            int(params["slow_len"]),
            float(params["band_mult"]),
        )
        for column in tdi.columns:
            data[f"tdi_{column}"] = tdi[column]

        # Raw purple line == RSI when fast MA is 1; keep the name explicit.
        purple = data["tdi_fast"]

        # --- HVB: an aggressive cross of the Market Base Line ----------------
        up_cross = ta.crossed_above(purple, data["tdi_mbl"])
        down_cross = ta.crossed_below(purple, data["tdi_mbl"])

        window = int(params["hvb_step_bars"])
        travel = purple - purple.shift(window)
        data["hvb_travel"] = travel
        data["hvb_margin_up"] = purple - data["tdi_mbl"]
        data["hvb_margin_down"] = data["tdi_mbl"] - purple

        strong_up = travel >= float(params["hvb_step"])
        strong_down = travel <= -float(params["hvb_step"])
        clear_up = data["hvb_margin_up"] >= float(params["hvb_margin"])
        clear_down = data["hvb_margin_down"] >= float(params["hvb_margin"])

        data["hvb_up"] = (up_cross & strong_up & clear_up).fillna(False)
        data["hvb_down"] = (down_cross & strong_down & clear_down).fillna(False)

        # --- structure ------------------------------------------------------
        data = swings.structure_frame(
            data,
            minor_k=int(params["swing_minor"]),
            major_k=int(params["swing_major"]),
        )

        data["atr"] = ta.atr(data, int(params["atr_len"]))
        return data

    # --------------------------------------------------------------- rules
    def rules(self, data: pd.DataFrame, params: dict[str, Any]) -> TriggerSet:
        structure_up = pd.Series(False, index=data.index)
        structure_down = pd.Series(False, index=data.index)
        if params.get("allow_shift"):
            structure_up |= data["shift_up"]
            structure_down |= data["shift_down"]
        if params.get("allow_ms"):
            structure_up |= data["ms_up"]
            structure_down |= data["ms_down"]

        # Same candle (window = 0): TDI breaks the base line with speed AND price
        # breaks structure. A window > 0 allows the break to trail the HVB.
        window = int(params.get("confluence_window", 0))
        if window > 0:
            recent_up = structure_up.astype(int).rolling(window + 1, min_periods=1).max().astype(bool)
            recent_down = structure_down.astype(int).rolling(window + 1, min_periods=1).max().astype(bool)
        else:
            recent_up, recent_down = structure_up, structure_down

        long_entry = data["hvb_up"] & recent_up
        short_entry = data["hvb_down"] & recent_down

        # Exit: momentum crosses back the other way.
        long_exit = ta.crossed_below(data["tdi_fast"], data["tdi_mbl"])
        short_exit = ta.crossed_above(data["tdi_fast"], data["tdi_mbl"])

        if params.get("long_only"):
            short_entry = pd.Series(False, index=data.index)
            short_exit = pd.Series(False, index=data.index)

        return TriggerSet(long_entry, long_exit, short_entry, short_exit)

    # ------------------------------------------------------------- targets
    def targets(self, data: pd.DataFrame, params: dict[str, Any], side) -> Targets:
        """Stop beyond the swing that the break left behind, targets at R multiples."""
        if data.empty:
            return Targets()
        row = data.iloc[-1]
        price = float(row["close"])
        atr = float(row["atr"]) if pd.notna(row.get("atr")) else price * 0.005
        buffer = atr * float(params["stop_buffer_atr"])

        if side == "LONG":
            swing_low = row.get("sw_lo_minor")
            if pd.isna(swing_low) or float(swing_low) >= price:
                swing_low = row.get("sw_lo_major")
            stop = float(swing_low) - buffer if pd.notna(swing_low) and float(swing_low) < price else price - 2 * atr
        else:
            swing_high = row.get("sw_hi_minor")
            if pd.isna(swing_high) or float(swing_high) <= price:
                swing_high = row.get("sw_hi_major")
            stop = float(swing_high) + buffer if pd.notna(swing_high) and float(swing_high) > price else price + 2 * atr

        risk = abs(price - stop)
        if risk <= 0:
            return Targets()
        rr = float(params["target_r"])
        ladder = [1.0, rr, rr * 1.6]
        if side == "LONG":
            tps = [price + m * risk for m in ladder]
        else:
            tps = [price - m * risk for m in ladder]
        return Targets(entry=price, stop=stop, take_profits=tps)

    # ------------------------------------------------------------- checks
    def checks(self, data: pd.DataFrame, params: dict[str, Any]) -> list[RuleCheck]:
        row = data.iloc[-1]
        purple = float(row["tdi_fast"]) if pd.notna(row["tdi_fast"]) else float("nan")
        mbl = float(row["tdi_mbl"]) if pd.notna(row["tdi_mbl"]) else float("nan")
        travel = float(row["hvb_travel"]) if pd.notna(row["hvb_travel"]) else 0.0
        step = float(params["hvb_step"])
        above = purple > mbl

        def f(v: Any) -> str:
            try:
                return f"{float(v):.2f}"
            except (TypeError, ValueError):
                return "-"

        return [
            RuleCheck(
                "Purple line above the middle band" if above else "Purple line below the middle band",
                True,
                f"RSI {f(purple)} vs MBL {f(mbl)}",
            ),
            RuleCheck(
                f"HVB speed ≥ {step:g} pts / {int(params['hvb_step_bars'])} bars",
                abs(travel) >= step,
                f"travel {travel:+.2f}",
            ),
            RuleCheck(
                "Structure: SHIFT (minor) or MS (major)",
                bool(row.get("shift_up") or row.get("shift_down") or row.get("ms_up") or row.get("ms_down")),
                f"minor high {f(row.get('sw_hi_minor'))} · major high {f(row.get('sw_hi_major'))}",
            ),
            RuleCheck(
                "Same-candle confluence",
                bool(row["hvb_up"] or row["hvb_down"]) and bool(
                    row.get("shift_up") or row.get("shift_down") or row.get("ms_up") or row.get("ms_down")
                ),
                "purple cross + swing break on this bar",
            ),
        ]

    def required_bars(self, params: dict[str, Any]) -> int:
        return (
            max(
                int(params["rsi_len"]) + int(params["band_len"]),
                2 * int(params["swing_major"]) + 1,
                80,
            )
            + 40
        )
