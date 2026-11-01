"""Notification policy: what deserves the trader's attention, and when.

The old app toasted *everything* - every sweep, every log line, every state
change, once per connected socket and again on every poll - so the screen was
permanently covered by a stack of boxes that had to be dismissed to see the
chart. A notification system that fires constantly carries no information: if
everything is urgent, nothing is.

This module is the single place that decides. It is pure Python with an
injectable clock so the rules can be tested exactly.

Three destinations, and an event goes to exactly one of them:

* ``toast``  - a real, actionable event (a new **grade A** book signal, or an
  error the trader must see). At most ``toast_burst`` inside ``toast_window``
  seconds, never the same key twice, and a repeat of something already shown is
  suppressed instead of re-announced.
* ``inbox``  - everything else worth keeping: signals of any grade, state
  changes, warnings. It accumulates silently behind an unread badge.
* ``drop``   - chatter. A filtered setup, a repeated warm-up line, the same
  message again. Counted, never rendered twice.

The rules are deliberately boring and predictable:

    same key inside dedupe_window          -> drop
    same (category, title) inside coalesce -> merge into the group (count += 1)
    severity in {"debug"}                  -> drop
    severity in {"warn", "error"}          -> inbox only (the UI has one banner)
    severity "action"                      -> inbox + maybe toast
    toast budget exhausted                 -> inbox (the bell still moves)

So the worst case for the trader is "the bell has a number", never "the screen
is covered".
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

ACTION = "action"
INFO = "info"
WARN = "warn"
ERROR = "error"
DEBUG = "debug"

TOASTABLE = {ACTION}


@dataclass
class Event:
    """One thing that happened, already normalised for the page."""

    key: str
    title: str
    severity: str = INFO
    category: str = "system"
    detail: str = ""
    symbol: str = ""
    timeframe: str = ""
    side: str = ""
    grade: str = ""
    time: int = 0
    count: int = 1
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "severity": self.severity,
            "category": self.category,
            "detail": self.detail,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "grade": self.grade,
            "time": self.time,
            "count": self.count,
            "meta": self.meta,
        }


class NotificationBus:
    """Turns raw engine chatter into at most one quiet, meaningful stream."""

    def __init__(
        self,
        *,
        dedupe_window: float = 900.0,      # the same key again inside 15 min: drop
        coalesce_window: float = 120.0,    # the same title again: bump the counter
        toast_window: float = 60.0,        # one toast per minute...
        toast_burst: int = 2,              # ...with a small burst allowance
        max_events: int = 400,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dedupe_window = dedupe_window
        self.coalesce_window = coalesce_window
        self.toast_window = toast_window
        self.toast_burst = max(0, int(toast_burst))
        self.max_events = max(1, int(max_events))
        self.clock = clock

        self.events: deque[Event] = deque(maxlen=self.max_events)
        self.unread = 0
        self.filtered = 0          # setups the engine refused: counted, not shown
        self.suppressed = 0        # duplicates and chatter that were dropped
        self._seen: dict[str, float] = {}
        self._groups: dict[tuple[str, str], tuple[float, Event]] = {}
        self._toasts: deque[float] = deque()

    # ------------------------------------------------------------------ feed
    def absorb(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Take one engine message; return the notify envelopes to push.

        Engine messages are the raw material: ``alert`` (a setup or a sweep),
        ``log`` (warm-up chatter) and ``filtered`` (a rejected candidate). State
        messages are handled by the caller and never come through here.
        """
        kind = str(message.get("type") or "")
        if kind == "filtered":
            self.filtered += 1
            self.suppressed += 1
            return []
        if kind not in {"alert", "log", "setup"}:
            return []

        event = self._to_event(message)
        if event is None:
            return []
        return self.publish(event)

    def publish(self, event: Event) -> list[dict[str, Any]]:
        """Apply the policy to one event. Returns 0 or 1 envelopes."""
        now = self.clock()
        if not event.time:
            event.time = int(time.time() * 1000)

        if event.severity == DEBUG:
            self.suppressed += 1
            return []

        # ---- the same thing twice is not news
        last = self._seen.get(event.key)
        if last is not None and now - last < self.dedupe_window:
            self.suppressed += 1
            return []
        self._seen[event.key] = now
        if len(self._seen) > 4000:                 # bounded memory, oldest first
            cutoff = now - self.dedupe_window
            self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}

        # ---- the same *kind* of thing again merges into the open group.
        # Signals are exempt: two setups on one instrument share a title, and
        # folding them together would hide a trade (and swallow its toast).
        merged = None if event.category == "signal" else self._merge(
            (event.category, event.title), event, now)
        if merged is not None:
            return [{"event": merged.as_dict(), "toast": False, "unread": self.unread,
                     "counts": self.counts()}]

        self.events.appendleft(event)
        self.unread += 1
        toast = self._may_toast(event, now)
        return [{"event": event.as_dict(), "toast": toast, "unread": self.unread,
                 "counts": self.counts()}]

    def _merge(self, group_key: tuple[str, str], event: Event, now: float) -> Event | None:
        existing = self._groups.get(group_key)
        if existing is None:
            self._groups[group_key] = (now, event)
            return None
        seen_at, first = existing
        if now - seen_at > self.coalesce_window or first.severity != event.severity:
            self._groups[group_key] = (now, event)
            return None
        first.count += 1
        first.time = event.time
        if event.detail and event.detail not in first.detail:
            first.detail = (first.detail + " · " + event.detail)[:400]
        self._groups[group_key] = (seen_at, first)   # keep the original window
        self.suppressed += 1
        return first

    def _may_toast(self, event: Event, now: float) -> bool:
        if event.severity not in TOASTABLE:
            return False
        while self._toasts and now - self._toasts[0] > self.toast_window:
            self._toasts.popleft()
        if len(self._toasts) >= self.toast_burst:
            self.suppressed += 1
            return False
        self._toasts.append(now)
        return True

    # ------------------------------------------------------------------ read
    def counts(self) -> dict[str, int]:
        return {
            "unread": self.unread,
            "events": len(self.events),
            "filtered": self.filtered,
            "suppressed": self.suppressed,
        }

    def list(self, since: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first. ``since`` is a millisecond stamp (0 = everything)."""
        out = []
        for event in self.events:
            if since and event.time <= since:
                continue
            out.append(event.as_dict())
            if len(out) >= max(1, limit):
                break
        return out

    def mark_read(self) -> int:
        was, self.unread = self.unread, 0
        return was

    # -------------------------------------------------------- normalisation
    def _to_event(self, message: dict[str, Any]) -> Event | None:
        kind = str(message.get("type") or "")
        if kind == "log":
            level = str(message.get("level") or "info").lower()
            text = str(message.get("message") or "").strip()
            if not text:
                return None
            severity = {"error": ERROR, "warn": WARN, "warning": WARN}.get(level, INFO)
            return Event(
                key=f"log:{_slug(text)}",
                title=text,
                severity=severity,
                category="system",
                time=int(time.time() * 1000),
            )

        if kind == "setup":
            alert = message.get("setup") or {}
        else:
            alert = message.get("alert") or {}
        if not isinstance(alert, dict) or not alert:
            return None

        what = str(alert.get("kind") or "signal")
        symbol = str(alert.get("symbol") or "")
        timeframe = str(alert.get("timeframe") or "")
        side = str(alert.get("side") or "")
        grade = str(alert.get("grade") or "")
        when = int(alert.get("time") or alert.get("created_ms") or time.time() * 1000)
        key = str(alert.get("key") or f"{what}:{symbol}:{timeframe}:{when}")

        if what != "signal":
            # a sweep or a filtered line is context, not an instruction
            return Event(
                key=key, title=f"{side} {symbol} {timeframe} swept liquidity".strip(),
                severity=INFO, category="sweep", symbol=symbol, timeframe=timeframe,
                side=side, time=when,
            )

        entry = alert.get("entry")
        target = alert.get("target")
        rr = alert.get("rr")
        bits = []
        if entry is not None:
            bits.append(f"entry {_num(entry)}")
        if target is not None:
            bits.append(f"draw {_num(target)}")
        if rr:
            bits.append(f"1:{float(rr):.1f}")
        title = f"{side} {symbol} {timeframe}".strip()
        return Event(
            key=key,
            title=title,
            severity=ACTION if grade in ("A", "") else INFO,
            category="signal",
            detail=" · ".join(bits),
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            grade=grade,
            time=when,
            meta={"entry": entry, "stop": alert.get("stop"), "target": target, "rr": rr},
        )


def _slug(text: str) -> str:
    """A stable key for a message, ignoring the numbers that change each time."""
    out = []
    for token in str(text).split():
        out.append("#" if any(ch.isdigit() for ch in token) else token.lower())
    return " ".join(out)[:120]


def _num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) >= 1000:
        return f"{number:,.0f}"
    return f"{number:.4g}"


def coalesce(events: Iterable[Event]) -> list[Event]:
    """Group a list of events by (category, title), keeping the newest order."""
    seen: dict[tuple[str, str], Event] = {}
    order: list[tuple[str, str]] = []
    for event in events:
        key = (event.category, event.title)
        if key in seen:
            seen[key].count += event.count
        else:
            seen[key] = event
            order.append(key)
    return [seen[key] for key in order]
