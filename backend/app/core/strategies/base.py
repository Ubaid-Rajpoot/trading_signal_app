"""Strategy framework.

A strategy is a pure function from OHLCV history to entry/exit trigger series.
That single contract powers both the live signal cards and the backtester, so
what you see on the dashboard is exactly what gets tested.

To add your own strategy: subclass ``Strategy`` in a new module under this
package, implement ``prepare`` (indicator columns) and ``rules`` (trigger
booleans), then register it in ``__init__.py``. See ``custom.py`` for a
line-by-line template.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

Signal = Literal["LONG", "SHORT", "NEUTRAL"]


@dataclass(frozen=True)
class ParamSpec:
    """Declares one tunable parameter so the UI can render a control for it."""

    key: str
    label: str
    kind: Literal["int", "float", "bool"] = "int"
    default: Any = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    help: str = ""


@dataclass
class RuleCheck:
    """One human-readable rule evaluation shown in the signal breakdown."""

    label: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "passed": self.passed, "detail": self.detail}


@dataclass
class Targets:
    entry: float | None = None
    stop: float | None = None
    take_profits: list[float] = field(default_factory=list)

    def as_dict(self, side: Signal, price_decimals: int = 4) -> dict[str, Any]:
        def r(x: float | None) -> float | None:
            if x is None or (isinstance(x, float) and not np.isfinite(x)):
                return None
            return round(float(x), price_decimals)

        risk = abs(self.entry - self.stop) if self.entry and self.stop else None
        r_multiples = []
        for tp in self.take_profits:
            r_multiples.append(round(abs(tp - self.entry) / risk, 2) if risk else None)
        return {
            "side": side,
            "entry": r(self.entry),
            "stop": r(self.stop),
            "take_profits": [r(tp) for tp in self.take_profits],
            "risk_reward": r_multiples,
            "risk_pct": round(risk / self.entry * 100, 2) if risk and self.entry else None,
        }


@dataclass
class TriggerSet:
    """Boolean triggers aligned to the prepared frame index."""

    long_entry: pd.Series
    long_exit: pd.Series
    short_entry: pd.Series
    short_exit: pd.Series

    def __post_init__(self) -> None:
        for name in ("long_entry", "long_exit", "short_entry", "short_exit"):
            series = getattr(self, name)
            if series is None:
                setattr(self, name, pd.Series(False, index=self.long_entry.index))
            else:
                setattr(self, name, series.reindex(self.long_entry.index).fillna(False).astype(bool))


class Strategy:
    """Base class. Subclasses only need ``prepare`` and ``rules``."""

    id: str = "base"
    name: str = "Base strategy"
    description: str = ""
    market: str = "Crypto spot / perpetual"
    timeframe_hint: str = "1h"
    allows_short: bool = True
    # False = no stop loss / take profit at all; the strategy's own exit trigger
    # is the only way out (used by the TDI + shift setup system).
    uses_stops: bool = True
    params: list[ParamSpec] = []
    plot_lines: list[dict[str, str]] = []  # {column, label, colour}
    pane: dict[str, Any] | None = None  # optional oscillator sub-chart (e.g. TDI)

    # ---------------------------------------------------------------- params
    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {p.key: p.default for p in cls.params}

    @classmethod
    def resolve_params(cls, overrides: dict[str, Any] | None) -> dict[str, Any]:
        resolved = cls.default_params()
        for spec in cls.params:
            if not overrides or spec.key not in overrides:
                continue
            raw = overrides[spec.key]
            try:
                if spec.kind == "bool":
                    value: Any = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
                elif spec.kind == "int":
                    value = int(round(float(raw)))
                else:
                    value = float(raw)
            except (TypeError, ValueError):
                continue
            if spec.min is not None:
                value = max(value, spec.min)
            if spec.max is not None:
                value = min(value, spec.max)
            resolved[spec.key] = value
        return resolved

    @classmethod
    def schema(cls) -> dict[str, Any]:
        return {
            "id": cls.id,
            "name": cls.name,
            "description": cls.description,
            "market": cls.market,
            "timeframe_hint": cls.timeframe_hint,
            "allows_short": cls.allows_short,
            "uses_stops": cls.uses_stops,
            "params": [
                {
                    "key": p.key,
                    "label": p.label,
                    "kind": p.kind,
                    "default": p.default,
                    "min": p.min,
                    "max": p.max,
                    "step": p.step,
                    "help": p.help,
                }
                for p in cls.params
            ],
            "defaults": cls.default_params(),
            "plot_lines": cls.plot_lines,
            "pane": cls.pane,
        }

    # ------------------------------------------------------------ interface
    def prepare(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        raise NotImplementedError

    def rules(self, data: pd.DataFrame, params: dict[str, Any]) -> TriggerSet:
        raise NotImplementedError

    def targets(self, data: pd.DataFrame, params: dict[str, Any], side: Signal) -> Targets:
        """Default: ATR stop + ladder of R-multiple targets."""
        if "atr" not in data.columns or data.empty:
            return Targets()
        row = data.iloc[-1]
        price = float(row["close"])
        atr = float(row["atr"]) if np.isfinite(row["atr"]) else price * 0.01
        mult = float(params.get("atr_mult", 2.0))
        rr = float(params.get("target_r", 2.0))
        ladder = params.get("targets", [1.0, rr, rr * 1.6])
        if side == "LONG":
            stop = price - mult * atr
            tps = [price + m * mult * atr for m in ladder]
        elif side == "SHORT":
            stop = price + mult * atr
            tps = [price - m * mult * atr for m in ladder]
        else:
            return Targets()
        return Targets(entry=price, stop=stop, take_profits=tps)

    def checks(self, data: pd.DataFrame, params: dict[str, Any]) -> list[RuleCheck]:
        """Optional last-bar rule diagnostics; overridden per strategy."""
        return []

    def required_bars(self, params: dict[str, Any]) -> int:
        return 120

    @staticmethod
    def fmt_price(value: Any) -> str:
        """Human-friendly price for rule descriptions (never scientific notation)."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return "-"
        if not np.isfinite(v):
            return "-"
        if abs(v) >= 1000:
            return f"{v:,.2f}"
        if abs(v) >= 10:
            return f"{v:,.3f}"
        if abs(v) >= 1:
            return f"{v:.4f}"
        return f"{v:.6f}"
