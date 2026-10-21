"""TDI + Structural Shift, version 2.0 - structure & trend filtered.

Same TDI rule as v1, plus the trader's refinement:

    "Ye setup external ya internal structure ke andar ban jaye to valid hai.
     Agar range me bane, ya jahan PDF ke mutabiq build-up ho raha hai - liquidity
     ban rahi hai - wahan ye kaam nahi karta. Market trendy ho, ranging na ho."

So a v2 entry needs **all** of the following on the same candle:

1. **HVB** - purple RSI line breaks the base line with speed (unchanged).
2. **SHIFT / MS** - price closes through a swing level (unchanged).
3. **Structure scope** - that structure sits inside the external range built by
   the last major swing high/low, and price is inside it too. The setup is
   therefore *inside* external or internal structure, never out in empty space.
4. **Trend** - the external trend (direction of the last major break) agrees
   with the trade: longs only in an uptrend, shorts only in a downtrend.
5. **Not ranging** - a fresh major break within ``max_bos_age`` bars, ADX above
   ``adx_min`` and an external range at least ``min_range_atr`` ATR tall.
6. **No liquidity build-up** - no tight cluster of equal highs/lows sitting in
   front of the trade, where price usually sweeps instead of continuing.

Exits are unchanged: the purple line crossing back through the base line.
Still no stop loss and no take profit - this reports setups, not targets.

v1 (``tdi_shift_v1``) is kept untouched for comparison; this module only adds
filters, it never relaxes one.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .. import market_structure as ms
from .base import ParamSpec, RuleCheck, Strategy, Targets, TriggerSet
from .tdi_shift_v1 import TdiShiftV1


class TdiShift(Strategy):
    id = "tdi_shift_v2"
    name = "TDI + Shift · v2 (trend + structure)"
    version = "2.0"
    version_note = (
        "v1 rules plus the market-regime filter: setups only inside external or "
        "internal structure, only with the external trend, never inside a range, "
        "never into a liquidity build-up."
    )
    supersedes = "tdi_shift_v1"
    description = (
        "TDIGM confluence (HVB + same-candle SHIFT/MS) filtered by market structure: "
        "the setup must form inside external or internal structure, in the direction "
        "of the external trend, in a trending market - not in a range and not into a "
        "liquidity build-up."
    )
    market = "Crypto spot / perpetual"
    timeframe_hint = "15m"
    allows_short = True
    uses_stops = False  # no SL/TP: exit only when the purple line crosses back

    # Everything from v1 (TDI + HVB + structure) ...
    params = list(TdiShiftV1.params) + [
        # ---- market regime (v2) --------------------------------------------
        ParamSpec("require_trend", "V2 · trade with the external trend", "bool", True, None, None, None,
                  "Longs only while the last major break was up, shorts only while it was down."),
        ParamSpec("reject_ranging", "V2 · block ranging markets", "bool", True, None, None, None,
                  "A stale break, weak ADX or a compressed range means chop - no setups there."),
        ParamSpec("require_structure", "V2 · setup must sit inside structure", "bool", True, None, None, None,
                  "The broken swing and price must both be inside the external range."),
        ParamSpec("reject_liquidity", "V2 · avoid liquidity build-ups", "bool", True, None, None, None,
                  "Clusters of equal highs/lows are swept rather than followed."),
        ParamSpec("filter_mode", "V2 · filter preset", "select", "strict", None, None, None,
                  "strict = only clean trends; balanced = more room; loose = catches more, "
                  "filters still on. Individual thresholds above win over the preset.",
                  options=["strict", "balanced", "loose"]),
        ParamSpec("adx_len", "V2 · ADX length", "int", 14, 5, 50, 1),
        ParamSpec("adx_min", "V2 · minimum ADX", "float", 18.0, 0.0, 50.0, 1.0,
                  "Below this the market is not moving enough to call it a trend."),
        ParamSpec("er_len", "V2 · efficiency window (bars)", "int", 40, 10, 200, 5,
                  "Window for the efficiency ratio: net travel divided by total travel."),
        ParamSpec("er_min", "V2 · min efficiency ratio", "float", 0.2, 0.0, 1.0, 0.01,
                  "Below this the market is chopping, not trending, however tall the range is."),
        ParamSpec("max_bos_age", "V2 · max bars since the last break", "int", 120, 5, 500, 5,
                  "Older than this and the trend is stale = ranging."),
        ParamSpec("min_structure_atr", "V2 · min internal structure (ATR)", "float", 0.4, 0.0, 10.0, 0.1,
                  "Too small and it is noise, not a structure to trade the break of."),
        ParamSpec("max_structure_atr", "V2 · max internal structure (ATR)", "float", 6.0, 0.5, 30.0, 0.5,
                  "Too wide and it is a range, not a compact correction."),
        ParamSpec("liq_lookback", "V2 · liquidity lookback (bars)", "int", 30, 5, 200, 5),
        ParamSpec("liq_tolerance_atr", "V2 · equal level tolerance (ATR)", "float", 0.25, 0.02, 2.0, 0.01,
                  "How close two swings must be to count as the same level."),
        ParamSpec("liq_touches", "V2 · touches = build-up", "int", 3, 2, 6, 1,
                  "This many equal highs/lows in the window counts as resting liquidity."),
    ]

    plot_lines = list(TdiShiftV1.plot_lines)
    pane = TdiShiftV1.pane

    # Presets only move the thresholds - the four rules themselves stay on, so
    # no preset can turn v2 back into "trade anywhere".
    PRESETS: dict[str, dict[str, Any]] = {
        "strict": {
            "adx_min": 18.0, "er_min": 0.20, "max_bos_age": 120,
            "max_structure_atr": 6.0, "liq_touches": 3,
        },
        "balanced": {
            "adx_min": 15.0, "er_min": 0.12, "max_bos_age": 200,
            "max_structure_atr": 8.0, "liq_touches": 4,
        },
        "loose": {
            "adx_min": 12.0, "er_min": 0.08, "max_bos_age": 300,
            "max_structure_atr": 10.0, "liq_touches": 5,
        },
    }

    @classmethod
    def resolve_params(cls, overrides: dict[str, Any] | None) -> dict[str, Any]:
        resolved = super().resolve_params(overrides)
        preset = cls.PRESETS.get(str(resolved.get("filter_mode", "strict")), {})
        # A preset fills in the thresholds the caller did not set explicitly.
        for key, value in preset.items():
            if not overrides or key not in overrides:
                resolved[key] = value
        return resolved

    # ------------------------------------------------------------- prepare
    def prepare(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        data = TdiShiftV1.prepare(self, df, params)
        data = ms.regime_frame(
            data,
            minor_k=int(params["swing_minor"]),
            major_k=int(params["swing_major"]),
            atr_len=int(params["atr_len"]),
            adx_len=int(params["adx_len"]),
            adx_min=float(params["adx_min"]),
            er_len=int(params["er_len"]),
            er_min=float(params["er_min"]),
            max_bos_age=int(params["max_bos_age"]),
            liq_lookback=int(params["liq_lookback"]),
            liq_tolerance_atr=float(params["liq_tolerance_atr"]),
            liq_touches=int(params["liq_touches"]),
            min_structure_atr=float(params["min_structure_atr"]),
            max_structure_atr=float(params["max_structure_atr"]),
        )
        return data

    # --------------------------------------------------------------- rules
    def rules(self, data: pd.DataFrame, params: dict[str, Any]) -> TriggerSet:
        base = TdiShiftV1.rules(self, data, params)

        gate_long = pd.Series(True, index=data.index)
        gate_short = pd.Series(True, index=data.index)

        if params.get("require_trend"):
            gate_long &= data["trend_dir"].fillna(0) > 0
            gate_short &= data["trend_dir"].fillna(0) < 0
        if params.get("reject_ranging"):
            trending = data["regime"].eq(ms.TRENDING).fillna(False)
            gate_long &= trending
            gate_short &= trending
        if params.get("require_structure"):
            gate_long &= data["internal_high_ok"].fillna(False)
            gate_short &= data["internal_low_ok"].fillna(False)
        if params.get("reject_liquidity"):
            gate_long &= ~data["liq_above"].fillna(False)
            gate_short &= ~data["liq_below"].fillna(False)

        return TriggerSet(
            base.long_entry & gate_long,
            base.long_exit,
            base.short_entry & gate_short,
            base.short_exit,
        )

    # ------------------------------------------------------- filter report
    def filter_report(self, data: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
        """Count the setups v1 would have taken and say why v2 skipped them."""
        raw = TdiShiftV1.rules(self, data, params)
        kept = self.rules(data, params)

        raw_up = int((raw.long_entry | raw.short_entry).sum())
        blocked: dict[str, int] = {}
        for bar in (raw.long_entry | raw.short_entry).to_numpy().nonzero()[0]:
            if bool(kept.long_entry.iloc[bar] or kept.short_entry.iloc[bar]):
                continue
            row = data.iloc[bar]
            long_side = bool(raw.long_entry.iloc[bar])
            reasons = []
            if params.get("reject_ranging") and row.get("regime") != ms.TRENDING:
                reasons.append("ranging")
            direction = 1 if long_side else -1
            if params.get("require_trend") and float(row.get("trend_dir") or 0) * direction <= 0:
                reasons.append("against-trend")
            if params.get("require_structure"):
                ok = row.get("internal_high_ok") if long_side else row.get("internal_low_ok")
                if not bool(ok):
                    reasons.append("no-structure")
            if params.get("reject_liquidity"):
                pool = row.get("liq_above") if long_side else row.get("liq_below")
                if bool(pool):
                    reasons.append("liquidity")
            key = " + ".join(reasons) or "other"
            blocked[key] = blocked.get(key, 0) + 1

        kept_count = int((kept.long_entry | kept.short_entry).sum())
        return {
            "raw": raw_up,
            "taken": kept_count,
            "blocked": blocked,
        }

    # ------------------------------------------------------------- targets
    def targets(self, data: pd.DataFrame, params: dict[str, Any], side) -> Targets:
        return Targets()

    # ------------------------------------------------------------- checks
    def checks(self, data: pd.DataFrame, params: dict[str, Any]) -> list[RuleCheck]:
        checks = TdiShiftV1.checks(self, data, params)
        row = data.iloc[-1]
        regime = row.get("regime") or ms.UNKNOWN
        trend = row.get("trend_dir")

        def num(value, digits=1):
            try:
                value = float(value)
                return f"{value:.{digits}f}" if pd.notna(value) else "-"
            except (TypeError, ValueError):
                return "-"

        checks += [
            RuleCheck(
                "Market trend (external structure)",
                regime == ms.TRENDING,
                f"{regime} · trend {'up' if (trend or 0) > 0 else 'down' if (trend or 0) < 0 else 'none'}"
                f" · ADX {num(row.get('adx'))} · efficiency {num(row.get('efficiency'), 2)}"
                f" · {num(row.get('bars_since_bos'), 0)} bars since last break",
            ),
            RuleCheck(
                "Trade with the trend, not against it",
                bool(row.get("setup_ok_long")) or bool(row.get("setup_ok_short")),
                f"external range {num(row.get('ext_low'), 2)} – {num(row.get('ext_high'), 2)}"
                f" · {num(row.get('bars_since_bos'), 0)} bars since the break",
            ),
            RuleCheck(
                "Setup inside internal structure",
                bool(row.get("internal_structure")),
                f"structure {num(row.get('structure_atr'))} ATR tall"
                f" · minor high {num(row.get('int_high'), 2)} / low {num(row.get('int_low'), 2)}",
            ),
            RuleCheck(
                "No liquidity build-up in the way",
                not bool(row.get("liq_above")) and not bool(row.get("liq_below")),
                "equal highs above" if bool(row.get("liq_above"))
                else "equal lows below" if bool(row.get("liq_below"))
                else "no clustered levels",
            ),
        ]
        return checks

    def required_bars(self, params: dict[str, Any]) -> int:
        return max(
            TdiShiftV1.required_bars(self, params),
            2 * int(params["swing_major"]) + int(params["max_bos_age"]) + 60,
            int(params["liq_lookback"]) + 2 * int(params["swing_minor"]) + 60,
        )
