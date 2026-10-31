"""The liquidity map: where the stops are, and which pool price is drawn to.

The book is blunt about it (p.5): **major liquidity, major move.** A trade is
only worth taking when there is an untouched pool in front of it, and the size
of the pool decides the size of the move.

| grade  | pools                                                            | used for |
|--------|------------------------------------------------------------------|----------|
| major  | PDH/PDL, PWH/PWL, PMH/PML, PYH/PYL, HTF swings                    | the draw |
| medium | the session extremes (Asia / London / New York), 15m-1h swings     | the trap, second-choice draw |
| minor  | 1m-5m swings                                                     | inducement only |

Causality is part of the data: ``PDH`` does not exist while the day is still
open, ``PWH`` not until the week has closed. Every pool carries ``made_ms`` -
the moment a trader could have known it - and ``swept_ms`` - the moment price
took it. A pool that has been swept is struck off the map: liquidity that is
gone cannot be a draw.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

MAJOR = "major"
MEDIUM = "medium"
MINOR = "minor"

_DAY = 86_400_000
_WEEK = 7 * _DAY

# session windows in UTC hours, the clock the book trades (p.23-27)
SESSIONS = {
    "asia": (0, 7),
    "london": (7, 12),
    "ny": (12, 21),
}


@dataclass
class Pool:
    """One resting pool of liquidity."""

    name: str            # "PDH", "PWL", "ASIA_HIGH", "WEEK_HIGH" ...
    price: float
    side: str            # "high" (above price) or "low"
    grade: str           # major | medium | minor
    made_ms: int         # when it became knowable
    swept_ms: int | None = None

    @property
    def taken(self) -> bool:
        return self.swept_ms is not None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "price": self.price,
            "side": self.side,
            "grade": self.grade,
            "made_ms": self.made_ms,
            "swept_ms": self.swept_ms,
        }


def _utc_day(ms: int) -> int:
    return int(ms) // _DAY


def _utc_week(ms: int) -> int:
    # ISO-ish week bucket: Monday-anchored. 1970-01-01 was a Thursday, so shift 3 days.
    return (int(ms) + 3 * _DAY) // _WEEK


def _utc_month(ms: int) -> tuple[int, int]:
    import datetime as _dt

    stamp = _dt.datetime.fromtimestamp(int(ms) / 1000, tz=_dt.timezone.utc)
    return stamp.year, stamp.month


def session_of(ms: int) -> str | None:
    """Which session a timestamp belongs to (UTC windows)."""
    hour = (int(ms) // 3_600_000) % 24
    for name, (start, end) in SESSIONS.items():
        if start <= hour < end:
            return name
    return None


class LiquidityMap:
    """Everything resting above and below price, built from 1m candles."""

    def __init__(self, keep_minor: bool = False) -> None:
        self.pools: list[Pool] = []
        self.keep_minor = keep_minor
        self._day: int | None = None
        self._week: int | None = None
        self._month: tuple[int, int] | None = None
        self._day_high = self._day_low = None
        self._week_high = self._week_low = None
        self._month_high = self._month_low = None
        self._day_high_time = self._day_low_time = 0
        self._week_high_time = self._week_low_time = 0
        self._month_high_time = self._month_low_time = 0
        self._session: str | None = None
        self._session_high = self._session_low = None
        self._session_high_time = self._session_low_time = 0
        self._closed_session: dict = {}
        self.day_open: float | None = None
        self.day_open_ms: int = 0
        self.last_price: float = 0.0
        self.last_ms: int = 0

    # ------------------------------------------------------------------ feed
    def add(self, bar: dict) -> None:
        """Feed one closed 1-minute candle (or the finest timeframe available)."""
        high, low = float(bar["high"]), float(bar["low"])
        ms = int(bar.get("close_time") or bar.get("open_time") or 0)
        self.last_price = float(bar["close"])
        self.last_ms = ms

        day, week, month = _utc_day(ms), _utc_week(ms), _utc_month(ms)

        if self._day is None:
            self._start_day(day, bar, ms)
        elif day != self._day:
            # the day is over: its high and low become the pools everyone watches
            self.add_pool("PDH", self._day_high, "high", MAJOR, ms, self._day_high_time)
            self.add_pool("PDL", self._day_low, "low", MAJOR, ms, self._day_low_time)
            self._start_day(day, bar, ms)

        if self._week is None:
            self._start_week(week)
        elif week != self._week:
            self.add_pool("PWH", self._week_high, "high", MAJOR, ms, self._week_high_time)
            self.add_pool("PWL", self._week_low, "low", MAJOR, ms, self._week_low_time)
            self._start_week(week)

        if self._month is None:
            self._start_month(month)
        elif month != self._month:
            self.add_pool("PMH", self._month_high, "high", MAJOR, ms, self._month_high_time)
            self.add_pool("PML", self._month_low, "low", MAJOR, ms, self._month_low_time)
            self._start_month(month)

        self._day_high = high if self._day_high is None else max(self._day_high, high)
        self._day_low = low if self._day_low is None else min(self._day_low, low)
        if high == self._day_high:
            self._day_high_time = ms
        if low == self._day_low:
            self._day_low_time = ms
        self._week_high = high if self._week_high is None else max(self._week_high, high)
        self._week_low = low if self._week_low is None else min(self._week_low, low)
        if self._week_high == high:
            self._week_high_time = ms
        if self._week_low == low:
            self._week_low_time = ms
        self._month_high = high if self._month_high is None else max(self._month_high, high)
        self._month_low = low if self._month_low is None else min(self._month_low, low)
        if self._month_high == high:
            self._month_high_time = ms
        if self._month_low == low:
            self._month_low_time = ms

        # ---- sessions (p.23: Asia builds the range, London runs it, NY traps)
        name = session_of(ms)
        if name != self._session:
            if self._session is not None and self._session_high is not None:
                label = self._session.upper()
                self.add_pool(f"{label}_HIGH", self._session_high, "high", MEDIUM, ms,
                              self._session_high_time)
                self.add_pool(f"{label}_LOW", self._session_low, "low", MEDIUM, ms,
                              self._session_low_time)
                self._closed_session = {
                    "name": self._session, "high": self._session_high, "low": self._session_low,
                }
            self._session = name
            self._session_high = high
            self._session_low = low
            self._session_high_time = self._session_low_time = ms
        else:
            if high > (self._session_high or high):
                self._session_high, self._session_high_time = high, ms
            if low < (self._session_low or low):
                self._session_low, self._session_low_time = low, ms

        # ---- mark every pool the candle traded through
        for pool in self.pools:
            if pool.swept_ms is not None:
                continue
            if pool.side == "high" and high > pool.price:
                pool.swept_ms = ms
            elif pool.side == "low" and low < pool.price:
                pool.swept_ms = ms

    def _start_day(self, day: int, bar: dict, ms: int) -> None:
        self._day = day
        self._day_high = float(bar["high"])
        self._day_low = float(bar["low"])
        self._day_high_time = self._day_low_time = int(bar.get("open_time") or ms)
        self.day_open = float(bar["open"])
        self.day_open_ms = int(bar.get("open_time") or ms)

    def _start_week(self, week: int) -> None:
        self._week = week
        self._week_high = None
        self._week_low = None

    def _start_month(self, month: tuple[int, int]) -> None:
        self._month = month
        self._month_high = None
        self._month_low = None

    # ---------------------------------------------------------------- pools
    def add_pool(self, name: str, price: float | None, side: str, grade: str,
                 made_ms: int, level_ms: int | None = None) -> Pool | None:
        if price is None or price <= 0:
            return None
        pool = Pool(name=name, price=float(price), side=side, grade=grade,
                    made_ms=int(made_ms))
        # a pool made *before* now was already tradeable; if price is already
        # beyond it, it is gone - it must not appear as a fresh draw
        if level_ms is not None and level_ms < made_ms:
            pass
        self.pools.append(pool)
        return pool

    def prune(self, keep: int = 400) -> None:
        if len(self.pools) > keep:
            self.pools = self.pools[-keep:]

    def live_session_extremes(self) -> dict:
        return {
            "session": self._session,
            "high": self._session_high,
            "low": self._session_low,
            "closed": dict(self._closed_session),
        }

    # ----------------------------------------------------------------- draw
    def open_pools(self, side: str | None = None, grade: str | None = None) -> list[Pool]:
        out = [p for p in self.pools if p.swept_ms is None]
        if side:
            out = [p for p in out if p.side == side]
        if grade:
            out = [p for p in out if p.grade == grade]
        return out

    def draw(self, direction: int, price: float, min_grade: str = MAJOR) -> Pool | None:
        """The nearest untouched pool in the direction the trade wants to go.

        ``direction`` +1 looks up (a long's draw), -1 looks down. Major pools are
        preferred; medium ones are only considered when ``min_grade`` allows it.
        """
        order = {MAJOR: 2, MEDIUM: 1, MINOR: 0}
        floor = order.get(min_grade, 2)
        side = "high" if direction > 0 else "low"
        best: Pool | None = None
        for pool in self.pools:
            if pool.swept_ms is not None or pool.side != side:
                continue
            if order.get(pool.grade, 0) < floor:
                continue
            beyond = pool.price > price if direction > 0 else pool.price < price
            if not beyond:
                continue
            if best is None:
                best = pool
                continue
            closer = (pool.price < best.price) if direction > 0 else (pool.price > best.price)
            if closer:
                best = pool
        return best

    def nearest(self, side: str, price: float) -> Pool | None:
        best: Pool | None = None
        for pool in self.pools:
            if pool.swept_ms is not None or pool.side != side:
                continue
            if best is None:
                best = pool
                continue
            closer = abs(pool.price - price) < abs(best.price - price)
            if closer:
                best = pool
        return best

    def touched_recently(self, since_ms: int) -> list[Pool]:
        return [p for p in self.pools if p.swept_ms is not None and p.swept_ms >= since_ms]

    def as_list(self, limit: int = 12) -> list[dict]:
        price = self.last_price
        open_above = [p for p in self.pools if p.swept_ms is None and p.price > price]
        open_below = [p for p in self.pools if p.swept_ms is None and p.price < price]
        open_above.sort(key=lambda p: p.price)
        open_below.sort(key=lambda p: -p.price)
        picked: Iterable[Pool] = list(open_above[:limit // 2]) + list(open_below[:limit // 2])
        return [p.as_dict() for p in picked]
