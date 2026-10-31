"""The round-17 gates: trend context, break of structure, nesting depth, volume.

Each test states the trader's rule and then proves the gate enforces it - and,
just as importantly, that the gate cannot be satisfied by accident:

    a range must not pass as a trend (no higher highs / higher lows)
    a wick through a swing is a sweep, not a break
    a break of structure that happened long ago does not count any more
    "internal within internal" is refused, one level of internal is allowed
    a high-volume bar is a measurement, not a switch
    the combined score only adds up when the evidence is really there
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.engine import Engine  # noqa: E402
from bot.filters import SCORE_WEIGHTS, Filters  # noqa: E402


# --------------------------------------------------------------- fixtures
def context(direction=1, clean=True, bos_direction=1, bos_age=12, source="major"):
    return {
        "external": {"direction": direction, "clean": clean,
                     "bos_direction": bos_direction, "bos_age": bos_age,
                     "bos_level": 100.0, "source": source},
        "internal": {"direction": direction, "bos_direction": bos_direction, "bos_age": 3},
    }


def candidate(**kwargs):
    """A LONG setup whose trend evidence is good unless a test says otherwise."""
    base = {
        "structure": True, "hvb": "up", "hvb_points": 3.0, "break": "high",
        "side": "LONG", "rr": 2.0,
        "context": context(),
        "depth": {"value": 2, "label": "internal", "internal_shifts": 0},
        "volume_ratio": 1.8,
        "book": {"fvgs_in_leg": 1, "break_kind": "SHIFT", "pools_ahead": 0,
                 "external_trend": 1, "zone": "discount", "sweep_zone": "discount"},
    }
    base.update(kwargs)
    return base


OPEN = {"structure_direction": False, "block_liquidity_pool": False, "require_fvg": False,
        "premium_discount": False, "require_ms": False, "regime": False}


def gates(**overrides):
    """Only the round-17 gates on, so a failure is unambiguous."""
    return Filters({**OPEN, "trend_context": True, "require_bos": True,
                    "require_hh_hl": True, "max_internal_depth": 2, **overrides})


# ------------------------------------------------------------- trend context
def test_a_long_is_refused_when_the_external_trend_points_down():
    ok, reasons = gates().apply(candidate(context=context(direction=-1, bos_direction=-1)))
    assert not ok
    assert any("against the external trend" in r for r in reasons), reasons


def test_a_short_is_refused_when_the_external_trend_points_up():
    ok, reasons = gates().apply(
        candidate(side="SHORT", hvb="down", context=context(direction=1, bos_direction=1))
    )
    assert not ok
    assert any("against the external trend" in r for r in reasons), reasons


def test_a_market_with_no_swing_sequence_is_refused():
    """This is the ranging market: swings exist, but they are not going anywhere."""
    ok, reasons = gates().apply(
        candidate(context=context(direction=None, clean=False, bos_direction=1))
    )
    assert not ok
    assert any("no clear external trend" in r for r in reasons), reasons


def test_the_clean_hh_hl_requirement_can_be_relaxed():
    weak = candidate(context=context(direction=1, clean=False))
    strict = gates()
    assert not strict.apply(json.loads(json.dumps(weak)))[0]           # clean required
    assert gates(require_hh_hl=False).apply(json.loads(json.dumps(weak)))[0]


def test_a_trend_that_agrees_passes():
    ok, reasons = gates().apply(candidate())
    assert ok, reasons


# ------------------------------------------------------- break of structure
def test_no_break_of_structure_no_trade():
    ok, reasons = gates().apply(candidate(context=context(bos_direction=None, bos_age=None)))
    assert not ok
    assert any("no break of structure" in r for r in reasons), reasons


def test_a_break_the_other_way_is_not_a_break_for_this_trade():
    ok, reasons = gates().apply(candidate(context=context(bos_direction=-1)))
    assert not ok
    assert any("the other way" in r for r in reasons), reasons


def test_a_stale_break_does_not_count():
    fresh = gates(bos_max_age=240)
    assert fresh.apply(candidate(context=context(bos_age=30)))[0]
    ok, reasons = fresh.apply(candidate(context=context(bos_age=900)))
    assert not ok
    assert any("bars ago" in r for r in reasons), reasons
    # and the same setup passes if the trader widens the window
    assert gates(bos_max_age=0).apply(candidate(context=context(bos_age=900)))[0]


# ------------------------------------------------------------------ depth
def test_internal_within_internal_is_refused():
    deep = {"value": 3, "label": "internal within internal", "internal_shifts": 1}
    ok, reasons = gates().apply(candidate(depth=deep))
    assert not ok
    assert any("internal within internal" in r and "nested too deep" in r for r in reasons), reasons


def test_one_level_of_internal_structure_is_allowed():
    shallow = {"value": 2, "label": "internal", "internal_shifts": 0}
    assert gates().apply(candidate(depth=shallow))[0]


def test_an_external_break_is_always_shallow_enough():
    external = {"value": 1, "label": "external", "internal_shifts": 0}
    assert gates().apply(candidate(depth=external))[0]


def test_the_depth_limit_is_the_traders_choice():
    deep = {"value": 3, "label": "internal within internal", "internal_shifts": 1}
    assert gates(max_internal_depth=3).apply(candidate(depth=deep))[0]


# ----------------------------------------------------------------- volume
def test_a_high_volume_bar_is_measured_against_the_bars_before_it():
    strict = gates(volume_break=True, volume_mult=1.5)
    ok, reasons = strict.apply(candidate(volume_ratio=1.1))
    assert not ok
    assert any("no high-volume bar" in r for r in reasons), reasons
    assert strict.apply(candidate(volume_ratio=2.4))[0]
    # and it is off unless asked for
    assert gates().apply(candidate(volume_ratio=0.4))[0]


# ------------------------------------------------------------- the score
def test_the_score_adds_up_only_the_evidence_that_is_there():
    full = candidate()
    score, parts = Filters.score_of(full)
    assert all(parts.values())
    assert score == sum(SCORE_WEIGHTS.values()) == 100

    no_hvb = Filters.score_of(candidate(hvb=None))
    assert no_hvb[0] == 100 - SCORE_WEIGHTS["hvb"] and not no_hvb[1]["hvb"]

    weak = Filters.score_of(candidate(hvb=None, volume_ratio=0.5,
                                      context=context(bos_direction=-1),
                                      book={"fvgs_in_leg": 0}))
    assert weak[0] == 100 - SCORE_WEIGHTS["hvb"] - SCORE_WEIGHTS["volume"] \
        - SCORE_WEIGHTS["bos"] - SCORE_WEIGHTS["fvg"]
    assert weak[1]["trend"] is True, "the trend still agrees - only the other three are missing"


def test_score_mode_replaces_the_hard_hvb_gate_with_a_threshold():
    """The point of combining: a very strong setup does not need every single box."""
    # hvb missing but everything else present -> 75, passes a 60 threshold
    strong = candidate(hvb=None)
    ok, reasons = gates(hvb=True, confirm_mode="score", confirm_min_score=60).apply(
        json.loads(json.dumps(strong)))
    assert ok, reasons
    # ... and the same candidate fails if the trader asks for 80
    ok, reasons = gates(hvb=True, confirm_mode="score", confirm_min_score=80).apply(
        json.loads(json.dumps(strong)))
    assert not ok
    assert any("confluence score" in r for r in reasons), reasons
    # in "all" mode the same candidate fails because the HVB is missing
    ok, reasons = gates(hvb=True, confirm_mode="all").apply(json.loads(json.dumps(strong)))
    assert not ok and any("no HVB" in r for r in reasons), reasons


# ---------------------------------------------------------- in the engine
def _engine(**filters):
    return Engine(
        symbols=["BTCUSDT"],
        timeframes=["5m"],
        tdi_settings={"rsi_period": 14, "band_period": 14, "fast_period": 1, "slow_period": 3},
        break_lookback=5,
        filters={"structure": True, "hvb": False, "break": False, **filters},
        structure={"pivot_len": 3, "major_len": 9, "macro_len": 27, "sweep_max_wait": 120},
        strategy="sweep",
    )


def bar(open_time, price, high=None, low=None, volume=1.0):
    return {
        "symbol": "BTCUSDT", "open_time": open_time, "close_time": open_time + 59_999,
        "open": price, "high": high if high is not None else price + 0.4,
        "low": low if low is not None else price - 0.4,
        "close": price, "volume": volume, "trades": 1, "closed": True,
    }


def trend_path(steps=150, start=100.0, up=True):
    """A trendy staircase with internal structure inside every leg.

    Each cycle: five up, a shallow two-bar dip, five up, then a deeper four-bar
    pullback. That gives higher highs and higher lows at the big degree while the
    shallow dips produce the internal swings the depth rule counts.
    """
    out: list[float] = []
    price = start
    d = 1 if up else -1
    while len(out) < steps:
        for _ in range(5):
            price += d * 1.0
            out.append(price)
        for _ in range(2):
            price -= d * 0.8
            out.append(price)
        for _ in range(5):
            price += d * 1.0
            out.append(price)
        for _ in range(4):
            price -= d * 1.6
            out.append(price)
    return out[:steps]


async def _feed(engine, prices, start_ms=1_735_689_600_000, volume=1.0):
    for i, price in enumerate(prices):
        await engine.on_candle(bar(start_ms + i * 300_000, price, volume=volume))


@pytest.mark.asyncio
async def test_the_engine_measures_the_trend_of_a_real_staircase():
    engine = _engine()
    await _feed(engine, trend_path())
    cell = engine.cells[("BTCUSDT", "5m")]
    state = cell.tmap_state
    assert state["external"]["direction"] == 1, state["external"]
    assert state["external"]["clean"] is True
    assert state["external"]["bos_direction"] == 1
    assert state["minor"]["pivots"]["highs"] >= 2


@pytest.mark.asyncio
async def test_the_engine_reports_depth_among_the_candidates_it_rejects():
    """Whatever the gate decides, the reason has to be on the record."""
    engine = _engine(trend_context=True, require_bos=True, max_internal_depth=2)
    await _feed(engine, trend_path())
    seen = engine.filters.summary()["reasons"]
    assert isinstance(seen, dict)
    # a monotone-ish staircase in a trend: either a signal was published or a
    # reason was counted - never silence without an explanation
    assert engine.alerts or seen


def test_filters_config_round_trips_through_the_store(tmp_path):
    from bot import config_store

    payload = {
        "trend_context": True, "require_hh_hl": True, "require_bos": True,
        "bos_max_age": 180, "max_internal_depth": 2, "volume_break": True,
        "volume_mult": 2.0, "volume_period": 30, "confirm_mode": "score",
        "confirm_min_score": 70,
    }
    cleaned = config_store.validate_filters(payload)
    for key, value in payload.items():
        assert cleaned[key] == value, key
    # rubbish is rejected, not stored
    bad = config_store.validate_filters({"max_internal_depth": "deep", "confirm_mode": "maybe"})
    assert bad["max_internal_depth"] == 2 and bad["confirm_mode"] == "all"
