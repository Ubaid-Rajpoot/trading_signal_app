"""YOUR STRATEGY GOES HERE.

This file is a working, fully-registered template. Replace the body of
``prepare`` (indicator columns) and ``rules`` (entry/exit triggers) with your
own edges; nothing else in the app needs to change. Restart the API and your
strategy appears in the dashboard selector and the backtester.

The three rules of this framework
---------------------------------
1. ``prepare`` receives OHLCV (columns: time, open, high, low, close, volume)
   and must return the same frame with extra indicator columns.
2. ``rules`` returns a ``TriggerSet`` of four boolean Series on the same index:
   long_entry, long_exit, short_entry, short_exit.
3. Never look at the current bar's close when deciding the current bar's
   signal - use shift(1) for levels, otherwise your backtest will lie to you.

The example below is a "momentum pullback": in an uptrend, buy a dip that has
already started to turn back up, and exit when momentum rolls over.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .. import indicators as ta
from .base import ParamSpec, RuleCheck, Strategy, TriggerSet


class CustomStrategy(Strategy):
    id = "custom"
    name = "My Strategy (template)"
    description = (
        "Editable template in backend/app/core/strategies/custom.py. Default logic: "
        "in a rising trend, buy pullbacks that reclaim the fast EMA while RSI turns up; "
        "exit when RSI rolls over or price loses the slow EMA."
    )
    market = "Crypto spot / perpetual"
    timeframe_hint = "1h"
    allows_short = False

    params = [
        ParamSpec("trend_len", "Trend EMA", "int", 200, 50, 400, 1),
        ParamSpec("fast_len", "Fast EMA", "int", 20, 5, 100, 1),
        ParamSpec("slow_len", "Slow EMA", "int", 50, 10, 200, 1),
        ParamSpec("rsi_len", "RSI length", "int", 14, 5, 50, 1),
        ParamSpec("rsi_buy", "RSI buy zone max", "float", 55.0, 30, 80, 1,
                  "Only buy while RSI is below this level."),
        ParamSpec("rsi_exit", "RSI exit level", "float", 72.0, 50, 95, 1),
        ParamSpec("atr_len", "ATR length", "int", 14, 5, 50, 1),
        ParamSpec("atr_mult", "ATR stop multiple", "float", 2.0, 0.5, 6.0, 0.1),
        ParamSpec("target_r", "Second target (R)", "float", 2.0, 1.0, 8.0, 0.5),
        ParamSpec("long_only", "Long only", "bool", True, None, None, None),
    ]

    plot_lines = [
        {"column": "fast_ema", "label": "Fast EMA", "colour": "#22d3ee"},
        {"column": "slow_ema", "label": "Slow EMA", "colour": "#f59e0b"},
        {"column": "trend_ema", "label": "Trend EMA", "colour": "#8b5cf6"},
    ]

    def prepare(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        data = df.copy()
        data["fast_ema"] = ta.ema(data["close"], int(params["fast_len"]))
        data["slow_ema"] = ta.ema(data["close"], int(params["slow_len"]))
        data["trend_ema"] = ta.ema(data["close"], int(params["trend_len"]))
        data["rsi"] = ta.rsi(data["close"], int(params["rsi_len"]))
        data["atr"] = ta.atr(data, int(params["atr_len"]))
        return data

    def rules(self, data: pd.DataFrame, params: dict[str, Any]) -> TriggerSet:
        # --- your entry conditions -----------------------------------------
        uptrend = (data["close"] > data["trend_ema"]) & (data["fast_ema"] > data["slow_ema"])
        reclaim = ta.crossed_above(data["close"], data["fast_ema"])
        momentum_turn = (data["rsi"] > data["rsi"].shift(1)) & (data["rsi"] < float(params["rsi_buy"]))
        long_entry = uptrend & reclaim & momentum_turn

        # --- your exit conditions ------------------------------------------
        long_exit = (data["rsi"] > float(params["rsi_exit"])) | (data["close"] < data["slow_ema"])

        # --- shorts (optional) ---------------------------------------------
        downtrend = (data["close"] < data["trend_ema"]) & (data["fast_ema"] < data["slow_ema"])
        reject = ta.crossed_below(data["close"], data["fast_ema"])
        short_entry = downtrend & reject
        short_exit = (data["close"] > data["slow_ema"]) | (data["rsi"] < 25)

        if params.get("long_only"):
            short_entry = pd.Series(False, index=data.index)
            short_exit = pd.Series(False, index=data.index)

        return TriggerSet(long_entry, long_exit, short_entry, short_exit)

    def checks(self, data: pd.DataFrame, params: dict[str, Any]) -> list[RuleCheck]:
        row = data.iloc[-1]
        return [
            RuleCheck("Uptrend", bool(row["close"] > row["trend_ema"] and row["fast_ema"] > row["slow_ema"]),
                      f"close {self.fmt_price(row['close'])}, EMA{int(params['trend_len'])} {self.fmt_price(row['trend_ema'])}"),
            RuleCheck("Reclaiming fast EMA", bool(row["close"] > row["fast_ema"]),
                      f"EMA{int(params['fast_len'])} {self.fmt_price(row['fast_ema'])}"),
            RuleCheck("RSI in buy zone", bool(row["rsi"] < float(params["rsi_buy"])),
                      f"RSI {row['rsi']:.1f}"),
        ]

    def required_bars(self, params: dict[str, Any]) -> int:
        return max(int(params["trend_len"]), int(params["slow_len"])) + 30
