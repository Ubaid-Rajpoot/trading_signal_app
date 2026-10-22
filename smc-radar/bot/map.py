"""The market map: swings, liquidity, strong / very strong levels, algo candles,
rejection blocks - and the mitigation life-cycle of every zone.

Every rule here comes from *Advanced ICT Institutional SMC Trading* (David
Woods). The page numbers are in the docstrings so the code can always be
checked against the book, and `docs/book-notes.md` quotes each passage.

    p.42   Strong High = the High that caused Manipulation and Structure
           Breaking.  Weak Low = the Low that failed to break structure.

    p.45   VERY STRONG HIGH = HIGH GRAB LIQUIDITY (HIGH CAUSED MANIPULATION)
           BREAK STRONG HIGH + HAVE AN INDUCEMENT

    p.48   WHEN A HIGH OR A LOW DEVELOPS WEAKNESS THE STRONG H/L BECOMES WEAK
           MITIGATION WHEN THE PRICE IS MITIGATED.

    p.67   AC = ALGO CANDLE - "the candle is what develops when it absorbs
           ... and it forms FVG immediately afterwards"

    p.68   AC IS WHAT MAKES THE HIGH OR LOW STRONG

    p.69   VERY STRONG ALGO CANDLE = GRAB LIQUIDITY / BREAK STRONG HIGH OR LOW
           (MOMENTUM SHIFT) HAVE AN INDUCEMENT / FVG

    p.88   HTF REJECTION BLOCK IS A LTF ALGO CANDLE
    p.91   STRONG REJECTION BLOCK = HAVE AN INDUCEMENT + FORMS ON A SESSION
           (HIGH OR LOW)

So the words mean, exactly:

    sweep        price trades beyond a pivot high/low - the liquidity is taken
    break        a later CLOSE beyond the opposite pivot - the structure broke
    strong high  a pivot high that was swept and then broke the structure down
                 (mirror: strong low)
    inducement   a small liquidity pool (equal highs/lows) that lures the crowd
                 in before the real move
    algo candle  the last candle at the extreme, the one that absorbed the
                 orders; the displacement out of it leaves an FVG
    very strong  the same thing, but the level it grabbed was itself a strong
                 (major) level, the break is a major one, and there is an
                 inducement + an FVG - all four are recorded as `evidence`
    rejection block  an algo candle that sits on a session high/low
    mitigation   price coming back INTO the zone and filling the orders there
    invalidation price closing beyond the extreme - the strong level became
                 weak, so it is no longer supply/demand
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------- config
DEFAULTS: dict[str, Any] = {
    "pivot_len": 3,          # bars each side of a minor swing  (SHIFT territory)
    "major_len": 12,         # bars each side of a major swing  (MS / external)
    "disp_bars": 3,          # candles the displacement may take to print its FVG
    "min_fvg_atr": 0.25,     # an FVG smaller than this fraction of ATR is noise
    "lookback": 240,         # bars kept for FVG / inducement / session work
    "induce_tol": 0.25,      # equal levels are within this fraction of ATR
    "induce_count": 2,       # this many equal levels = an inducement
    "max_zone_age": 2000,    # bars a zone may stay on the map
    "mitigation_mode": "zone",  # "zone" (touch the block) or "level" (the exact extreme)
    "atr_period": 14,
    "sessions": [            # UTC, inclusive start, exclusive end
        {"name": "Asia", "start": 0, "end": 8},
        {"name": "London", "start": 7, "end": 16},
        {"name": "New York", "start": 12, "end": 21},
    ],
}

HIGH = "high"
LOW = "low"

# The kinds the page can filter on. ``side`` says which liquidity it is about:
# a high zone is supply (sell-side of the range), a low zone is demand.
KINDS = (
    "strong_high",
    "very_strong_high",
    "strong_low",
    "very_strong_low",
    "algo_bear",              # algo candle at a HIGH (bearish displacement)
    "algo_bull",              # algo candle at a LOW
    "very_strong_algo_bear",
    "very_strong_algo_bull",
    "rejection_block_bear",   # algo candle on a session HIGH
    "rejection_block_bull",   # algo candle on a session LOW
)


# ---------------------------------------------------------------------- pieces
@dataclass
class Pivot:
    side: str            # HIGH or LOW
    price: float
    index: int
    time: int
    major: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"side": self.side, "price": self.price, "time": self.time, "major": self.major}


@dataclass
class FairValueGap:
    side: str            # "bearish" (above price) or "bullish" (below price)
    top: float
    bottom: float
    index: int
    time: int

    @property
    def size(self) -> float:
        return abs(self.top - self.bottom)


@dataclass
class Zone:
    """One thing the page draws and keeps drawing until it is resolved.

    ``top``/``bottom`` is the block; ``level`` is the exact high/low the trader
    is watching (the "specific high" that has to be mitigated).
    """

    kind: str
    side: str            # HIGH = supply, LOW = demand
    symbol: str
    timeframe: str
    top: float
    bottom: float
    level: float
    index: int
    time: int
    status: str = "active"        # active | mitigated | invalidated
    strength: int = 1             # how many of the book's conditions were met
    evidence: list[str] = field(default_factory=list)
    swept_level: float | None = None
    break_level: float | None = None
    fvg: dict[str, Any] | None = None
    inducement: int = 0
    session: str | None = None
    mitigated_at: int | None = None
    mitigated_index: int | None = None
    mitigated_price: float | None = None
    invalidated_at: int | None = None
    invalidated_price: float | None = None
    created_index: int = 0

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.timeframe}|{self.kind}|{self.time}|{self.side}"

    @property
    def direction(self) -> str:
        """What the zone is for: a high is where you look for a short."""
        return "SHORT" if self.side == HIGH else "LONG"

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "side": self.side,
            "direction": self.direction,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "top": round(self.top, 8),
            "bottom": round(self.bottom, 8),
            "level": round(self.level, 8),
            "index": self.index,
            "time": self.time,
            "status": self.status,
            "strength": self.strength,
            "evidence": list(self.evidence),
            "swept_level": self.swept_level,
            "break_level": self.break_level,
            "fvg": self.fvg,
            "inducement": self.inducement,
            "session": self.session,
            "mitigated_at": self.mitigated_at,
            "mitigated_price": self.mitigated_price,
            "invalidated_at": self.invalidated_at,
            "invalidated_price": self.invalidated_price,
        }


# ------------------------------------------------------------------- helpers
def true_range_series(bars: list[dict]) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for i in range(1, len(bars)):
        high, low = float(bars[i]["high"]), float(bars[i]["low"])
        prev = float(bars[i - 1]["close"])
        out[i] = max(high - low, abs(high - prev), abs(low - prev))
    return out


def wilder_atr(bars: list[dict], period: int = 14) -> list[float | None]:
    tr = true_range_series(bars)
    out: list[float | None] = [None] * len(bars)
    period = max(1, int(period))
    seeded = 0
    total = 0.0
    running: float | None = None
    for i, value in enumerate(tr):
        if value is None:
            continue
        if running is None:
            total += value
            seeded += 1
            if seeded == period:
                running = total / period
                out[i] = running
            continue
        running = (running * (period - 1) + value) / period
        out[i] = running
    return out


def session_of(time_ms: int, sessions: list[dict]) -> str | None:
    minute = (int(time_ms) // 60_000) % 1440
    for window in sessions or []:
        start, end = int(window["start"]) * 60, int(window["end"]) * 60
        inside = (start <= minute < end) if start < end else (minute >= start or minute < end)
        if inside:
            return str(window.get("name") or "session")
    return None


def equal_levels(values: list[float], tolerance: float) -> int:
    """How many of these extremes sit inside the same ``tolerance`` band."""
    if not values or tolerance <= 0:
        return 0
    values = sorted(values, reverse=True)
    best = 1
    for i, anchor in enumerate(values):
        count = sum(1 for v in values[i:] if abs(anchor - v) <= tolerance)
        best = max(best, count)
    return best


def fair_value_gap(bar0: dict, bar1: dict, bar2: dict, index: int) -> FairValueGap | None:
    """The three-candle imbalance of p.67/p.79 (the displacement's void)."""
    a_high, a_low = float(bar0["high"]), float(bar0["low"])
    c_high, c_low = float(bar2["high"]), float(bar2["low"])
    if a_low > c_high:  # market fell but left a gap above
        return FairValueGap("bearish", top=a_low, bottom=c_high, index=index, time=int(bar2["close_time"]))
    if a_high < c_low:  # market rose and left a gap below
        return FairValueGap("bullish", top=c_low, bottom=a_high, index=index, time=int(bar2["close_time"]))
    return None


# ------------------------------------------------------------------- the map
class MarketMap:
    """One instrument on one timeframe, updated bar by bar.

    The class is deliberately incremental: a bar costs a few dozen operations, so
    a thousand instruments can stream at once.
    """

    def __init__(self, symbol: str, timeframe: str, settings: dict[str, Any] | None = None) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.cfg = {**DEFAULTS, **(settings or {})}
        self.pivot_len = max(1, int(self.cfg["pivot_len"]))
        self.major_len = max(self.pivot_len + 1, int(self.cfg["major_len"]))
        self.disp_bars = max(1, int(self.cfg["disp_bars"]))
        self.keep = max(120, int(self.cfg["lookback"]))
        self.max_age = max(50, int(self.cfg["max_zone_age"]))
        self.mitigation_mode = str(self.cfg.get("mitigation_mode") or "zone")

        self.bars: deque[dict] = deque(maxlen=self.keep)
        self.atr: deque[float | None] = deque(maxlen=self.keep)
        self.period = max(1, int(self.cfg["atr_period"]))
        self._tr_seed: list[float] = []
        self.count = 0

        self.minor: deque[Pivot] = deque(maxlen=64)
        self.major: deque[Pivot] = deque(maxlen=16)
        self.zones: list[Zone] = []

        # live sweep per side: {"pivot": Pivot, "extreme": float, "index": int}
        self.sweeps: dict[str, dict[str, Any] | None] = {HIGH: None, LOW: None}
        self.external_trend: int | None = None
        self.last_events: dict[str, Any] = {}
        self.seen: set[str] = set()

    # ------------------------------------------------------------------ util
    @property
    def last_price(self) -> float | None:
        return float(self.bars[-1]["close"]) if self.bars else None

    def atr_now(self) -> float | None:
        for value in reversed(self.atr):
            if value is not None:
                return value
        return None

    @property
    def trend_label(self) -> str:
        return {1: "bull", -1: "bear", None: "unknown"}[self.external_trend]

    def active(self) -> list[Zone]:
        return [z for z in self.zones if z.status == "active"]

    def _add_zone(self, zone: Zone) -> Zone | None:
        if zone.key in self.seen:
            return None
        self.seen.add(zone.key)
        zone.created_index = self.count
        self.zones.append(zone)
        # keep the list bounded: drop the oldest resolved zone first
        if len(self.zones) > 400:
            for i, candidate in enumerate(self.zones):
                if candidate.status != "active":
                    del self.zones[i]
                    break
        return zone

    # ------------------------------------------------------------------- add
    def add(self, bar: dict) -> dict[str, Any]:
        """Feed one CLOSED candle; return everything that happened on it."""
        self.bars.append(
            {
                "open_time": int(bar["open_time"]),
                "close_time": int(bar.get("close_time") or bar["open_time"]),
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": float(bar.get("volume") or 0.0),
            }
        )
        self._push_atr(bar)
        index = self.count
        self.count += 1

        events: dict[str, Any] = {"pivots": [], "sweeps": [], "breaks": [], "zones": [], "resolved": []}
        self._confirm_pivots(index, events)
        self._resolve_zones(bar, index, events)
        self._track_sweeps(bar, index, events)
        self.last_events = {k: v for k, v in events.items() if v}
        return events

    def _push_atr(self, bar: dict) -> None:
        """Wilder's ATR, one bar at a time - a warm-up must not be quadratic."""
        if len(self.bars) < 2:
            self.atr.append(None)
            return
        prev_close = float(self.bars[-2]["close"])
        high, low = float(bar["high"]), float(bar["low"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        last = self.atr[-1] if self.atr else None
        if last is None:
            self._tr_seed.append(tr)
            if len(self._tr_seed) >= self.period:
                self.atr.append(sum(self._tr_seed[: self.period]) / self.period)
            else:
                self.atr.append(None)
        else:
            self.atr.append((last * (self.period - 1) + tr) / self.period)

    def _bars_list(self) -> list[dict]:
        return list(self.bars)

    def _pivot_at(self, position: int, width: int) -> tuple[str, float] | None:
        bars = self._bars_list()
        start = max(0, position - width)
        window = bars[start : position + width + 1]
        if len(window) < width * 2 + 1:
            return None
        bar = bars[position]
        if float(bar["high"]) >= max(float(b["high"]) for b in window):
            return HIGH, float(bar["high"])
        if float(bar["low"]) <= min(float(b["low"]) for b in window):
            return LOW, float(bar["low"])
        return None

    # ---------------------------------------------------------------- pivots
    def _confirm_pivots(self, index: int, events: dict) -> None:
        bars = self._bars_list()
        for width, bucket, major in (
            (self.pivot_len, self.minor, False),
            (self.major_len, self.major, True),
        ):
            position = len(bars) - 1 - width
            if position < width:
                continue
            found = self._pivot_at(position, width)
            if found is None:
                continue
            side, price = found
            if any(p.price == price and p.side == side and p.major == major for p in bucket):
                continue
            pivot = Pivot(side=side, price=price, index=index - width, time=int(bars[position]["close_time"]), major=major)
            bucket.append(pivot)
            events["pivots"].append(pivot.as_dict())
            if major:
                # the last major break gives the external trend (p.53 MS)
                if side == HIGH and float(bars[-1]["close"]) > price:
                    self.external_trend = 1
                elif side == LOW and float(bars[-1]["close"]) < price:
                    self.external_trend = -1
            else:
                self._inspect_algo_candle(pivot, index, events)

    # ------------------------------------------------------- algo candle (p.67)
    def _inspect_algo_candle(self, pivot: Pivot, index: int, events: dict) -> None:
        """The pivot bar is the candidate: is it the candle that absorbed orders?"""
        bars = self._bars_list()
        offset = self.count - len(bars)
        k = pivot.index - offset  # position of the pivot candle in the window
        if k < 0 or k >= len(bars):
            return
        bar = bars[k]
        wanted = "bearish" if pivot.side == HIGH else "bullish"
        gap = None
        for j in range(k + 2, min(len(bars), k + 2 + self.disp_bars)):
            found = fair_value_gap(bars[j - 2], bars[j - 1], bars[j], j)
            if found is not None and found.side == wanted:
                gap = found
                break
        if gap is None:
            return  # no displacement, no FVG -> not an algo candle

        atr = self.atr_now() or 0.0
        if atr and gap.size < float(self.cfg["min_fvg_atr"]) * atr:
            return  # a gap a fraction of the ATR is noise, not displacement
        if pivot.side == HIGH:
            top, bottom, level = float(bar["high"]), min(float(bar["open"]), float(bar["close"])), float(bar["high"])
        else:
            top, bottom, level = max(float(bar["open"]), float(bar["close"])), float(bar["low"]), float(bar["low"])

        evidence = ["displacement left an FVG"] if gap else []
        strength = 1

        # inducement / liquidity pool around the extreme (p.45, p.69, p.91)
        window = bars[max(0, k - 30) : k + 1]
        tol = float(self.cfg["induce_tol"]) * atr if atr else 0.0
        if pivot.side == HIGH:
            induce = equal_levels([float(b["high"]) for b in window], tol) - 1
        else:
            induce = equal_levels([float(b["low"]) for b in window], tol) - 1
        induce = max(0, induce)
        if induce >= int(self.cfg["induce_count"]):
            evidence.append(f"inducement: {induce + 1} equal {'highs' if pivot.side == HIGH else 'lows'}")
            strength += 1

        # liquidity grab: did this candle take out an earlier swing? (p.45, p.69)
        earlier = [p for p in self.minor if p.index < pivot.index and p.side == pivot.side]
        grabbed = None
        if earlier:
            prior = max(earlier, key=lambda p: p.index)
            if (pivot.side == HIGH and level > prior.price) or (pivot.side == LOW and level < prior.price):
                grabbed = prior.price
                evidence.append("grabbed the previous swing's liquidity")

        # momentum shift: the displacement closed beyond the opposite swing
        opposite = [p for p in self.minor if p.side != pivot.side and p.index < pivot.index]
        shift_level = None
        if opposite:
            prior = max(opposite, key=lambda p: p.index)
            closes = [float(b["close"]) for b in bars[k + 1 :]]
            if pivot.side == HIGH and any(c < prior.price for c in closes):
                shift_level = prior.price
                evidence.append("closed through the opposite swing (momentum shift)")
            elif pivot.side == LOW and any(c > prior.price for c in closes):
                shift_level = prior.price
                evidence.append("closed through the opposite swing (momentum shift)")

        session = session_of(pivot.time, self.cfg["sessions"])
        if session:
            evidence.append(f"forms on the {session} session")

        if grabbed is not None:
            strength += 1
        if shift_level is not None:
            strength += 1

        base_kind = "algo_bear" if pivot.side == HIGH else "algo_bull"
        very_strong = grabbed is not None and shift_level is not None and gap is not None and induce >= int(self.cfg["induce_count"])
        kind = ("very_strong_algo_bear" if pivot.side == HIGH else "very_strong_algo_bull") if very_strong else base_kind

        zone = Zone(
            kind=kind,
            side=pivot.side,
            symbol=self.symbol,
            timeframe=self.timeframe,
            top=top,
            bottom=bottom,
            level=level,
            index=pivot.index,
            time=pivot.time,
            strength=strength,
            evidence=evidence,
            swept_level=grabbed,
            break_level=shift_level,
            fvg={"side": gap.side, "top": gap.top, "bottom": gap.bottom, "time": gap.time},
            inducement=induce,
            session=session,
        )
        if self._add_zone(zone):
            events["zones"].append(zone.as_dict())

        # The same candle is a REJECTION BLOCK when it is the extreme of a
        # session AND has inducement - that is the book's "STRONG REJECTION
        # BLOCK = HAVE AN INDUCEMENT + forms on a session high/low" (p.90/91).
        if (
            session
            and induce >= int(self.cfg["induce_count"])
            and self._is_session_extreme(k, bars, session, pivot.side)
        ):
            kind = "rejection_block_bear" if pivot.side == HIGH else "rejection_block_bull"
            block = Zone(
                kind=kind,
                side=pivot.side,
                symbol=self.symbol,
                timeframe=self.timeframe,
                top=top,
                bottom=bottom,
                level=level,
                index=pivot.index,
                time=pivot.time,
                strength=strength,
                evidence=evidence + [f"session high/low of the {session} session"],
                swept_level=grabbed,
                break_level=shift_level,
                fvg={"side": gap.side, "top": gap.top, "bottom": gap.bottom, "time": gap.time},
                inducement=induce,
                session=session,
            )
            if self._add_zone(block):
                events["zones"].append(block.as_dict())

    def _is_session_extreme(self, k: int, bars: list[dict], session: str, side: str) -> bool:
        """Is bar ``k`` the high (low) of its own session, not just inside one?"""
        sessions = self.cfg["sessions"]
        start = k
        while start - 1 >= 0 and session_of(int(bars[start - 1]["close_time"]), sessions) == session:
            start -= 1
        end = k
        while end + 1 < len(bars) and session_of(int(bars[end + 1]["close_time"]), sessions) == session:
            end += 1
        if end - start + 1 < 6:
            return False  # too young to have a high/low worth drawing
        window = bars[start : end + 1]
        if side == HIGH:
            return float(bars[k]["high"]) >= max(float(b["high"]) for b in window)
        return float(bars[k]["low"]) <= min(float(b["low"]) for b in window)

    # ---------------------------------------------------------------- sweeps
    def _track_sweeps(self, bar: dict, index: int, events: dict) -> None:
        """Liquidity grab first, then the break that confirms it (p.42).

        The pending grab is checked BEFORE the current candle is allowed to arm
        a new one - otherwise a candle that sweeps one side would erase the grab
        still waiting on the other side.
        """
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        for side in (HIGH, LOW):
            sweep = self.sweeps[side]
            if sweep is None:
                continue
            pivot: Pivot = sweep["pivot"]
            if side == HIGH:
                sweep["extreme"] = max(sweep["extreme"], high)
            else:
                sweep["extreme"] = min(sweep["extreme"], low)
            opposite = self._opposite_pivot(side, pivot)
            broke = False
            if opposite is not None and index > sweep["index"]:
                broke = close < opposite.price if side == HIGH else close > opposite.price
            if broke:
                self._make_strong_high(pivot, opposite, sweep["extreme"], bar, index, events)
                self.sweeps[side] = None
            elif index - sweep["index"] > max(30, self.pivot_len * 10):
                self.sweeps[side] = None  # stale, forget it

        for side in (HIGH, LOW):
            if self.sweeps[side] is not None:
                continue
            level = self._sweepable(side, high, low)
            if level is None:
                continue
            self.sweeps[side] = {"pivot": level, "extreme": high if side == HIGH else low, "index": index}
            events["sweeps"].append({"side": side, "price": level.price, "time": int(bar["close_time"])})

    def _sweepable(self, side: str, high: float, low: float) -> Pivot | None:
        for pivot in reversed(self.minor):
            if pivot.side != side:
                continue
            if self.count - pivot.index > self.max_age:
                continue
            if side == HIGH and high > pivot.price:
                return pivot
            if side == LOW and low < pivot.price:
                return pivot
        return None

    def _opposite_pivot(self, side: str, pivot: Pivot) -> Pivot | None:
        """The swing whose break confirms the manipulation (p.42).

        The pullback that forms AFTER the grab is the one that matters; only if
        there is none yet do we fall back to an older swing on the other side.
        """
        candidates = [
            p
            for p in self.minor
            if p.side != side and p.index != pivot.index
            and ((p.price < pivot.price) if side == HIGH else (p.price > pivot.price))
        ]
        if not candidates:
            return None
        after = [p for p in candidates if p.index > pivot.index]
        pool = after or [p for p in candidates if p.index < pivot.index]
        return max(pool, key=lambda p: p.index) if pool else None

    # ------------------------------------------------- strong / very strong (p.42, p.45)
    def _make_strong_high(
        self, pivot: Pivot, opposite: Pivot, extreme: float, bar: dict, index: int, events: dict
    ) -> None:
        bars = self._bars_list()
        offset = self.count - len(bars)
        k = pivot.index - offset
        k = max(0, min(len(bars) - 1, k))
        candle = bars[k]
        side = pivot.side
        if side == HIGH:
            top, bottom, level = float(candle["high"]), min(float(candle["open"]), float(candle["close"])), float(candle["high"])
        else:
            top, bottom, level = max(float(candle["open"]), float(candle["close"])), float(candle["low"]), float(candle["low"])

        evidence = [
            "liquidity grabbed above the swing" if side == HIGH else "liquidity grabbed below the swing",
            "structure broke the other way",
        ]
        strength = 2

        # was the swept level itself a strong (major) level? p.45
        strong_level = any(p.side == side and abs(p.price - pivot.price) < 1e-9 for p in self.major)
        if strong_level:
            evidence.append("the level it grabbed was a strong (major) level")
            strength += 1

        # a major break on this candle? (p.53 MS)
        major_break = any(
            p.side != side and ((side == HIGH and float(bar["close"]) < p.price) or (side == LOW and float(bar["close"]) > p.price))
            for p in self.major
        )
        if major_break:
            evidence.append("this break is a major one (MS)")
            strength += 1

        # an FVG in the leg that broke? p.69/p.87
        gap = None
        for j in range(max(2, k), len(bars)):
            found = fair_value_gap(bars[j - 2], bars[j - 1], bars[j], j)
            if found is not None and found.side == ("bearish" if side == HIGH else "bullish"):
                gap = found
                break
        if gap is not None:
            evidence.append("the leg left an FVG")
            strength += 1

        atr = self.atr_now() or 0.0
        tol = float(self.cfg["induce_tol"]) * atr if atr else 0.0
        window = bars[max(0, k - 30) : k + 1]
        values = [float(b["high"]) for b in window] if side == HIGH else [float(b["low"]) for b in window]
        induce = max(0, equal_levels(values, tol) - 1)
        if induce >= int(self.cfg["induce_count"]):
            evidence.append(f"inducement: {induce + 1} equal {'highs' if side == HIGH else 'lows'}")
            strength += 1

        session = session_of(int(candle["close_time"]), self.cfg["sessions"])

        # strong high/low
        kind = "strong_high" if side == HIGH else "strong_low"
        zone = Zone(
            kind=kind,
            side=side,
            symbol=self.symbol,
            timeframe=self.timeframe,
            top=top,
            bottom=bottom,
            level=level,
            index=pivot.index,
            time=pivot.time,
            strength=strength,
            evidence=evidence,
            swept_level=pivot.price,
            break_level=opposite.price,
            fvg={"side": gap.side, "top": gap.top, "bottom": gap.bottom, "time": gap.time} if gap else None,
            inducement=induce,
            session=session,
        )
        # p.45: very strong = grabbed liquidity + broke a strong high/low + inducement
        if strong_level and induce >= int(self.cfg["induce_count"]):
            zone.kind = "very_strong_high" if side == HIGH else "very_strong_low"
            zone.evidence.append("very strong: strong level + inducement")
        if self._add_zone(zone):
            events["zones"].append(zone.as_dict())

        # the opposite side is now the WEAK high/low of the pair (p.42)
        weak = Zone(
            kind="weak_low" if side == HIGH else "weak_high",
            side=LOW if side == HIGH else HIGH,
            symbol=self.symbol,
            timeframe=self.timeframe,
            top=max(opposite.price, opposite.price),  # a line, drawn as a flat block
            bottom=opposite.price,
            level=opposite.price,
            index=opposite.index,
            time=opposite.time,
            strength=1,
            evidence=["the swing that failed to hold (weak)"],
            swept_level=pivot.price,
            break_level=opposite.price,
        )
        if self._add_zone(weak):
            events["zones"].append(weak.as_dict())
        events["breaks"].append({"side": side, "level": opposite.price, "time": int(bar["close_time"])})

    # ---------------------------------------------------------- mitigation
    def _resolve_zones(self, bar: dict, index: int, events: dict) -> None:
        """p.48: a zone lives until price mitigates it, or until it is broken."""
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        for zone in self.zones:
            if zone.status != "active":
                continue
            age = index - zone.created_index
            if age > self.max_age:
                zone.status = "invalidated"
                zone.invalidated_at = int(bar["close_time"])
                zone.invalidated_price = close
                events["resolved"].append({"key": zone.key, "status": "expired"})
                continue
            if zone.side == HIGH:
                # the high is broken for good when a candle CLOSES above it
                if close > zone.top:
                    zone.status = "invalidated"
                    zone.invalidated_at = int(bar["close_time"])
                    zone.invalidated_price = close
                    events["resolved"].append({"key": zone.key, "status": "invalidated"})
                    continue
                reached = high >= zone.level if self.mitigation_mode == "level" else high >= zone.bottom
                if reached:
                    zone.status = "mitigated"
                    zone.mitigated_at = int(bar["close_time"])
                    zone.mitigated_index = index
                    zone.mitigated_price = high if self.mitigation_mode == "level" else max(low, zone.bottom)
                    events["resolved"].append({"key": zone.key, "status": "mitigated"})
            else:
                if close < zone.bottom:
                    zone.status = "invalidated"
                    zone.invalidated_at = int(bar["close_time"])
                    zone.invalidated_price = close
                    events["resolved"].append({"key": zone.key, "status": "invalidated"})
                    continue
                reached = low <= zone.level if self.mitigation_mode == "level" else low <= zone.top
                if reached:
                    zone.status = "mitigated"
                    zone.mitigated_at = int(bar["close_time"])
                    zone.mitigated_index = index
                    zone.mitigated_price = low if self.mitigation_mode == "level" else min(high, zone.top)
                    events["resolved"].append({"key": zone.key, "status": "mitigated"})

    # ------------------------------------------------------------------ view
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for zone in self.zones:
            out[zone.kind] = out.get(zone.kind, 0) + 1
        return out

    def snapshot(self, limit: int = 60) -> dict[str, Any]:
        active = sorted(self.active(), key=lambda z: -z.time)
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": len(self.bars),
            "price": self.last_price,
            "atr": self.atr_now(),
            "external_trend": self.external_trend,
            "counts": self.counts(),
            "zones": [z.as_dict() for z in active[:limit]],
        }
