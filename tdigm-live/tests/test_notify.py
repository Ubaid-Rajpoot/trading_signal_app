"""The notification policy is the trader's headline defect, so it is pinned here.

Every rule that keeps the screen quiet is a test: one toast at a time, at most
``toast_burst`` per window, never the same key twice, one row per event, a
bounded inbox, and - the subtle one - **two signals are never merged**, even
when they share a title, because that would hide a trade.

The clock is injected, so these run instantly and deterministically.
"""

from __future__ import annotations

import pytest

from bot.notify import ACTION, INFO, WARN, Event, NotificationBus


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def bus():
    clock = Clock()
    return NotificationBus(clock=clock), clock


def signal(key: str, title: str = "LONG BTCUSDT 15m", grade: str = "A", **extra) -> Event:
    return Event(
        key=key,
        title=title,
        severity=ACTION,
        category="signal",
        grade=grade,
        detail="entry 100 · draw 90 · 1:4.0",
        time=1_700_000_000_000,
        **extra,
    )


# --------------------------------------------------------------------- rules
def test_an_actionable_signal_toasts_and_lands_in_the_inbox(bus):
    notifier, _ = bus
    out = notifier.publish(signal("a"))
    assert len(out) == 1
    assert out[0]["toast"] is True
    assert out[0]["unread"] == 1
    assert len(notifier.list()) == 1


def test_the_same_key_inside_the_window_is_dropped(bus):
    notifier, clock = bus
    notifier.publish(signal("a"))
    clock.tick(120)
    assert notifier.publish(signal("a")) == []
    assert notifier.counts()["suppressed"] == 1
    assert len(notifier.list()) == 1


def test_the_same_key_after_the_window_is_allowed_again(bus):
    notifier, clock = bus
    notifier.publish(signal("a"))
    clock.tick(notifier.dedupe_window + 1)
    out = notifier.publish(signal("a"))
    assert len(out) == 1


def test_the_burst_is_two_and_then_the_screen_stays_quiet(bus):
    notifier, clock = bus
    first = notifier.publish(signal("a"))
    clock.tick(5)
    second = notifier.publish(signal("b"))
    clock.tick(5)
    third = notifier.publish(signal("c"))
    assert first[0]["toast"] is True
    assert second[0]["toast"] is True, "a small burst is allowed"
    assert third[0]["toast"] is False, "but the third inside the window is inbox only"
    assert third[0]["unread"] == 3, "the bell still moves"
    assert notifier.counts()["events"] == 3


def test_a_strict_single_toast_bus_never_shows_two():
    clock = Clock()
    notifier = NotificationBus(clock=clock, toast_burst=1)
    assert notifier.publish(signal("a"))[0]["toast"] is True
    clock.tick(5)
    assert notifier.publish(signal("b"))[0]["toast"] is False


def test_the_toast_budget_recovers_with_time(bus):
    notifier, clock = bus
    notifier.publish(signal("a"))
    notifier.publish(signal("b"))       # burst of two
    clock.tick(1)
    assert notifier.publish(signal("c"))[0]["toast"] is False
    clock.tick(notifier.toast_window + 1)
    assert notifier.publish(signal("d"))[0]["toast"] is True


def test_info_events_never_toast(bus):
    notifier, _ = bus
    out = notifier.publish(Event(key="w", title="warm-up BTCUSDT", severity=INFO, category="system"))
    assert out[0]["toast"] is False
    assert out[0]["event"]["severity"] == INFO


def test_warnings_are_inbox_only_and_errors_too(bus):
    notifier, _ = bus
    body = notifier.absorb({"type": "log", "level": "warn", "message": "feed is slow"})[0]
    assert body["event"]["severity"] == WARN
    assert body["toast"] is False
    err = notifier.absorb({"type": "log", "level": "error", "message": "feed stopped: boom"})[0]
    assert err["toast"] is False
    assert notifier.counts()["events"] == 2


def test_debug_messages_never_reach_the_page(bus):
    notifier, _ = bus
    assert notifier.publish(Event(key="d", title="tick", severity="debug")) == []
    assert notifier.counts()["events"] == 0
    assert notifier.counts()["suppressed"] == 1


def test_chatter_with_the_same_title_merges_into_one_row(bus):
    notifier, clock = bus
    notifier.publish(Event(key="k1", title="retrying XYZUSDT", severity=INFO, category="system"))
    clock.tick(30)
    out = notifier.publish(Event(key="k2", title="retrying XYZUSDT", severity=INFO, category="system"))
    assert out[0]["toast"] is False
    rows = notifier.list()
    assert len(rows) == 1
    assert rows[0]["count"] == 2
    assert notifier.counts()["unread"] == 1


def test_two_signals_with_the_same_title_are_two_rows(bus):
    notifier, clock = bus
    notifier.publish(signal("s1"))
    clock.tick(2)
    notifier.publish(signal("s2"))       # same title, different trade
    rows = notifier.list()
    assert len(rows) == 2, "a shared title must not hide a second trade"
    assert notifier.counts()["unread"] == 2


def test_the_inbox_is_bounded(bus):
    notifier, clock = bus
    for i in range(600):
        clock.tick(1)
        notifier.publish(Event(key=f"e{i}", title=f"event {i}", severity=INFO, category="c"))
    assert notifier.max_events == 400
    assert len(notifier.list(limit=1000)) == 400
    assert notifier.counts()["events"] == 400


def test_marking_read_clears_the_badge_and_keeps_the_history(bus):
    notifier, _ = bus
    notifier.publish(signal("a"))
    notifier.publish(signal("b"))
    assert notifier.mark_read() == 2
    assert notifier.counts()["unread"] == 0
    assert notifier.counts()["events"] == 2
    assert notifier.mark_read() == 0


def test_list_is_newest_first_and_can_resume_from_a_stamp(bus):
    notifier, clock = bus
    notifier.publish(signal("a"))
    clock.tick(1)
    notifier.publish(signal("b", title="SHORT ETHUSDT 5m"))
    rows = notifier.list()
    assert [row["key"] for row in rows] == ["b", "a"]
    newer = notifier.list(since=rows[0]["time"])
    assert newer == []


# ------------------------------------------------------------- normalisation
def test_a_filtered_candidate_is_counted_and_never_shown(bus):
    notifier, _ = bus
    assert notifier.absorb({"type": "filtered", "reason": "rr too low"}) == []
    counts = notifier.counts()
    assert counts["filtered"] == 1
    assert counts["events"] == 0


def test_a_log_line_becomes_a_stable_key(bus):
    notifier, _ = bus
    notifier.absorb({"type": "log", "message": "warmed 12 symbols in 3.4s"})
    notifier.absorb({"type": "log", "message": "warmed 48 symbols in 9.1s"})
    rows = notifier.list()
    assert len(rows) == 1, "the same sentence with different numbers is the same event"
    assert notifier.counts()["suppressed"] == 1, "the repeat is counted, not drawn"


def test_two_different_log_lines_are_two_rows(bus):
    notifier, _ = bus
    notifier.absorb({"type": "log", "message": "added 4 instruments"})
    notifier.absorb({"type": "log", "message": "re-scan finished"})
    assert len(notifier.list()) == 2


def test_an_alert_with_a_grade_b_toasts_never(bus):
    notifier, _ = bus
    out = notifier.absorb({
        "type": "alert",
        "alert": {"kind": "signal", "symbol": "BTCUSDT", "timeframe": "15m",
                  "side": "LONG", "grade": "B", "time": 1_700_000_000_000,
                  "entry": 100.0, "stop": 95.0, "target": 120.0, "rr": 4.0},
    })
    event = out[0]["event"]
    assert event["severity"] == INFO
    assert out[0]["toast"] is False
    assert "BTCUSDT" in event["title"]
    assert "draw 120" in event["detail"]


def test_a_grade_a_alert_can_toast(bus):
    notifier, _ = bus
    out = notifier.absorb({
        "type": "alert",
        "alert": {"kind": "signal", "symbol": "BTCUSDT", "timeframe": "15m",
                  "side": "SHORT", "grade": "A", "time": 1_700_000_000_000,
                  "entry": 100.0, "stop": 105.0, "target": 80.0, "rr": 4.0},
    })
    assert out[0]["toast"] is True


def test_unknown_messages_are_ignored(bus):
    notifier, _ = bus
    assert notifier.absorb({"type": "state", "signals": []}) == []
    assert notifier.absorb({}) == []
    assert notifier.counts()["events"] == 0
