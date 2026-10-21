"""Market structure: swing points, liquidity, the sweep, and the reversal break.

This is the drawing the trader sent, in code:

    a swing HIGH is liquidity (stops sit above it)
    -> price pokes through it                      = the liquidity was taken (SWEEP)
    -> price closes through the opposite swing LOW = the reversal is confirmed (BREAK)
    -> only then is there a setup

The mirrored version gives the long side: sweep a swing low, then close above the
swing high that was protecting it.

Both sides are tracked **at the same time**: taking one side's liquidity does not
switch the other side off. That matters, because the classic reversal pattern
takes liquidity on both sides before it turns.

Every level is a *pivot* - a high that no other high in ``pivot_len`` bars on
either side beats. Nothing here looks ahead: a pivot is only known once
``pivot_len`` bars have closed to its right, and a sweep is only known once the
bar that pokes through has closed.

Definitions used by the alerts (all exact, no hidden magic):

    pivot_len           bars on each side of a swing point (default 5)
    sweep               bar.high > level (high level) / bar.low < level (low level)
    sweep invalidation  a later CLOSE beyond the sweep extreme - price accepted
                        the new level, so there is nothing to reverse
    opposite break      close below the most recent intact swing low (after a
                        high sweep) or above the most recent swing high (after a
                        low sweep), within ``sweep_max_wait`` bars of the sweep
    stop                the sweep extreme (just beyond the swept high/low)
    target              "next": the next resting liquidity beyond the entry, or
                        "range": the far extreme of the pre-sweep range (the
                        dashed line / arrow in the drawing)
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any

DEFAULTS: dict[str, Any] = {
    "pivot_len": 5,
    "sweep_max_wait": 60,
    "level_max_age": 180,
    "max_levels": 40,
    "target_mode": "next",
}

SIDES = ("high", "low")


@dataclass
class Level:
    """A swing high/low - i.e. resting liquidity."""

    side: str  # "high" (buy-side) or "low" (sell-side)
    price: float
    index: int
    time: int
    swept: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"side": self.side, "price": self.price, "time": self.time, "swept": self.swept}


@dataclass
class Sweep:
    """A liquidity level that price poked through."""

    side: str  # which liquidity was taken: "high" or "low"
    level: float
    index: int
    time: int
    extreme: float  # the furthest the sweep went (the stop goes just beyond)
    opposite: Level | None = None  # the level that must break to confirm

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "level": self.level,
            "time": self.time,
            "extreme": self.extreme,
            "opposite": self.opposite.as_dict() if self.opposite else None,
        }


@dataclass
class Setup:
    """A confirmed structure setup (the opposite side broke)."""

    side: str  # "SHORT" when buy-side liquidity was swept, "LONG" the other way
    entry: float
    stop: float
    target: float | None
    rr: float | None
    sweep: Sweep
    index: int
    time: int
    broke: Level

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "rr": self.rr,
            "swept_side": self.sweep.side,
            "swept_level": self.sweep.level,
            "sweep_time": self.sweep.time,
            "sweep_extreme": self.sweep.extreme,
            "broke_level": self.broke.price,
            "broke_time": self.broke.time,
            "bars_since_sweep": self.index - self.sweep.index,
        }


class Structure:
    """Swing/pivot state for one instrument on one timeframe.

    Cheap: each closed bar appends one high/low, confirms at most one pivot and
    walks a short list of levels.
    """

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        cfg = {**DEFAULTS, **(settings or {})}
        self.pivot_len = max(1, int(cfg["pivot_len"]))
        self.sweep_max_wait = max(1, int(cfg["sweep_max_wait"]))
        self.level_max_age = max(self.pivot_len * 2, int(cfg["level_max_age"]))
        self.max_levels = max(4, int(cfg["max_levels"]))
        self.target_mode = str(cfg.get("target_mode") or "next")

        keep = self.level_max_age + self.pivot_len + 5
        self.highs: deque[float] = deque(maxlen=keep)
        self.lows: deque[float] = deque(maxlen=keep)
        self.times: deque[int] = deque(maxlen=keep)
        self.closes: deque[float] = deque(maxlen=keep)
        self.count = 0  # absolute index of the next bar
        self.levels: deque[Level] = deque(maxlen=self.max_levels)
        self.sweeps: dict[str, Sweep | None] = {"high": None, "low": None}
        self.last_setup: Setup | None = None
        self.setups = 0
        self.invalidated = 0
        self.events: dict[str, Any] = {}

    # ---------------------------------------------------------------- pivots
    def _pivot_at(self, position: int) -> tuple[str, float] | None:
        """Is the bar at ``position`` a confirmed swing point?"""
        n = self.pivot_len
        highs = list(self.highs)
        lows = list(self.lows)
        start = max(0, position - n)
        window_high = highs[start : position + n + 1]
        # Equal highs are a double top - still liquidity - so ties are allowed;
        # duplicate levels are collapsed by price when they are added.
        if highs[position] >= max(window_high):
            return "high", highs[position]
        window_low = lows[start : position + n + 1]
        if lows[position] <= min(window_low):
            return "low", lows[position]
        return None

    # ----------------------------------------------------------------- add
    def add(self, bar: dict) -> dict[str, Any]:
        """Feed one CLOSED candle; return the raw events it produced."""
        self.highs.append(float(bar["high"]))
        self.lows.append(float(bar["low"]))
        self.closes.append(float(bar["close"]))
        self.times.append(int(bar.get("close_time") or bar.get("open_time") or 0))
        index = self.count
        self.count += 1

        events: dict[str, Any] = {
            "pivot": None,
            "sweep": None,
            "sweeps": [],
            "setup": None,
            "invalidated": False,
        }
        price = float(bar["close"])
        high = float(bar["high"])
        low = float(bar["low"])

        # 1. confirm the pivot `pivot_len` bars back (it now has enough bars to
        #    its right); a freshly confirmed pivot is what makes a level.
        confirm_pos = len(self.highs) - 1 - self.pivot_len
        if confirm_pos >= self.pivot_len:
            found = self._pivot_at(confirm_pos)
            if found is not None:
                side, level_price = found
                duplicate = any(
                    abs(level.price - level_price) < 1e-12 and level.side == side for level in self.levels
                )
                if not duplicate:
                    pivot_time = int(self.times[confirm_pos])
                    self.levels.append(
                        Level(side=side, price=level_price, index=index - self.pivot_len, time=pivot_time)
                    )
                    events["pivot"] = {"side": side, "price": level_price, "time": pivot_time}

        # 2. sweeps: each side is tracked separately, so taking buy-side
        #    liquidity does not stop the bot watching the sell side.
        for side in SIDES:
            if self.sweeps[side] is not None:
                continue
            level = self._sweepable(side, high, low, index)
            if level is None:
                continue
            sweep = Sweep(
                side=side,
                level=level.price,
                index=index,
                time=int(self.times[-1]),
                extreme=high if side == "high" else low,
                opposite=self._opposite_level("low" if side == "high" else "high", level.price),
            )
            level.swept = True
            self.sweeps[side] = sweep
            events["sweeps"].append(sweep)
            if events["sweep"] is None:
                events["sweep"] = sweep

        # 3. a sweep is only a setup if price then REVERSES through the other
        #    side. If instead it closes beyond the sweep extreme, the market
        #    kept going - the level was never defended, so drop it.
        for side in SIDES:
            sweep = self.sweeps[side]
            if sweep is None:
                continue
            previous_extreme = sweep.extreme
            waited = index - sweep.index
            if side == "high":
                sweep.extreme = max(sweep.extreme, high)
                if price > previous_extreme or waited > self.sweep_max_wait:
                    self.sweeps[side] = None
                    self.invalidated += 1
                    events["invalidated"] = True
                elif sweep.opposite is not None and index > sweep.index and price < sweep.opposite.price:
                    events["setup"] = self._make_setup("SHORT", bar, index, sweep)
            else:
                sweep.extreme = min(sweep.extreme, low)
                if price < previous_extreme or waited > self.sweep_max_wait:
                    self.sweeps[side] = None
                    self.invalidated += 1
                    events["invalidated"] = True
                elif sweep.opposite is not None and index > sweep.index and price > sweep.opposite.price:
                    events["setup"] = self._make_setup("LONG", bar, index, sweep)

        self._expire(index)
        self.events = events
        return events

    def _sweepable(self, side: str, high: float, low: float, index: int) -> Level | None:
        """The nearest still-intact level of ``side`` that this bar poked through."""
        for level in reversed(self.levels):
            if level.side != side or level.swept:
                continue
            if index - level.index > self.level_max_age:
                continue
            if side == "high" and high > level.price:
                return level
            if side == "low" and low < level.price:
                return level
        return None

    def _opposite_level(self, side: str, sweep_price: float) -> Level | None:
        """The swing on the other side that has to break for a reversal.

        The most recent intact swing of that side on the far side of the sweep:
        a low below the swept high for a short, a high above the swept low for a
        long.
        """
        for level in reversed(self.levels):
            if level.side != side or level.swept:
                continue
            if side == "low" and level.price < sweep_price:
                return level
            if side == "high" and level.price > sweep_price:
                return level
        return None

    def _expire(self, index: int) -> None:
        while self.levels and index - self.levels[0].index > self.level_max_age:
            self.levels.popleft()

    # --------------------------------------------------------------- levels
    def next_target(self, side: str, entry: float, before: int | None = None) -> Level | None:
        """The liquidity this trade is aiming at - the exit, never a guess.

        ``before`` skips the little swing points printed inside the reversal
        leg: they are the move itself, not a target, and taking them would mean
        a target a few ticks away.
        """
        candidates = [
            level
            for level in self.levels
            if not level.swept and level.price != entry and (before is None or level.index < before)
        ]
        if side == "SHORT":
            below = [level for level in candidates if level.side == "low" and level.price < entry]
            if not below:
                return None
            return min(below, key=lambda l: l.price) if self.target_mode == "range" else max(below, key=lambda l: l.price)
        above = [level for level in candidates if level.side == "high" and level.price > entry]
        if not above:
            return None
        return max(above, key=lambda l: l.price) if self.target_mode == "range" else min(above, key=lambda l: l.price)

    def _make_setup(self, side: str, bar: dict, index: int, sweep: Sweep) -> Setup:
        entry = float(bar["close"])
        stop = sweep.extreme
        target_level = self.next_target(side, entry, before=sweep.index)
        target = target_level.price if target_level else None
        risk = abs(entry - stop)
        reward = abs(target - entry) if target is not None else None
        rr = (reward / risk) if risk > 0 and reward is not None else None
        setup = Setup(
            side=side,
            entry=entry,
            stop=stop,
            target=target,
            rr=round(rr, 2) if rr is not None else None,
            sweep=sweep,
            index=index,
            time=int(self.times[-1]),
            broke=sweep.opposite,  # type: ignore[arg-type]
        )
        self.last_setup = setup
        self.setups += 1
        if sweep.opposite is not None:
            sweep.opposite.swept = True  # that liquidity is spent - no double trade
        self.sweeps[sweep.side] = None  # one setup per sweep
        return setup

    # ----------------------------------------------------------------- view
    @property
    def sweep(self) -> Sweep | None:
        """The most recent live sweep (both sides are tracked)."""
        live = [s for s in self.sweeps.values() if s is not None]
        return max(live, key=lambda s: s.index) if live else None

    def armed(self, price: float | None = None) -> dict[str, Any] | None:
        """What the structure is waiting for right now (drives the UI)."""
        sweep = self.sweep
        if sweep is None:
            return None
        opposite = sweep.opposite
        distance = None
        if price and opposite:
            distance = round((opposite.price - price) / price * 100, 2)
        return {
            "swept_side": sweep.side,
            "swept_level": sweep.level,
            "sweep_time": sweep.time,
            "extreme": sweep.extreme,
            "watching": opposite.price if opposite else None,
            "watching_side": opposite.side if opposite else None,
            "distance_pct": distance,
            "bars": self.count - sweep.index,
            "direction": "SHORT" if sweep.side == "high" else "LONG",
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "levels": len(self.levels),
            "setups": self.setups,
            "invalidated": self.invalidated,
            "sweeps": {side: (s.as_dict() if s else None) for side, s in self.sweeps.items()},
        }


def rr_of(entry: float, stop: float, target: float | None) -> float | None:
    risk = abs(entry - stop)
    if risk <= 0 or target is None:
        return None
    value = abs(target - entry) / risk
    return round(value, 2) if math.isfinite(value) else None
