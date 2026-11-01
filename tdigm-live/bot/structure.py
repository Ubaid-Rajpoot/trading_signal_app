"""Market structure: swing points, liquidity, the sweep, and the reversal break.

This is the drawing the trader sent, in code:

    a swing HIGH is liquidity (stops sit above it)
    -> price pokes through it                      = the liquidity was taken (SWEEP)
    -> price closes through the LOW endpoint of that same leg = reversal confirmed (BREAK)
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

    pivot_len           bars on each side of a MINOR swing point (default 5)
    major_len           bars on each side of a MAJOR swing point (default 20);
                        a break of a minor swing is a SHIFT, of a major one the
                        book's MS (p.53), and the last major break gives the
                        external trend / the dealing range of p.42
    break_kind          "SHIFT" or "MS", recorded on every setup
    sweep               bar.high > level (high level) / bar.low < level (low level)
    paired leg          SHORT uses a HIGH -> later LOW leg; LONG uses a LOW ->
                        later HIGH leg. The swept and broken levels must be the
                        two endpoints of that same completed leg
    sweep lifetime      the stop follows the run's furthest extreme; the paired
                        candidate expires after ``sweep_max_wait`` bars
    opposite break      close through the paired endpoint of that exact leg,
                        within ``sweep_max_wait`` bars of the sweep
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
    "major_len": 20,
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
    kind: str = "minor"  # "minor" (SHIFT territory) or "major" (MS territory)

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "price": self.price,
            "time": self.time,
            "swept": self.swept,
            "kind": self.kind,
        }


@dataclass
class Sweep:
    """A liquidity level that price poked through.

    The sweep candle's immutable OHLC is retained with the event.  Besides
    making the chart auditable months later, this lets the publication boundary
    prove ``high > swept high`` / ``low < swept low`` instead of trusting that a
    loosely shaped payload happened to contain a timestamp called ``sweep``.
    """

    side: str  # which liquidity was taken: "high" or "low"
    level: float
    index: int
    time: int
    extreme: float  # the furthest the sweep went (the stop goes just beyond)
    opposite: Level | None = None  # the paired endpoint that must break
    boundary: Level | None = None  # the swept endpoint of that same structural leg
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "level": self.level,
            "time": self.time,
            "extreme": self.extreme,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "boundary": self.boundary.as_dict() if self.boundary else None,
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
    break_kind: str = "SHIFT"  # "SHIFT" (minor) or "MS" (major, p.53)

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
            "sweep_open": self.sweep.open,
            "sweep_high": self.sweep.high,
            "sweep_low": self.sweep.low,
            "sweep_close": self.sweep.close,
            # The structural endpoints prove both events belong to one leg:
            # SHORT = high -> low, LONG = low -> high.
            "swept_level_time": self.sweep.boundary.time if self.sweep.boundary else None,
            "broke_level": self.broke.price,
            # The level's own time is useful context, but it is not the break
            # event.  ``break_time`` is the body-close/HVB candle itself.
            "broke_level_time": self.broke.time,
            "break_time": self.time,
            "break_kind": self.break_kind,
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
        self.major_len = max(self.pivot_len, int(cfg.get("major_len") or 20))
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
        self.major_levels: deque[Level] = deque(maxlen=12)
        self.external_trend: int | None = None
        self.last_major_break: dict[str, Any] | None = None
        self._major_count = 0
        # Several completed legs can have their boundary taken before one of
        # their paired endpoints breaks. Keep every candidate; ``sweeps`` is the
        # latest-per-side compatibility view used by the page.
        self._candidates: dict[str, list[Sweep]] = {"high": [], "low": []}
        self.sweeps: dict[str, Sweep | None] = {"high": None, "low": None}
        self.last_setup: Setup | None = None
        self.sweep_count = 0
        self.setups = 0
        self.invalidated = 0
        self.events: dict[str, Any] = {}

    # ---------------------------------------------------------------- pivots
    def _pivot_at(self, position: int, width: int | None = None) -> tuple[str, float] | None:
        """Is the bar at ``position`` a confirmed swing point?"""
        n = self.pivot_len if width is None else max(1, int(width))
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
            "major_pivot": None,
            "major_break": None,
            "sweep": None,
            "sweeps": [],
            "setup": None,
            "invalidated": False,
        }
        price = float(bar["close"])
        high = float(bar["high"])
        low = float(bar["low"])
        # A body break is a transition from the inside to a close beyond the
        # level.  Merely remaining beyond a level that an earlier candle already
        # crossed is not a new break (and used to create a false setup one bar
        # late after a sweep candle closed through both sides).
        previous_close = float(self.closes[-2]) if len(self.closes) >= 2 else price

        # 1. confirm the pivot `pivot_len` bars back (it now has enough bars to
        #    its right); a freshly confirmed pivot is what makes a level.
        confirm_pos = len(self.highs) - 1 - self.pivot_len
        if confirm_pos >= self.pivot_len:
            found = self._pivot_at(confirm_pos)
            if found is not None:
                side, level_price = found
                # Equal prices after an opposite pivot are a new structural leg,
                # not a duplicate. Collapse only adjacent same-side plateau
                # confirmations; global price de-duplication erased leg pairing.
                duplicate = bool(
                    self.levels
                    and self.levels[-1].side == side
                    and abs(self.levels[-1].price - level_price) < 1e-12
                )
                if not duplicate:
                    pivot_time = int(self.times[confirm_pos])
                    self.levels.append(
                        Level(side=side, price=level_price, index=index - self.pivot_len, time=pivot_time)
                    )
                    events["pivot"] = {"side": side, "price": level_price, "time": pivot_time}

        # 1b. major swings (p.53 MS): the same fractal, but a much wider window.
        #     A major pivot can only be confirmed `major_len` bars later.
        major_pos = len(self.highs) - 1 - self.major_len
        if major_pos >= self.major_len and self.major_len > self.pivot_len:
            found_major = self._pivot_at(major_pos, self.major_len)
            if found_major is not None:
                side, level_price = found_major
                duplicate = any(
                    abs(level.price - level_price) < 1e-12 and level.side == side
                    for level in self.major_levels
                )
                if not duplicate:
                    self.major_levels.append(
                        Level(side=side, price=level_price, index=index - self.major_len,
                              time=int(self.times[major_pos]), kind="major")
                    )
                    events["major_pivot"] = {"side": side, "price": level_price}

        # 1c. the latest major break is the external trend; it also defines the
        #     dealing range of p.42 (last major low .. last major high).
        for level in self.major_levels:
            if level.swept:
                continue
            if level.side == "high" and price > level.price:
                level.swept = True
                self.external_trend = 1
                self.last_major_break = {"side": "high", "price": level.price, "index": index}
                events["major_break"] = self.last_major_break
            elif level.side == "low" and price < level.price:
                level.swept = True
                self.external_trend = -1
                self.last_major_break = {"side": "low", "price": level.price, "index": index}
                events["major_break"] = self.last_major_break

        # 2. Completed structural legs, not unrelated nearest pivots.
        #
        # SHORT leg: HIGH -> later LOW. The high must be taken; that exact low
        # must later receive the body close.
        # LONG leg:  LOW  -> later HIGH. The low must be taken; that exact high
        # must later receive the body close.
        #
        # A large run can take several completed legs before reversing, so keep
        # every paired candidate. Selecting a recent high and an unrelated old
        # low is explicitly impossible here.
        existing = {
            (s.side, s.boundary.index if s.boundary else -1,
             s.opposite.index if s.opposite else -1)
            for values in self._candidates.values() for s in values
        }
        for side in SIDES:
            grouped: dict[int, tuple[Level, list[Level]]] = {}
            for boundary, opposite in self._completed_legs(side, index):
                key = (side, boundary.index, opposite.index)
                if key in existing:
                    continue
                bucket = grouped.setdefault(boundary.index, (boundary, []))[1]
                bucket.append(opposite)

            for boundary, opposites in grouped.values():
                taken = high > boundary.price if side == "high" else low < boundary.price
                if not taken:
                    continue
                made: list[Sweep] = []
                for opposite in opposites:
                    sweep = Sweep(
                        side=side,
                        level=boundary.price,
                        index=index,
                        time=int(self.times[-1]),
                        extreme=high if side == "high" else low,
                        opposite=opposite,
                        boundary=boundary,
                        open=float(bar["open"]),
                        high=high,
                        low=low,
                        close=price,
                    )
                    self._candidates[side].append(sweep)
                    events["sweeps"].append(sweep)
                    made.append(sweep)
                if made:
                    boundary.swept = True
                    self.sweep_count += 1
                    latest = max(made, key=lambda row: row.opposite.index if row.opposite else -1)
                    if events["sweep"] is None or (
                        latest.opposite is not None
                        and events["sweep"].opposite is not None
                        and latest.opposite.index > events["sweep"].opposite.index
                    ):
                        events["sweep"] = latest

        # 3. Extend the stop to the run's final extreme and wait for the paired
        # endpoint. A close above an earlier swept high is not enough to kill the
        # candidate: the supplied BTC example deliberately runs through that old
        # high before reversing. Time expiry or consumption of the paired level
        # removes it; a true inside->outside body close confirms it.
        for side in SIDES:
            survivors: list[Sweep] = []
            broken: list[Sweep] = []
            for sweep in self._candidates[side]:
                waited = index - sweep.index
                if side == "high":
                    sweep.extreme = max(sweep.extreme, high)
                else:
                    sweep.extreme = min(sweep.extreme, low)

                if waited > self.sweep_max_wait or sweep.opposite is None:
                    self.invalidated += 1
                    events["invalidated"] = True
                    continue
                crossed = (
                    index > sweep.index
                    and (
                        (side == "high" and previous_close >= sweep.opposite.price and price < sweep.opposite.price)
                        or (side == "low" and previous_close <= sweep.opposite.price and price > sweep.opposite.price)
                    )
                )
                if crossed:
                    broken.append(sweep)
                else:
                    survivors.append(sweep)

            self._candidates[side] = survivors
            if broken:
                # If one displacement crosses several old endpoints, the latest
                # completed leg is the active structure. Every crossed endpoint
                # is consumed, but only that paired leg becomes the setup.
                chosen = max(broken, key=lambda row: row.opposite.index if row.opposite else -1)
                for row in broken:
                    if row.opposite is not None:
                        row.opposite.swept = True
                if events["setup"] is None:
                    events["setup"] = self._make_setup(
                        "SHORT" if side == "high" else "LONG", bar, index, chosen
                    )

        self._sync_sweeps()
        self._expire(index)
        self.events = events
        return events

    def _completed_legs(self, side: str, index: int) -> list[tuple[Level, Level]]:
        """Completed, intact legs whose boundary on ``side`` can be swept.

        The endpoint chooses its nearest preceding opposite pivot. This is the
        missing relation in the old detector: price alone ("some low below this
        high") cannot say that two pivots belong to one leg.
        """
        levels = list(self.levels)
        endpoint_side = "low" if side == "high" else "high"
        pairs: list[tuple[Level, Level]] = []
        for pos, endpoint in enumerate(levels):
            if endpoint.side != endpoint_side or endpoint.swept:
                continue
            if index <= endpoint.index or index - endpoint.index > self.level_max_age:
                continue
            boundary = next(
                (level for level in reversed(levels[:pos]) if level.side == side),
                None,
            )
            if boundary is None or boundary.swept:
                continue
            if index - boundary.index > self.level_max_age:
                continue
            if side == "high" and boundary.price <= endpoint.price:
                continue
            if side == "low" and boundary.price >= endpoint.price:
                continue
            pairs.append((boundary, endpoint))
        return pairs

    def _sync_sweeps(self) -> None:
        """Expose the latest active paired candidate on each side to the UI."""
        for side in SIDES:
            values = self._candidates[side]
            self.sweeps[side] = max(
                values,
                key=lambda row: (row.index, row.opposite.index if row.opposite else -1),
                default=None,
            )

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
            break_kind="MS" if getattr(sweep.opposite, "kind", "minor") == "major" else "SHIFT",
        )
        self.last_setup = setup
        self.setups += 1
        if sweep.opposite is not None:
            sweep.opposite.swept = True  # that liquidity is spent - no double trade
            # A break candle can also wick through this endpoint and momentarily
            # look like a sweep in the other direction. Once it is the confirmed
            # break of this setup, no candidate may reuse it as either endpoint.
            for candidate_side in SIDES:
                self._candidates[candidate_side] = [
                    row for row in self._candidates[candidate_side]
                    if row.boundary is not sweep.opposite and row.opposite is not sweep.opposite
                ]
        self.sweeps[sweep.side] = None  # refreshed from remaining candidates below
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

    def range(self) -> dict[str, float] | None:
        """p.42 dealing range: the last major low .. the last major high."""
        highs = [l for l in self.major_levels if l.side == "high"]
        lows = [l for l in self.major_levels if l.side == "low"]
        if not highs or not lows:
            return None
        high = max(highs, key=lambda l: l.index)
        low = max(lows, key=lambda l: l.index)
        if high.price <= low.price:
            return None
        return {
            "high": high.price,
            "low": low.price,
            "high_time": high.time,
            "low_time": low.time,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "levels": len(self.levels),
            "major_levels": len(self.major_levels),
            "external_trend": self.external_trend,
            "break_kind": self.last_setup.break_kind if self.last_setup else None,
            "sweep_count": self.sweep_count,
            "setups": self.setups,
            "invalidated": self.invalidated,
            "sweeps": {side: (s.as_dict() if s else None) for side, s in self.sweeps.items()},
            "range": self.range(),
        }


def rr_of(entry: float, stop: float, target: float | None) -> float | None:
    risk = abs(entry - stop)
    if risk <= 0 or target is None:
        return None
    value = abs(target - entry) / risk
    return round(value, 2) if math.isfinite(value) else None
