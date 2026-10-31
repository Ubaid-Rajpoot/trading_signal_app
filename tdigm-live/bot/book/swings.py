"""Swing structure the way the book uses it.

Three degrees, smallest to largest, and three ideas on top of them:

* **break of structure (BOS / MS)** - a *close* through the last unbroken pivot
  (p.37-45). A wick through it is not a break, it is liquidity being taken.
* **sweep (trap / fake break)** - a wick through a pivot that closes back on the
  original side (p.11-13: "the market makers ran the stops, then reversed").
* **strong / weak high & low** - the book's own definition (p.42, p.45): a
  *strong high* is the high that caused manipulation **and** structure breaking;
  a *weak high* is a high the market failed to break. One strong high always
  leaves a weak low, and the draw on liquidity is the opposite weak extreme.

Everything is confirmed with a delay: a pivot of width ``w`` exists only after
``w`` further candles have closed, so a replay can never see it early.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Pivot:
    """A confirmed swing high or low."""

    side: str            # "high" or "low"
    price: float
    index: int           # bar index inside the series that fed us
    time: int            # close time of the pivot bar (ms)
    degree: str          # "minor" | "major" | "macro"
    width: int
    swept: bool = False
    swept_time: int | None = None
    broken: bool = False
    broken_time: int | None = None
    strong: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "price": self.price,
            "time": self.time,
            "degree": self.degree,
            "swept": self.swept,
            "broken": self.broken,
            "strong": self.strong,
        }


@dataclass
class Break:
    """A close through structure, judged by degree."""

    direction: int       # +1 up, -1 down
    level: float
    time: int
    index: int
    degree: str
    kind: str            # "SHIFT" (minor) | "MS" (major) | "MACRO"
    sweep: bool = False  # was a sweep recorded before this break?

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "level": self.level,
            "time": self.time,
            "degree": self.degree,
            "kind": self.kind,
            "sweep": self.sweep,
        }


@dataclass
class Degree:
    """One width of swing, with the pivots that are still in play."""

    name: str
    width: int
    highs: deque[Pivot] = field(default_factory=lambda: deque(maxlen=16))
    lows: deque[Pivot] = field(default_factory=lambda: deque(maxlen=16))
    window: deque = field(default_factory=lambda: deque(maxlen=0))

    def __post_init__(self) -> None:
        self.window = deque(maxlen=self.width * 2 + 1)

    def book(self, side: str) -> deque[Pivot]:
        return self.highs if side == "high" else self.lows

    def latest(self, side: str) -> Pivot | None:
        store = self.book(side)
        return store[-1] if store else None

    def front(self, side: str) -> Pivot | None:
        """The newest pivot on ``side`` that price has not closed through yet."""
        for pivot in reversed(self.book(side)):
            if not pivot.broken:
                return pivot
        return None


DEGREE_KIND = {"minor": "SHIFT", "major": "MS", "macro": "MACRO"}


class Structure:
    """Swing book for one candle series (one symbol, one timeframe)."""

    def __init__(self, widths: dict[str, int] | None = None,
                 max_breaks: int = 64) -> None:
        widths = widths or {"minor": 3, "major": 9, "macro": 21}
        self.degrees = {name: Degree(name, int(w)) for name, w in widths.items()}
        self.index = -1
        self.breaks: deque[Break] = deque(maxlen=max_breaks)
        self.last_sweep: Pivot | None = None
        self.last_sweep_index: int = -10**9
        self.bars = 0
        self._bar: dict | None = None

    # ------------------------------------------------------------------ feed
    def add(self, bar: dict) -> dict[str, list]:
        """Feed one closed candle; return what it confirmed or broke."""
        self.index += 1
        self.bars += 1
        self._bar = bar
        out: dict[str, list] = {"pivots": [], "breaks": [], "sweeps": []}
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        time = int(bar.get("close_time") or bar.get("open_time") or 0)

        for degree in self.degrees.values():
            degree.window.append((high, low, time))
            if len(degree.window) < degree.width * 2 + 1:
                continue
            mid = degree.width
            pivot = self._pivot_at(degree, mid, time)
            if pivot is not None:
                degree.book(pivot.side).append(pivot)
                out["pivots"].append(pivot)

            # ---- liquidity: a wick through the front pivot, close back inside
            for side, extreme, inside in (("high", high, close), ("low", low, close)):
                front = degree.front(side)
                if front is None or front.swept:
                    continue
                took = extreme > front.price if side == "high" else extreme < front.price
                back = inside <= front.price if side == "high" else inside >= front.price
                if took and back:
                    front.swept = True
                    front.swept_time = time
                    self.last_sweep = front
                    self.last_sweep_index = self.index
                    out["sweeps"].append(front)

            # ---- structure: a close through the front pivot on either side
            for direction, side in ((1, "high"), (-1, "low")):
                front = degree.front(side)
                if front is None:
                    continue
                through = close > front.price if direction > 0 else close < front.price
                if not through:
                    continue
                front.broken = True
                front.broken_time = time
                event = Break(
                    direction=direction,
                    level=front.price,
                    time=time,
                    index=self.index,
                    degree=degree.name,
                    kind=DEGREE_KIND[degree.name],
                    sweep=self.last_sweep is not None
                    and self.index - self.last_sweep_index <= degree.width * 2,
                )
                self.breaks.append(event)
                out["breaks"].append(event)
                if event.sweep and self.last_sweep is not None:
                    # p.42: the high that caused manipulation and the break is a
                    # strong high; the low it broke is the weak one to draw to
                    self.last_sweep.strong = True

        return out

    def _pivot_at(self, degree: Degree, mid: int, time: int) -> Pivot | None:
        window = list(degree.window)
        high = window[mid][0]
        low = window[mid][1]
        if high >= max(w[0] for i, w in enumerate(window) if i != mid):
            return Pivot("high", high, self.index - mid, window[mid][2], degree.name, degree.width)
        if low <= min(w[1] for i, w in enumerate(window) if i != mid):
            return Pivot("low", low, self.index - mid, window[mid][2], degree.name, degree.width)
        return None

    # ------------------------------------------------------------------ read
    def trend(self, degree: str = "major") -> int | None:
        """Direction of the last break at this degree (or None)."""
        want = self.degrees.get(degree)
        if want is None:
            return None
        for event in reversed(self.breaks):
            if event.degree == degree:
                return event.direction
        return None

    def last_break(self, degree: str = "major", within: int | None = None) -> Break | None:
        for event in reversed(self.breaks):
            if event.degree != degree:
                continue
            if within is not None and self.index - event.index > within:
                return None
            return event
        return None

    def sequence(self, degree: str = "major") -> dict[str, Any]:
        """HH / HL / LH / LL from the last two pivots on each side."""
        node = self.degrees.get(degree)
        out = {"hh": False, "hl": False, "lh": False, "ll": False, "direction": None}
        if node is None:
            return out
        if len(node.highs) >= 2:
            out["hh"] = node.highs[-1].price > node.highs[-2].price
            out["lh"] = node.highs[-1].price < node.highs[-2].price
        if len(node.lows) >= 2:
            out["hl"] = node.lows[-1].price > node.lows[-2].price
            out["ll"] = node.lows[-1].price < node.lows[-2].price
        if out["hh"] and out["hl"]:
            out["direction"] = 1
        elif out["lh"] and out["ll"]:
            out["direction"] = -1
        return out

    def failure_swing(self, degree: str = "major") -> int | None:
        """p.53-54: the move fails to extend, and the failure is the signal.

        * bullish - no new low, but a new high: the down sequence is over;
        * bearish - no new high, but a new low: the up sequence is over.
        """
        node = self.degrees.get(degree)
        if node is None or len(node.highs) < 2 or len(node.lows) < 2:
            return None
        higher_low = node.lows[-1].price > node.lows[-2].price
        lower_low = node.lows[-1].price < node.lows[-2].price
        higher_high = node.highs[-1].price > node.highs[-2].price
        lower_high = node.highs[-1].price < node.highs[-2].price
        if higher_low and higher_high:
            return 1
        if lower_high and lower_low:
            return -1
        return None

    def range_of(self, degree: str = "major", count: int = 6) -> tuple[float, float]:
        """The dealing range: recent major swing highs and lows."""
        node = self.degrees.get(degree)
        if node is None or not node.highs or not node.lows:
            bar = self._bar or {}
            price = float(bar.get("close") or 0.0)
            return price, price
        highs = [p.price for p in list(node.highs)[-count:]]
        lows = [p.price for p in list(node.lows)[-count:]]
        return max(highs), min(lows)
