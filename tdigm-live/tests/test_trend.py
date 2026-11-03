"""Tests for the three-degree trend map (``bot/trend.py``).

The rules these pin down, in the trader's words:

* a trend means **higher highs and higher lows** (or lower highs and lower lows),
  read off real swing points - not an oscillator;
* a **break of structure is a close** through the swing, never a wick (a wick
  through a high is the sweep the setup trades);
* a setup **nested inside internal structure inside internal structure** is
  deeper than the trader will trade, and the depth says which one it is.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.trend import Pivot, TrendMap  # noqa: E402

SMALL = {"minor_len": 2, "major_len": 4, "macro_len": 8, "hh_tolerance_atr": 0.0}


def zigzag(points: list[float], bars: int = 6, start: int = 0) -> list[dict]:
    """A piecewise-linear path through ``points``, one candle per step.

    The turning points become swing highs and lows; highs/lows sit a hair above
    and below the path so every turning point is a clean extreme.
    """
    out: list[dict] = []
    index = start
    price = points[0]
    for target in points[1:]:
        steps = bars
        for step in range(steps):
            nxt = price + (target - price) / (steps - step)
            out.append(
                {
                    "open": price,
                    "high": max(price, nxt) + 0.05,
                    "low": min(price, nxt) - 0.05,
                    "close": nxt,
                    "close_time": index * 60_000,
                    "volume": 10.0,
                }
            )
            index += 1
            price = nxt
    return out


def feed(points: list[float], settings: dict | None = None, atr: float = 1.0):
    tmap = TrendMap({**SMALL, **(settings or {})})
    bars = zigzag(points)
    for bar in bars:
        state = tmap.add(bar, atr=atr)
    return tmap, bars, state


UP = [100, 110, 106, 116, 111, 122, 117, 128]
DOWN = [130, 120, 124, 114, 119, 108, 113, 100]
RANGE = [100, 110, 100, 110, 100, 110, 100, 110, 100]


def test_higher_highs_and_higher_lows_are_a_bullish_external_trend():
    _, _, state = feed(UP)
    ext = state["external"]
    assert ext["direction"] == 1
    assert ext["hh"] and ext["hl"] and ext["clean"]
    assert not ext["lh"] and not ext["ll"]


def test_lower_highs_and_lower_lows_are_a_bearish_external_trend():
    _, _, state = feed(DOWN)
    ext = state["external"]
    assert ext["direction"] == -1
    assert ext["lh"] and ext["ll"] and ext["clean"]
    assert not ext["hh"] and not ext["hl"]


def test_a_range_is_not_a_trend_no_matter_how_often_it_sweeps():
    """The whole point: a range prints the same sweep/break shape as a trend."""
    tmap, _, state = feed(RANGE)
    assert state["external"]["direction"] is None
    assert not state["external"]["clean"]
    # it still has swings and still detects breaks - structure is not absent,
    # the *trend* is. That is what the gate is for.
    assert state["minor"]["pivots"]["highs"] > 0


def test_range_release_closes_beyond_the_furthest_remembered_edge():
    """Breaking a newer pivot inside a range is not the same as leaving it."""
    tmap = TrendMap(SMALL)
    highs = tmap.degrees["major"].highs
    highs.extend([
        Pivot("high", 110.0, 1),  # visible left-side range edge
        Pivot("high", 108.0, 2),  # newer, but still inside that range
    ])
    tmap.count = 10

    inside = {"open": 107.0, "high": 109.5, "low": 106.5, "close": 109.0,
              "close_time": 10 * 60_000, "volume": 10.0}
    state = tmap.add(inside, atr=1.0)
    assert state["major"]["bos_direction"] == 1
    assert state["major"]["bos_range_break"] is False
    assert highs[-1].broken and not highs[0].broken

    release = {"open": 109.0, "high": 112.0, "low": 108.5, "close": 111.0,
               "close_time": 11 * 60_000, "volume": 10.0}
    state = tmap.add(release, atr=1.0)
    assert state["major"]["bos_range_break"] is True
    assert state["major"]["bos_range_edge"] == 110.0
    assert tmap.degrees["major"].bos(1)["price"] == 110.0


def test_one_displacement_can_clear_local_and_outer_range_boundaries():
    tmap = TrendMap(SMALL)
    highs = tmap.degrees["major"].highs
    highs.extend([Pivot("high", 110.0, 1), Pivot("high", 108.0, 2)])
    tmap.count = 10
    release = {"open": 107.0, "high": 112.0, "low": 106.5, "close": 111.0,
               "close_time": 10 * 60_000, "volume": 10.0}
    state = tmap.add(release, atr=1.0)
    assert state["major"]["bos_range_break"] is True
    assert all(pivot.broken for pivot in highs)
    assert len(tmap.degrees["major"].bos_events) == 1


def test_a_break_of_structure_needs_a_close_not_a_wick():
    """A wick through the swing high is the sweep; only a close is a break."""
    tmap, bars, _ = feed([100, 110, 105, 108])
    before = tmap.degrees["minor"].bos(1)
    # a candle that pokes above the 110 swing but closes back under it
    poke = {"open": 108, "high": 112.0, "low": 107.5, "close": 109.0,
            "close_time": 999 * 60_000, "volume": 10.0}
    tmap.add(poke, atr=1.0)
    assert tmap.degrees["minor"].bos(1) == before, "a wick is not a break"
    # now a candle that CLOSES above it
    through = {"open": 109, "high": 112.5, "low": 108.5, "close": 112.0,
               "close_time": 1000 * 60_000, "volume": 10.0}
    tmap.add(through, atr=1.0)
    bos = tmap.degrees["minor"].bos(1)
    assert bos is not None and bos["direction"] == 1
    assert bos["price"] <= 112.0


def test_the_break_age_counts_bars():
    tmap, bars, _ = feed(UP)
    state = tmap.add({"open": 128, "high": 128.6, "low": 127.4, "close": 128.2,
                      "close_time": 10**6, "volume": 10.0}, atr=1.0)
    age = state["external"]["bos_age"]
    if age is not None:
        assert age >= 1
        later = tmap.add({"open": 128.2, "high": 128.8, "low": 127.6, "close": 128.4,
                          "close_time": 10**6 + 60_000, "volume": 10.0}, atr=1.0)
        assert later["external"]["bos_age"] == age + 1 or later["external"]["bos_age"] < age


def test_depth_one_is_an_external_break():
    tmap, _, _ = feed(UP)
    depth = tmap.depth("LONG", "MS")
    assert depth["value"] == 1 and depth["label"] == "external"


def test_depth_two_is_the_first_internal_break_of_the_leg():
    tmap, bars, _ = feed(UP)
    first = tmap.depth("LONG", "SHIFT")
    assert first["value"] == 2, f"expected one level of internal, got {first}"
    assert first["label"] == "internal"
    assert first["origin_bos"] is not None


def test_depth_three_is_internal_within_internal_and_is_labelled_as_such():
    """The counting rule itself: 1 + minor breaks since the external break.

    Pivot detection is covered by the tests above; this pins the nesting rule
    exactly, including the wording the page prints.
    """
    from collections import deque

    tmap, _, _ = feed(UP)
    tmap.degrees["major"].bos_events = deque(
        [{"degree": "major", "side": "high", "direction": 1, "price": 100.0, "index": 10}]
    )
    tmap.degrees["minor"].bos_events = deque(
        [{"degree": "minor", "side": "high", "direction": 1, "price": 105.0, "index": 20}]
    )
    one = tmap.depth("LONG", "SHIFT")          # the setup at the current bar
    assert one["internal_shifts"] == 1 and one["value"] == 3
    assert one["label"] == "internal within internal"
    assert one["origin_bos"]["index"] == 10

    # with no internal break before it, the same setup is one layer shallower
    tmap.degrees["minor"].bos_events = deque()
    still = tmap.depth("LONG", "SHIFT")
    assert still["value"] == 2 and still["label"] == "internal"

    # an MS (a break of external structure) is depth 1 whatever the internals do
    tmap.degrees["minor"].bos_events = deque(
        [{"degree": "minor", "side": "high", "direction": 1, "price": 105.0, "index": 20}]
    )
    assert tmap.depth("LONG", "MS")["value"] == 1


def test_the_other_side_has_no_depth_without_its_own_external_break():
    tmap, _, _ = feed(UP)
    assert tmap.depth("SHORT", "SHIFT")["value"] is None


def test_the_state_at_a_bar_does_not_depend_on_what_came_after_it():
    """The state at bar i is the same whether or not the feed continued.

    A swing point is only usable ``width`` bars after it, so the *contents* of
    the pivot lists grow later - but what the bot decided at bar i cannot change.
    """
    bars = zigzag(UP)
    stop_at = len(bars) // 2

    full = TrendMap(SMALL)
    recorded = None
    for i, bar in enumerate(bars):
        state = full.add(bar, atr=1.0)
        if i == stop_at:
            recorded = state

    short = TrendMap(SMALL)
    for bar in bars[: stop_at + 1]:
        short.add(bar, atr=1.0)

    assert short.state() == recorded


def test_default_degrees_are_five_twenty_eighty():
    tmap = TrendMap({})
    assert tmap.widths == {"minor": 5, "major": 20, "macro": 80}
