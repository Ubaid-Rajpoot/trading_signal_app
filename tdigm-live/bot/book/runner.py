"""Wire the book to real candles: 1m in, trade plans out.

One object per instrument holds the liquidity map (fed the finest candles),
one roll-up per entry timeframe, and one higher-timeframe structure per entry
timeframe. The live server and the back-test both call ``on_candle`` with closed
1-minute candles, so the plan on the page is the plan in the report.

The higher timeframe is fed **only when its candle closes**; an entry bar sees
the HTF context as it was at that moment, never a candle that completed later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..buckets import Rollup
from .imbalance import premium_discount
from .liquidity import LiquidityMap
from .narrative import BookSettings, Narrative, Plan
from .swings import Structure

DEFAULT_HTF: dict[str, str] = {
    "1m": "15m",
    "3m": "30m",
    "5m": "1h",
    "15m": "4h",
    "30m": "4h",
    "1h": "4h",
    "4h": "1d",
}


@dataclass
class _HigherTimeframe:
    name: str
    rollup: Rollup
    structure: Structure
    bars: int = 0


@dataclass
class SymbolState:
    symbol: str
    liquidity: LiquidityMap
    settings: dict[str, BookSettings] = field(default_factory=dict)
    rollups: dict[str, Rollup] = field(default_factory=dict)
    narratives: dict[str, Narrative] = field(default_factory=dict)
    htf: dict[str, _HigherTimeframe] = field(default_factory=dict)
    contexts: dict[str, dict[str, Any]] = field(default_factory=dict)


class BookRunner:
    """Every instrument, every entry timeframe, one deterministic pipeline."""

    def __init__(self, symbols: Iterable[str], timeframes: Iterable[str],
                 settings: BookSettings, htf_map: dict[str, str] | None = None) -> None:
        self.base = settings
        self.timeframes = list(timeframes)
        self.htf_map = {**DEFAULT_HTF, **(htf_map or {})}
        self.symbols = [s.upper() for s in symbols]
        self.states: dict[str, SymbolState] = {s: self._make_state(s) for s in self.symbols}
        self.plans: list[Plan] = []
        self.traps = 0
        self.armed = 0

    # --------------------------------------------------------------- universe
    def _make_state(self, symbol: str) -> SymbolState:
        state = SymbolState(symbol=symbol, liquidity=LiquidityMap())
        for tf in self.timeframes:
            htf_name = self.base.htf or self.htf_map.get(tf, "4h")
            grade = self.base.target_min_grade
            if grade == "auto":
                # p.5: the draw scales with the entry. An intraday entry is paid
                # by the session/day pools, a 1h+ entry by the weekly monthly ones.
                grade = "major" if tf in ("1h", "4h", "1d") else "medium"
            settings = self.base.replace(entry_tf=tf, htf=htf_name, target_min_grade=grade)
            state.settings[tf] = settings
            state.rollups[tf] = Rollup(tf)
            state.narratives[tf] = Narrative(symbol, settings, state.liquidity)
            if htf_name not in state.htf:
                state.htf[htf_name] = _HigherTimeframe(
                    name=htf_name, rollup=Rollup(htf_name),
                    structure=Structure(settings.widths),
                )
        return state

    def add_symbol(self, symbol: str) -> SymbolState:
        symbol = symbol.upper()
        if symbol not in self.states:
            self.states[symbol] = self._make_state(symbol)
        return self.states[symbol]

    def remove_symbol(self, symbol: str) -> None:
        self.states.pop(symbol.upper(), None)

    # ------------------------------------------------------------------- feed
    def on_candle(self, candle: dict) -> list[dict[str, Any]]:
        """Feed one closed 1m candle; return the plans it completed (as dicts)."""
        symbol = str(candle.get("symbol") or "").upper()
        state = self.states.get(symbol)
        if state is None or not candle.get("closed", True):
            return []
        state.liquidity.add(candle)

        made: list[dict[str, Any]] = []
        # ---- higher timeframes first: the context the entry bars will read
        for node in state.htf.values():
            for htf_bar in node.rollup.add(candle):
                node.structure.add(htf_bar)
                node.bars += 1

        # ---- entry timeframes
        for tf, rollup in state.rollups.items():
            for bar in rollup.add(candle):
                narrative = state.narratives[tf]
                context = self._context(state, tf, float(bar["close"]))
                events = narrative.on_bar(bar, context)
                for event in events:
                    if event["type"] == "trap":
                        self.traps += 1
                    elif event["type"] == "armed":
                        self.armed += 1
                    elif event["type"] == "setup":
                        plan: Plan = event["setup"]
                        self.plans.append(plan)
                        made.append(plan.as_dict())
        return made

    def _context(self, state: SymbolState, tf: str, price: float) -> dict[str, Any]:
        name = state.settings[tf].htf
        node = state.htf.get(name)
        if node is None:
            return {"bias": 0, "premium": 0.5, "htf": name}
        structure = node.structure
        bias = structure.trend("major") or structure.sequence("major").get("direction") or 0
        high, low = structure.range_of("major", 6)
        premium = premium_discount(price, high, low)
        context = {
            "bias": int(bias or 0),
            "premium": round(premium, 4),
            "range_high": high,
            "range_low": low,
            "htf": name,
            "bars": node.bars,
        }
        state.contexts[tf] = context
        return context

    # ------------------------------------------------------------------ reads
    def drops(self) -> dict[str, int]:
        total: dict[str, int] = {}
        for state in self.states.values():
            for narrative in state.narratives.values():
                for reason, count in narrative.drops.items():
                    total[reason] = total.get(reason, 0) + count
        return total

    def state_of(self, symbol: str) -> dict[str, Any] | None:
        state = self.states.get(symbol.upper())
        if state is None:
            return None
        return {
            "symbol": state.symbol,
            "timeframes": [state.narratives[tf].state() for tf in state.narratives],
            "pools": state.liquidity.as_list(10),
            "context": state.contexts,
        }
