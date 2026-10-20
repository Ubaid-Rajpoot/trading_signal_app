"""Donchian channel breakout - the default strategy.

Classic turtle-style trend following, tuned for crypto:

* Trend filter: price above a slow EMA and that EMA sloping up.
* Entry: close breaks the highest high of the previous ``channel`` bars
  (the current bar is excluded so the trigger is causal).
* Exit: close breaks the lowest low of the previous ``exit_channel`` bars.
* Risk: ATR stop, laddered R-multiple targets.
* Optional ADX filter to skip chop.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .. import indicators as ta
from .base import ParamSpec, RuleCheck, Strategy, TriggerSet


class DonchianBreakout(Strategy):
    id = "donchian"
    name = "Donchian Breakout"
    description = (
        "Turtle-style trend following: buy a close above the prior N-bar high while "
        "price is above a rising slow EMA, exit on an M-bar low break. ATR-based "
        "stop and 1R / 2R / 3.2R targets."
    )
    market = "Crypto spot / perpetual"
    timeframe_hint = "4h"
    allows_short = True

    params = [
        ParamSpec("channel", "Breakout channel (bars)", "int", 20, 5, 100, 1,
                  "Lookback for the breakout high/low."),
        ParamSpec("exit_channel", "Exit channel (bars)", "int", 10, 3, 60, 1,
                  "Lookback for the opposite-side exit."),
        ParamSpec("trend_len", "Trend EMA length", "int", 100, 20, 400, 1,
                  "Slow EMA used as the trend filter."),
        ParamSpec("adx_len", "ADX length", "int", 14, 5, 50, 1, "Trend-strength filter length."),
        ParamSpec("adx_min", "Minimum ADX", "float", 18.0, 0, 45, 1,
                  "Skip signals in choppy markets (0 disables)."),
        ParamSpec("atr_len", "ATR length", "int", 14, 5, 50, 1),
        ParamSpec("atr_mult", "ATR stop multiple", "float", 2.0, 0.5, 6.0, 0.1),
        ParamSpec("target_r", "Second target (R)", "float", 2.0, 1.0, 8.0, 0.5),
        ParamSpec("long_only", "Long only", "bool", False, None, None, None,
                  "Disable shorts (for spot-only accounts)."),
    ]

    plot_lines = [
        {"column": "channel_high", "label": "Donchian high", "colour": "#f59e0b"},
        {"column": "channel_low", "label": "Donchian low", "colour": "#f59e0b"},
        {"column": "trend_ema", "label": "Trend EMA", "colour": "#8b5cf6"},
    ]

    def prepare(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        data = df.copy()
        channel = int(params["channel"])
        exit_channel = int(params["exit_channel"])

        # shift(1) keeps the breakout level strictly in the past.
        data["channel_high"] = data["high"].rolling(channel, min_periods=channel).max().shift(1)
        data["channel_low"] = data["low"].rolling(channel, min_periods=channel).min().shift(1)
        data["exit_high"] = data["high"].rolling(exit_channel, min_periods=exit_channel).max().shift(1)
        data["exit_low"] = data["low"].rolling(exit_channel, min_periods=exit_channel).min().shift(1)
        data["trend_ema"] = ta.ema(data["close"], int(params["trend_len"]))
        data["ema_slope"] = data["trend_ema"].diff(3)
        data["atr"] = ta.atr(data, int(params["atr_len"]))
        data["adx"] = ta.adx(data, int(params["adx_len"]))
        return data

    def rules(self, data: pd.DataFrame, params: dict[str, Any]) -> TriggerSet:
        trend_up = (data["close"] > data["trend_ema"]) & (data["ema_slope"] > 0)
        trend_down = (data["close"] < data["trend_ema"]) & (data["ema_slope"] < 0)
        adx_min = float(params["adx_min"])
        adx_ok = data["adx"] >= adx_min if adx_min > 0 else pd.Series(True, index=data.index)

        long_entry = (data["close"] > data["channel_high"]) & trend_up & adx_ok
        short_entry = (data["close"] < data["channel_low"]) & trend_down & adx_ok

        long_exit = data["close"] < data["exit_low"]
        short_exit = data["close"] > data["exit_high"]

        if params.get("long_only"):
            short_entry = pd.Series(False, index=data.index)
            short_exit = pd.Series(False, index=data.index)

        return TriggerSet(long_entry, long_exit, short_entry, short_exit)

    def checks(self, data: pd.DataFrame, params: dict[str, Any]) -> list[RuleCheck]:
        row = data.iloc[-1]
        trend_up = bool(row["close"] > row["trend_ema"] and row["ema_slope"] > 0)
        trend_down = bool(row["close"] < row["trend_ema"] and row["ema_slope"] < 0)
        adx_min = float(params["adx_min"])
        adx_ok = bool(row["adx"] >= adx_min) if adx_min > 0 else True
        return [
            RuleCheck("Price above rising trend EMA", trend_up,
                      f"close {self.fmt_price(row['close'])} vs EMA {self.fmt_price(row['trend_ema'])}"),
            RuleCheck("Price below falling trend EMA", trend_down,
                      f"close {self.fmt_price(row['close'])} vs EMA {self.fmt_price(row['trend_ema'])}"),
            RuleCheck(f"ADX >= {adx_min:g}", adx_ok, f"ADX {row['adx']:.1f}"),
            RuleCheck("Channel breakout",
                      bool(row["close"] > row["channel_high"] or row["close"] < row["channel_low"]),
                      f"channel {self.fmt_price(row['channel_low'])} to {self.fmt_price(row['channel_high'])}"),
        ]

    def required_bars(self, params: dict[str, Any]) -> int:
        return max(int(params["trend_len"]), int(params["channel"]), int(params["adx_len"])) + 30
