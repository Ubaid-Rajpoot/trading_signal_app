"""TDI + Structural Shift, version 3.0 - HVB means HIGH VOLUME break.

v1 and v2 measured "aggression" only in RSI points: the purple line had to
travel 4 points in 2 bars. The trader corrected this:

    "HVB ka matlab hai ke centre wala band break ho HIGH VOLUME ke saath."

HVB is literally *High Volume Breakout* (book p.93). Volume is the thing that
makes a break real, not the RSI arithmetic. So in v3:

    HVB = purple line crosses the Market Base Line
          AND volume is a spike (>= ``hvb_volume_mult`` x its own average)
          AND the cross is not a lazy wobble (>= ``hvb_step`` RSI points,
              default relaxed from 4.0 to 2.0 because volume now carries the
              weight; set ``hvb_step`` to 0 to ignore speed entirely)

Everything else is v2: same structure / trend / range / liquidity gates, same
exit (purple crossing back), no stop loss, no take profit.

v1 and v2 stay available and untouched - v3 only supersedes them.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .. import indicators as ta
from .. import market_structure as ms
from .base import ParamSpec, RuleCheck, TriggerSet
from .tdi_shift import TdiShift
from .tdi_shift_v1 import TdiShiftV1


class TdiShiftV3(TdiShift):
    id = "tdi_shift_v3"
    name = "TDI + Shift · v3 (HVB on volume)"
    version = "3.0"
    version_note = (
        "HVB now requires a volume spike on the centre-band break (the real "
        "meaning of High Volume Breakout), with a relaxed speed floor. All v2 "
        "structure / trend / range / liquidity gates stay."
    )
    supersedes = "tdi_shift_v2"
    description = (
        "TDIGM confluence where HVB = purple line breaks the Market Base Line on a "
        "volume spike, inside external/internal structure, with the external trend, "
        "never in a range and never into a liquidity build-up."
    )

    params = list(TdiShift.params) + [
        ParamSpec("hvb_volume_mult", "HVB · volume multiple", "float", 1.5, 0.5, 10.0, 0.1,
                  "Volume must be at least this multiple of its own average on the break bar."),
        ParamSpec("hvb_volume_len", "HVB · volume average (bars)", "int", 20, 5, 200, 1,
                  "Window for the average volume the spike is measured against."),
        ParamSpec("hvb_use_volume", "HVB · require the volume spike", "bool", True, None, None, None,
                  "Off = v2 behaviour (RSI travel only). On = a break without volume is not an HVB."),
    ]

    # v2 defaults relaxed where volume now does the work.
    _OVERRIDES = {"hvb_step": 2.0}

    @classmethod
    def resolve_params(cls, overrides: dict[str, Any] | None) -> dict[str, Any]:
        """Defaults relaxations accumulate down the version chain.

        Each version only declares what *it* changes (``_OVERRIDES``), and the
        whole chain is applied oldest-first. Without this a newer version would
        silently drop an older version's relaxation (v5 lost v4's HVB changes).
        """
        resolved = super().resolve_params(overrides)
        merged: dict[str, Any] = {}
        for klass in reversed(cls.__mro__):
            merged.update(getattr(klass, "_OVERRIDES", None) or {})
        for key, value in merged.items():
            if not overrides or key not in overrides:
                resolved[key] = value
        return resolved

    # ------------------------------------------------------------- prepare
    def prepare(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        # v2 already did TDI + swings + structure/regime; only HVB changes here,
        # and the regime columns do not depend on it.
        data = super().prepare(df, params)

        volume = data["volume"].astype(float)
        if "volume_ratio" in data.columns:
            ratio = data["volume_ratio"].astype(float)
        else:
            average = volume.rolling(int(params["hvb_volume_len"]), min_periods=5).mean()
            ratio = volume / average.replace(0, float("nan"))
            data["volume_ratio"] = ratio

        data["hvb_volume_ratio"] = ratio
        heavy = (ratio >= float(params["hvb_volume_mult"])).fillna(False)

        purple = data["tdi_fast"]
        up_cross = ta.crossed_above(purple, data["tdi_mbl"])
        down_cross = ta.crossed_below(purple, data["tdi_mbl"])

        strong_up = data["hvb_travel"] >= float(params["hvb_step"])
        strong_down = data["hvb_travel"] <= -float(params["hvb_step"])
        clear_up = data["hvb_margin_up"] >= float(params["hvb_margin"])
        clear_down = data["hvb_margin_down"] >= float(params["hvb_margin"])

        if params.get("hvb_use_volume", True):
            data["hvb_up"] = (up_cross & strong_up & clear_up & heavy).fillna(False)
            data["hvb_down"] = (down_cross & strong_down & clear_down & heavy).fillna(False)
        else:
            data["hvb_up"] = (up_cross & strong_up & clear_up).fillna(False)
            data["hvb_down"] = (down_cross & strong_down & clear_down).fillna(False)
        return data

    # --------------------------------------------------------------- rules
    def rules(self, data: pd.DataFrame, params: dict[str, Any]) -> TriggerSet:
        # v2's gating logic, applied to the volume-confirmed HVB columns.
        return TdiShift.rules(self, data, params)

    # ------------------------------------------------------------- checks
    def checks(self, data: pd.DataFrame, params: dict[str, Any]) -> list[RuleCheck]:
        checks = TdiShift.checks(self, data, params)
        row = data.iloc[-1]
        ratio = row.get("hvb_volume_ratio")
        try:
            ratio_text = f"{float(ratio):.2f}x average"
        except (TypeError, ValueError):
            ratio_text = "-"
        checks.insert(
            2,
            RuleCheck(
                f"HVB volume ≥ {float(params['hvb_volume_mult']):g}x average",
                bool(ratio is not None and pd.notna(ratio) and float(ratio) >= float(params["hvb_volume_mult"])),
                ratio_text,
            ),
        )
        return checks
