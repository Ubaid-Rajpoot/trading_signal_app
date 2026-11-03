"""Is there a trend, and how deep inside it is this setup?

The trader's first rule is not about the setup at all: **the market has to be
trending.** A range prints the same sweep-then-break shape as a trend, which is
exactly why the shape alone is dangerous. This module answers three questions
for every closed candle, on three degrees of the same price series:

    external   the big swings (``major_len`` bars each side, default 20)
    internal   the swings inside them (``pivot_len``, default 5)
    macro      the degree above (``macro_len``, default 80) - used as context
               when the major degree has not made up its mind yet

and, for one specific setup, a fourth:

    depth      how many layers of structure the setup sits inside

What "trending" means here (exactly):

    bullish  the last two confirmed MAJOR highs are higher than the two before,
             AND the last two confirmed MAJOR lows are higher than the two before
             (higher highs + higher lows, within ``hh_tolerance_atr`` ATR of flat)
    bearish  the mirror image (lower highs + lower lows)
    mixed    neither, which in practice is a range or a transition

That is the trader's own description, not an indicator. ADX and the efficiency
ratio (``bot/regime.py``) are a separate, weaker check that is still available;
this one looks at the actual swing sequence.

BREAK OF STRUCTURE (BOS)
    A *close* beyond the most recent confirmed swing of one side:

        close > last confirmed swing high   ->  bullish BOS (direction +1)
        close < last confirmed swing low    ->  bearish BOS (direction -1)

    A wick through the level is a sweep, not a break: it takes liquidity and
    leaves the structure alone. That distinction is the whole difference between
    the reversal this bot trades and the continuation it must not trade.

DEPTH - the "internal within internal" rule
    Structure nests. A trend leg contains internal legs; if price then prints
    *another* internal break inside the current internal leg, a setup there is
    two layers deep - the trader skips those.

        depth 1   the setup breaks the EXTERNAL structure (an MS, a major swing)
        depth 2   the first internal break inside the current external leg
        depth 3+  an internal break inside an internal break  -> skip

    Counted exactly as: 1 + (number of minor BOS in the trade direction since
    the last external BOS in that direction, excluding the setup's own break).

Nothing here looks ahead: a swing point is only used once ``width`` candles have
closed to its right, and a break is only used once the candle that closed through
it has closed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

DEFAULTS: dict[str, Any] = {
    "minor_len": 5,
    "major_len": 20,
    "macro_len": 80,
    "hh_tolerance_atr": 0.10,
    "external_degree": "major",   # "major" or "macro"
    "pivot_memory": 8,
}

DEGREES = ("minor", "major", "macro")


@dataclass
class Pivot:
    side: str          # "high" or "low"
    price: float
    index: int
    broken: bool = False


@dataclass
class Degree:
    """One degree of swings: its pivots, its break-of-structure and its sequence."""

    name: str
    width: int
    highs: deque = field(default_factory=lambda: deque(maxlen=8))
    lows: deque = field(default_factory=lambda: deque(maxlen=8))
    bos_events: deque = field(default_factory=lambda: deque(maxlen=64))

    def latest(self, side: str) -> Pivot | None:
        book = self.highs if side == "high" else self.lows
        return book[-1] if book else None

    def sequence(self, tolerance: float) -> dict[str, Any]:
        """Higher highs / higher lows (or the mirror) from the last two pivots."""
        out: dict[str, Any] = {
            "hh": False, "hl": False, "lh": False, "ll": False,
            "clean": False, "direction": None, "streak": 0,
            "trend_steps": 0, "directional_ratio": 0.0, "range_like": True,
        }
        if len(self.highs) >= 2:
            now, before = self.highs[-1].price, self.highs[-2].price
            out["hh"] = now > before + tolerance
            out["lh"] = now < before - tolerance
        if len(self.lows) >= 2:
            now, before = self.lows[-1].price, self.lows[-2].price
            out["hl"] = now > before + tolerance
            out["ll"] = now < before - tolerance
        if out["hh"] and out["hl"]:
            out["direction"] = 1
        elif out["lh"] and out["ll"]:
            out["direction"] = -1
        # a *clean* sequence is higher-highs+higher-lows or lower-highs+lower-lows
        out["clean"] = out["direction"] is not None
        # A clear trend needs both sides to progress, repeatedly. Looking only at
        # the latest HH+HL lets one lucky pair at the edge of a range masquerade
        # as a trend, especially on 1m. Keep the legacy max streak for display,
        # but gate on the minimum high/low run and the wider directional ratio.
        runs: list[int] = []
        matches = 0
        total = 0
        for book in (self.highs, self.lows):
            run = 0
            for i in range(len(book) - 1, 0, -1):
                step = book[i].price - book[i - 1].price
                directed = (
                    (out["direction"] == 1 and step > tolerance)
                    or (out["direction"] == -1 and step < -tolerance)
                )
                if directed:
                    run += 1
                else:
                    break
            runs.append(run)
            # Score every remembered step (up to pivot_memory pivots per side),
            # not just the last attractive pair. Production requires 100%, so a
            # visible older oscillation keeps the established-trend path closed;
            # only a fresh full-range shift can establish the new direction.
            start = 1
            for i in range(start, len(book)):
                step = book[i].price - book[i - 1].price
                total += 1
                if (
                    (out["direction"] == 1 and step > tolerance)
                    or (out["direction"] == -1 and step < -tolerance)
                ):
                    matches += 1
        out["streak"] = max(runs, default=0)
        out["trend_steps"] = min(runs, default=0) if len(runs) == 2 else 0
        out["directional_ratio"] = round(matches / total, 3) if total else 0.0
        out["range_like"] = bool(
            out["direction"] is None
            or out["trend_steps"] < 1
            or out["directional_ratio"] < 2 / 3
        )
        return out

    def bos(self, direction: int | None = None) -> dict[str, Any] | None:
        events = [e for e in self.bos_events if direction is None or e["direction"] == direction]
        return events[-1] if events else None


class TrendMap:
    """Incremental multi-degree structure for one instrument/timeframe.

    Fed one closed candle at a time (``add``), exactly like ``Structure``, so the
    live bot and the replay walk the same code.
    """

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        cfg = {**DEFAULTS, **(settings or {})}
        minor = max(1, int(cfg["minor_len"]))
        major = max(minor + 1, int(cfg["major_len"]))
        macro = max(major + 1, int(cfg["macro_len"]))
        self.settings = {**cfg, "minor_len": minor, "major_len": major, "macro_len": macro}
        self.widths = {"minor": minor, "major": major, "macro": macro}
        self.degrees = {name: Degree(name, self.widths[name]) for name in DEGREES}
        for degree in self.degrees.values():
            degree.highs = deque(maxlen=int(cfg.get("pivot_memory") or 8))
            degree.lows = deque(maxlen=int(cfg.get("pivot_memory") or 8))
        self.keep = macro * 3 + 10
        self.highs: deque[float] = deque(maxlen=self.keep)
        self.lows: deque[float] = deque(maxlen=self.keep)
        self.count = 0
        self.atr: float | None = None
        self.last_state: dict[str, Any] = {}

    # ------------------------------------------------------------------ pivots
    def _pivot(self, position: int, width: int) -> tuple[str, float] | None:
        """Is the bar at ``position`` of the window a swing point? Equal ties count."""
        highs, lows = list(self.highs), list(self.lows)
        start = max(0, position - width)
        if highs[position] >= max(highs[start : position + width + 1]):
            return "high", highs[position]
        if lows[position] <= min(lows[start : position + width + 1]):
            return "low", lows[position]
        return None

    def add(self, bar: dict, atr: float | None = None) -> dict[str, Any]:
        """Feed one closed candle and return the trend state after it."""
        self.highs.append(float(bar["high"]))
        self.lows.append(float(bar["low"]))
        close = float(bar["close"])
        if atr is not None and atr > 0:
            self.atr = float(atr)
        index = self.count
        self.count += 1
        tolerance = float(self.settings.get("hh_tolerance_atr") or 0) * (self.atr or 0.0)

        # 1. confirm a swing at each degree once it has `width` bars to its right
        for name, degree in self.degrees.items():
            width = degree.width
            position = len(self.highs) - 1 - width
            if position < width:
                continue
            found = self._pivot(position, width)
            if found is None:
                continue
            side, price = found
            book = degree.highs if side == "high" else degree.lows
            if book and abs(book[-1].price - price) < 1e-12:
                continue          # the same level confirmed twice
            book.append(Pivot(side, price, index - width))

        # 2. break of structure: a CLOSE beyond the most recent confirmed swing
        for name, degree in self.degrees.items():
            for side, direction in (("high", 1), ("low", -1)):
                book = degree.highs if side == "high" else degree.lows
                # Break every remembered pivot the close passed, but publish one
                # BOS for the candle.  This matters when a single displacement
                # clears a recent internal high (108) and the older range edge
                # (110): leaving 108 at the tail or handling only one pivot made
                # the genuine range release invisible or one candle late.
                eligible = [item for item in book if not item.broken and item.index < index]
                crossed = [
                    item for item in eligible
                    if (close > item.price + tolerance if side == "high"
                        else close < item.price - tolerance)
                ]
                if not crossed:
                    continue
                range_edge = (
                    max(item.price for item in book) if side == "high"
                    else min(item.price for item in book)
                )
                range_break = (
                    close > range_edge + tolerance if side == "high"
                    else close < range_edge - tolerance
                )
                for item in crossed:
                    item.broken = True
                # Use the outermost crossed boundary for an actual range release;
                # otherwise retain the newest local structural break.
                pivot = (
                    (max(crossed, key=lambda item: item.price) if side == "high"
                     else min(crossed, key=lambda item: item.price))
                    if range_break else max(crossed, key=lambda item: item.index)
                )
                degree.bos_events.append(
                    {"degree": name, "side": side, "direction": direction, "price": pivot.price,
                     "index": index, "range_edge": range_edge, "range_break": range_break}
                )

        self.last_state = self.state()
        return self.last_state

    # ------------------------------------------------------------------- state
    def _degree_state(self, name: str, index: int) -> dict[str, Any]:
        degree = self.degrees[name]
        tolerance = float(self.settings.get("hh_tolerance_atr") or 0) * (self.atr or 0.0)
        seq = degree.sequence(tolerance)
        up = degree.bos(1)
        down = degree.bos(-1)
        last = max([e for e in (up, down) if e], key=lambda e: e["index"], default=None)
        return {
            "degree": name,
            "width": degree.width,
            "pivots": {"highs": len(degree.highs), "lows": len(degree.lows)},
            "direction": seq["direction"],
            "hh": seq["hh"], "hl": seq["hl"], "lh": seq["lh"], "ll": seq["ll"],
            "clean": seq["clean"],
            "streak": seq["streak"],
            "trend_steps": seq["trend_steps"],
            "directional_ratio": seq["directional_ratio"],
            "range_like": seq["range_like"],
            "bos_direction": last["direction"] if last else None,
            "bos_level": last["price"] if last else None,
            "bos_age": (index - last["index"]) if last else None,
            "bos_range_break": bool(last and last.get("range_break")),
            "bos_range_edge": last.get("range_edge") if last else None,
            "last_high": degree.latest("high").price if degree.latest("high") else None,
            "last_low": degree.latest("low").price if degree.latest("low") else None,
        }

    def state(self) -> dict[str, Any]:
        index = self.count - 1 if self.count else 0
        out = {name: self._degree_state(name, index) for name in DEGREES}
        preferred = str(self.settings.get("external_degree") or "major")
        external = out.get(preferred) or out["major"]
        source = preferred
        # if the external degree cannot make up its mind, look one degree up: a
        # setup inside the internal structure of a bigger trend is still valid
        if external["direction"] is None and out["macro"]["direction"] is not None:
            external = out["macro"]
            source = "macro"
        out["external"] = {**external, "source": source}
        out["internal"] = out["minor"]
        return out

    @property
    def context(self) -> dict[str, Any]:
        return self.last_state or self.state()

    # ------------------------------------------------------------------- depth
    def depth(
        self, side: str, break_kind: str | None = None, index: int | None = None
    ) -> dict[str, Any]:
        """How many layers of structure this setup sits inside.

        ``side`` is the trade side ("LONG"/"SHORT"). Returns a dict so the page can
        print *why*:

            value        1 external, 2 one level of internal, 3+ nested
            label        "external" / "internal" / "internal within internal"
            origin_bos   the external break this internal structure lives inside
            internal_shifts   how many minor breaks already happened before this one
        """
        direction = 1 if str(side).upper().startswith("L") else -1
        at = self.count - 1 if index is None else int(index)
        out: dict[str, Any] = {"value": None, "label": "unknown", "internal_shifts": 0,
                               "origin_bos": None, "direction": direction}
        external = self.degrees["major"].bos(direction) or self.degrees["macro"].bos(direction)
        if external is None:
            return out
        out["origin_bos"] = {"price": external["price"], "index": external["index"],
                             "degree": external["degree"],
                             "age": at - external["index"]}
        if str(break_kind or "").upper() == "MS":
            out["value"] = 1
            out["label"] = "external"
            return out
        # every minor break in the same direction *before* this setup's own break
        shifts = sum(
            1
            for event in self.degrees["minor"].bos_events
            if event["direction"] == direction and external["index"] < event["index"] < at
        )
        out["internal_shifts"] = shifts
        out["value"] = 2 + shifts
        out["label"] = "internal" if shifts == 0 else "internal within internal"
        return out

    def as_dict(self) -> dict[str, Any]:
        return self.state()
