"""EMA crossover trend strategy - a second, faster-trading option."""

from __future__ import annotations

from typing import Any

import pandas as pd

from .. import indicators as ta
from .base import ParamSpec, RuleCheck, Strategy, TriggerSet


class EmaTrend(Strategy):
    id = "ema_trend"
    name = "EMA Trend Cross"
    description = (
        "Fast EMA crossing a slow EMA in the direction of a long-term EMA. RSI acts "
        "as an over-extension filter so you do not chase exhausted moves. Exit on "
        "the opposite cross."
    )
    market = "Crypto spot / perpetual"
    timeframe_hint = "1h"
    allows_short = True

    params = [
        ParamSpec("fast", "Fast EMA", "int", 21, 3, 100, 1),
        ParamSpec("slow", "Slow EMA", "int", 55, 10, 250, 1),
        ParamSpec("trend_len", "Macro EMA", "int", 200, 50, 400, 1,
                  "Only trade in the direction of this EMA."),
        ParamSpec("rsi_len", "RSI length", "int", 14, 5, 50, 1),
        ParamSpec("rsi_ceiling", "RSI ceiling for longs", "float", 72.0, 50, 95, 1),
        ParamSpec("rsi_floor", "RSI floor for shorts", "float", 28.0, 5, 50, 1),
        ParamSpec("atr_len", "ATR length", "int", 14, 5, 50, 1),
        ParamSpec("atr_mult", "ATR stop multiple", "float", 2.0, 0.5, 6.0, 0.1),
        ParamSpec("target_r", "Second target (R)", "float", 2.0, 1.0, 8.0, 0.5),
        ParamSpec("long_only", "Long only", "bool", False, None, None, None),
    ]

    plot_lines = [
        {"column": "fast_ema", "label": "Fast EMA", "colour": "#22d3ee"},
        {"column": "slow_ema", "label": "Slow EMA", "colour": "#f59e0b"},
        {"column": "trend_ema", "label": "Macro EMA", "colour": "#8b5cf6"},
    ]

    def prepare(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        data = df.copy()
        data["fast_ema"] = ta.ema(data["close"], int(params["fast"]))
        data["slow_ema"] = ta.ema(data["close"], int(params["slow"]))
        data["trend_ema"] = ta.ema(data["close"], int(params["trend_len"]))
        data["rsi"] = ta.rsi(data["close"], int(params["rsi_len"]))
        data["atr"] = ta.atr(data, int(params["atr_len"]))
        return data

    def rules(self, data: pd.DataFrame, params: dict[str, Any]) -> TriggerSet:
        macro_up = data["close"] > data["trend_ema"]
        macro_down = data["close"] < data["trend_ema"]

        long_cross = ta.crossed_above(data["fast_ema"], data["slow_ema"])
        short_cross = ta.crossed_below(data["fast_ema"], data["slow_ema"])

        long_entry = long_cross & macro_up & (data["rsi"] < float(params["rsi_ceiling"]))
        short_entry = short_cross & macro_down & (data["rsi"] > float(params["rsi_floor"]))

        long_exit = ta.crossed_below(data["fast_ema"], data["slow_ema"])
        short_exit = ta.crossed_above(data["fast_ema"], data["slow_ema"])

        if params.get("long_only"):
            short_entry = pd.Series(False, index=data.index)
            short_exit = pd.Series(False, index=data.index)

        return TriggerSet(long_entry, long_exit, short_entry, short_exit)

    def checks(self, data: pd.DataFrame, params: dict[str, Any]) -> list[RuleCheck]:
        row = data.iloc[-1]
        return [
            RuleCheck("Fast EMA above slow EMA", bool(row["fast_ema"] > row["slow_ema"]),
                      f"EMA{int(params['fast'])} {self.fmt_price(row['fast_ema'])} vs EMA{int(params['slow'])} {self.fmt_price(row['slow_ema'])}"),
            RuleCheck("Price above macro EMA", bool(row["close"] > row["trend_ema"]),
                      f"close {self.fmt_price(row['close'])} vs EMA {self.fmt_price(row['trend_ema'])}"),
            RuleCheck("RSI not over-extended", bool(row["rsi"] < float(params["rsi_ceiling"])),
                      f"RSI {row['rsi']:.1f}"),
        ]

    def required_bars(self, params: dict[str, Any]) -> int:
        return max(int(params["trend_len"]), int(params["slow"])) + 30
