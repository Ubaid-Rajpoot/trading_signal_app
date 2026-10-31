"""The clock: sessions, the New York midnight open, and the 90-minute cycle.

The book does not trade the same way at 03:00 and at 14:30 (p.23-27):

* **Asia** builds the range that the other two sessions raid.
* **London** takes out the Asian high/low - and the previous day's high/low -
  before the real move of the day.
* **New York** traps the open, and the high or low of the day very often forms
  here rather than in London.
* The **daily open** (midnight New York) splits the day into premium and
  discount (p.49); price above it is expensive, below it is cheap.
* The **90-minute cycle** starts at 00:00 New York; each quarter-hour block is
  a fresh little auction (p.31).

Timestamps are milliseconds, timezone is handled properly with ``zoneinfo`` so
daylight saving does not silently move the open by an hour twice a year.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

try:  # pragma: no cover - depends on the platform's tz database
    from zoneinfo import ZoneInfo

    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    NY = _dt.timezone(_dt.timedelta(hours=-5), "EST")

_MS_MIN = 60_000
_MS_DAY = 86_400_000
CYCLE_MS = 90 * _MS_MIN

SESSIONS: dict[str, tuple[int, int]] = {
    # UTC hours [start, end): Asia builds, London raids, New York traps
    "asia": (0, 7),
    "london": (7, 12),
    "ny": (12, 21),
}


@dataclass(frozen=True)
class Session:
    name: str
    start_ms: int
    end_ms: int

    def contains(self, ms: int) -> bool:
        return self.start_ms <= int(ms) < self.end_ms


def _utc(ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(int(ms) / 1000, tz=_dt.timezone.utc)


def session_at(ms: int) -> Session | None:
    """The session a timestamp falls in, as concrete start/end milliseconds."""
    hour = _utc(ms).hour
    for name, (start, end) in SESSIONS.items():
        if start <= hour < end:
            day = _utc(ms).replace(hour=0, minute=0, second=0, microsecond=0)
            start_ms = int(day.timestamp() * 1000) + start * 3_600_000
            end_ms = int(day.timestamp() * 1000) + end * 3_600_000
            return Session(name, start_ms, end_ms)
    return None


def session_name(ms: int) -> str | None:
    """Just the name - what the card prints ("london", "ny", ...)."""
    hour = _utc(ms).hour
    for name, (start, end) in SESSIONS.items():
        if start <= hour < end:
            return name
    return None


def ny_midnight(ms: int) -> int:
    """The most recent 00:00 New York, in UTC milliseconds."""
    local = _dt.datetime.fromtimestamp(int(ms) / 1000, tz=NY)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def cycle_index(ms: int) -> int:
    """Which 90-minute cycle of the New York day we are in (0..15)."""
    return int((int(ms) - ny_midnight(ms)) // CYCLE_MS)


def cycle_label(ms: int) -> str:
    return f"cycle {cycle_index(ms) + 1}"


def weekday(ms: int) -> int:
    """0 = Monday. p.27: Monday is the day the weekly manipulation happens."""
    return _utc(ms).weekday()


def timing_tag(ms: int) -> str:
    """A short, human label for the card."""
    name = session_name(ms)
    if name is None:
        return "off-session"
    if weekday(ms) == 0:
        return f"{name} (monday manipulation)"
    return f"{name} {cycle_label(ms)}"
