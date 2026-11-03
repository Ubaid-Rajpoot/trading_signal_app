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
LEG_MODEL = "directional_outer_v1"
TARGET_MODEL = "hvb_bos_leg_extreme_v1"


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
    confirmation_leg: dict[str, Any]
    break_kind: str = "SHIFT"  # "SHIFT" (minor) or "MS" (major, p.53)

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "rr": self.rr,
            "target_model": TARGET_MODEL,
            "leg_target": self.target,
            "leg_target_ms": self.confirmation_leg.get("target_time"),
            "confirmation_leg": self.confirmation_leg,
            "swept_side": self.sweep.side,
            "swept_level": self.sweep.level,
            "swept_count": 1,
            "swept_levels": [self.sweep.boundary.as_dict()] if self.sweep.boundary else [],
            "leg_model": LEG_MODEL,
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
            "invalid_reason": None,
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
        # Exactly one structural high/low may be taken at the selected degree.
        # Several independent/equal pivots, or another pivot printed after an
        # active sweep, are the range/multi-sweep X shape. A lower-high/higher-low
        # nested inside one clean outer LH+LL/HL+HH leg belongs to that one outer
        # degree (the tick drawing), so `_dominant_leg` may absorb only that case.
        for side in SIDES:
            taken_levels = self._taken_levels(side, high, low, index)
            if not taken_levels:
                continue
            self.sweep_count += 1

            active = list(self._candidates[side])
            dominant = self._dominant_leg(side, taken_levels, active, index)
            for level in taken_levels:
                level.swept = True

            if dominant is None:
                # One unpaired pivot is merely spent liquidity. Two independent
                # pivots—or any new pivot reached during an active run—are the X
                # drawings unless one clear outer directional leg contains them.
                if active or len(taken_levels) > 1:
                    self._candidates[side] = []
                    self.invalidated += 1
                    events["invalidated"] = True
                    events["invalid_reason"] = (
                        f"multiple {side}s swept without one clear directional structural leg"
                    )
                continue

            boundary, opposite = dominant
            # Reaching the outer boundary of a clean nested leg upgrades an
            # earlier internal sweep; it does not count the lower-high/higher-low
            # inside that same leg as a second range level. This is the distinction
            # between the user's tick drawing and the contracting X drawing.
            self._candidates[side] = []
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
            events["sweep"] = sweep

        # 3. Extend the stop while no second independent structural pivot is
        # taken, and wait for the exact paired endpoint. A candle may continue
        # beyond the selected outer level; range pivots invalidate in step 2,
        # while directional internals already absorbed into that leg do not.
        # Time expiry also removes it.
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

    def _taken_levels(
        self, side: str, high: float, low: float, index: int
    ) -> list[Level]:
        """Every still-resting pivot of one side crossed by this candle.

        Returning the complete set—not merely the latest level—is what makes the
        single-liquidity rule auditable. Equal-price pivots separated by an
        opposite swing are separate highs/lows and therefore count separately.
        """
        return [
            level
            for level in self.levels
            if (
                level.side == side
                and not level.swept
                and index > level.index
                and index - level.index <= self.level_max_age
                and ((side == "high" and high > level.price) or (side == "low" and low < level.price))
            )
        ]

    def _dominant_leg(
        self,
        side: str,
        taken: list[Level],
        active: list[Sweep],
        index: int,
    ) -> tuple[Level, Level] | None:
        """Select one outer leg only when it genuinely contains the internals.

        A clean H1→L1→LH→LL sequence is one bearish leg at the outer degree. A
        later rise can pass the internal LH while sweeping H1 without becoming
        the multiple-range-high X drawing. The outer pair must contain every
        crossed/active boundary and every active candidate must share its exact
        endpoint. Contracting, equal, or independently newer pivots cannot be
        absorbed this way and therefore remain a multi-sweep invalidation.
        """
        boundaries = [
            *taken,
            *(row.boundary for row in active if row.boundary is not None),
        ]
        active_endpoints = [row.opposite for row in active if row.opposite is not None]
        candidates: list[tuple[Level, Level]] = []
        for boundary, endpoint in self._completed_legs(side, index):
            # An upgrade has to reach this outer boundary now; an already active
            # outer sweep cannot absorb a structural pivot printed afterwards.
            if boundary not in taken:
                continue
            if not all(boundary.index <= level.index < endpoint.index for level in boundaries):
                continue
            if active_endpoints and not all(level is endpoint for level in active_endpoints):
                continue
            candidates.append((boundary, endpoint))
        if not candidates:
            return None
        # Only the earliest containing boundary can enclose every nested one.
        return min(candidates, key=lambda pair: pair[0].index)

    @staticmethod
    def _directional_leg(segment: list[Level], side: str) -> bool:
        """Whether nested pivots form one clear leg rather than a range.

        SHORT/high boundary: all internal highs and lows progress down (LH+LL).
        LONG/low boundary: all internal lows and highs progress up (HL+HH).
        A two-endpoint leg passes naturally. Equal or contracting oscillation
        fails, matching the orange X drawings.
        """
        endpoint_side = "low" if side == "high" else "high"
        if not segment or segment[0].side != side or segment[-1].side != endpoint_side:
            return False
        same = [level.price for level in segment if level.side == side]
        opposite = [level.price for level in segment if level.side == endpoint_side]
        if not same or not opposite:
            return False
        eps = 1e-12
        if side == "high":
            if same[0] <= opposite[-1]:
                return False
            return all(now < before - eps for before, now in zip(same, same[1:])) and all(
                now < before - eps for before, now in zip(opposite, opposite[1:])
            )
        if same[0] >= opposite[-1]:
            return False
        return all(now > before + eps for before, now in zip(same, same[1:])) and all(
            now > before + eps for before, now in zip(opposite, opposite[1:])
        )

    def _completed_legs(self, side: str, index: int) -> list[tuple[Level, Level]]:
        """One furthest intact directional endpoint for each resting boundary.

        Exact pairing is degree-aware: a completed outer leg may contain clean
        directional internal pivots, but never a contraction/range. For every
        boundary, the endpoint is extended only while the entire visible segment
        remains LH+LL (SHORT) or HL+HH (LONG). This preserves one precise outer
        pair without combining unrelated prices.
        """
        levels = list(self.levels)
        endpoint_side = "low" if side == "high" else "high"
        pairs: list[tuple[Level, Level]] = []
        for start, boundary in enumerate(levels):
            if boundary.side != side or boundary.swept:
                continue
            if index <= boundary.index or index - boundary.index > self.level_max_age:
                continue
            endpoint: Level | None = None
            for finish in range(start + 1, len(levels)):
                candidate = levels[finish]
                if candidate.side != endpoint_side or candidate.swept:
                    continue
                if index <= candidate.index or index - candidate.index > self.level_max_age:
                    continue
                if self._directional_leg(levels[start : finish + 1], side):
                    endpoint = candidate
            if endpoint is not None:
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

        # The target belongs to the new displacement leg that starts with the
        # selected sweep and contains the same-candle HVB/body break—not to an
        # unrelated older range and not to an arbitrary R multiple. At structure
        # time this is the leg-so-far; SetupRecord may extend its favorable H/L
        # on later pending candles and freezes it immediately before mitigation.
        first_index = self.count - len(self.highs)
        start = max(0, min(len(self.highs) - 1, sweep.index - first_index))
        highs = list(self.highs)[start:]
        lows = list(self.lows)[start:]
        times = list(self.times)[start:]
        high_pos = max(range(len(highs)), key=lambda pos: highs[pos])
        low_pos = min(range(len(lows)), key=lambda pos: lows[pos])
        leg_high, leg_low = highs[high_pos], lows[low_pos]
        target = leg_low if side == "SHORT" else leg_high
        target_time = times[low_pos] if side == "SHORT" else times[high_pos]
        confirmation_leg = {
            "model": TARGET_MODEL,
            "start_time": sweep.time,
            "break_time": int(self.times[-1]),
            "high": leg_high,
            "high_time": times[high_pos],
            "low": leg_low,
            "low_time": times[low_pos],
            "target_side": "low" if side == "SHORT" else "high",
            "target": target,
            "target_time": target_time,
        }
        risk = abs(entry - stop)
        reward = (entry - target) if side == "SHORT" else (target - entry)
        rr = reward / risk if risk > 0 and reward > 0 else None
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
            confirmation_leg=confirmation_leg,
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
