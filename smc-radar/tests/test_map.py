"""The book's definitions, turned into assertions.

Each test builds a deliberate price path and asks the map one question - "is
this a strong high?", "did that high get mitigated?" - so the rule chain that
ships in ``bot/map.py`` is pinned to the pages it came from.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from bot.map import (  # noqa: E402
    DEFAULTS,
    HIGH,
    LOW,
    MarketMap,
    equal_levels,
    fair_value_gap,
    session_of,
    wilder_atr,
)

DAY = 86_400_000


def bar(index: int, close: float, *, high: float | None = None, low: float | None = None, open_: float | None = None,
        step: int = 300_000, start: int = 1_700_000_000_000) -> dict:
    open_ = close if open_ is None else open_
    high = max(open_, close) + 0.1 if high is None else high
    low = min(open_, close) - 0.1 if low is None else low
    return {
        "open_time": start + index * step,
        "close_time": start + (index + 1) * step,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


def run(bars: list[dict], settings: dict | None = None, symbol: str = "TESTUSDT", timeframe: str = "5m") -> MarketMap:
    map_ = MarketMap(symbol, timeframe, settings)
    for b in bars:
        map_.add(b)
    return map_


def path(closes: list[float], **kwargs) -> list[dict]:
    return [bar(i, c, **kwargs) for i, c in enumerate(closes)]


# --------------------------------------------------------------- the helpers
def test_session_of_uses_utc_hours():
    sessions = DEFAULTS["sessions"]
    midnight = 1_700_000_000_000  # 2023-11-14 22:13:20 UTC, rounded below
    day_start = (midnight // DAY) * DAY
    assert session_of(day_start + 2 * 3_600_000, sessions) == "Asia"
    assert session_of(day_start + 13 * 3_600_000, sessions) == "London"
    assert session_of(day_start + 23 * 3_600_000, sessions) is None


def test_equal_levels_counts_touches_within_tolerance():
    assert equal_levels([1.0, 1.02, 3.0], 0.05) == 2
    assert equal_levels([1.0, 1.02, 1.01], 0.05) == 3
    assert equal_levels([], 0.05) == 0


def test_fair_value_gap_needs_a_three_candle_imbalance():
    a = bar(0, 10.0, high=10.5, low=9.5)
    b = bar(1, 12.0, high=12.5, low=10.0)   # bullish: low of b > high of a
    c = bar(2, 13.0, high=13.5, low=12.2)
    gap = fair_value_gap(a, b, c, 2)
    assert gap is not None and gap.side == "bullish"
    assert gap.bottom == 10.5 and gap.top == 12.2

    # overlapping candles leave no imbalance
    a = bar(0, 10.0, high=10.5, low=9.5)
    b = bar(1, 10.4, high=10.6, low=10.2)
    c = bar(2, 10.3, high=10.7, low=10.1)
    assert fair_value_gap(a, b, c, 2) is None


def test_wilder_atr_matches_the_incremental_version():
    bars = path([10, 11, 10.5, 12, 11.5, 13, 12.2, 12.8, 12.1, 13.4, 12.9, 13.1, 12.4, 13.6, 14.0, 13.2, 14.4])
    full = wilder_atr(bars, 14)[-1]
    map_ = run(bars)
    assert map_.atr_now() == pytest.approx(full, rel=1e-9)


# ---------------------------------------------------------- the strong levels
def strong_high_path() -> list[float]:
    """Peak at 3, pullback low at 6, sweep above the peak at 9, break at 13."""
    return [10, 11, 12, 13, 12, 11, 10, 11, 12, 13.5, 12.5, 11, 10.2, 9.5, 9.2, 9.4, 9.1, 9.3, 9.0, 9.2]


def test_pivots_are_confirmed_once_the_window_closes():
    map_ = run(path(strong_high_path()))
    highs = [p for p in map_.minor if p.side == HIGH]
    lows = [p for p in map_.minor if p.side == LOW]
    assert any(p.index == 3 for p in highs)
    assert any(p.index == 6 for p in lows)


def test_sweep_then_break_makes_a_strong_high():
    map_ = run(path(strong_high_path()))
    zones = [z for z in map_.zones if z.kind == "strong_high"]
    assert zones, "the sweep of the swing high + break of the swing low must register"
    zone = zones[0]
    assert zone.side == HIGH
    assert zone.level == pytest.approx(13.1)      # the swept high
    assert zone.swept_level == pytest.approx(13.1)
    assert any("liquidity" in e for e in zone.evidence)
    assert any("structure broke" in e for e in zone.evidence)
    assert zone.time == map_.bars[3]["close_time"]


def test_every_strong_high_produces_its_weak_low_pair():
    map_ = run(path(strong_high_path()))
    assert any(z.kind == "weak_low" for z in map_.zones), "p.42: for every strong HIGH there is a weak LOW"


def test_very_strong_needs_a_major_level_and_inducement():
    closes = strong_high_path()
    map_ = run(path(closes))
    very = [z for z in map_.zones if z.kind in {"very_strong_high", "very_strong_low"}]
    for zone in very:
        assert zone.inducement >= DEFAULTS["induce_count"]
        assert any("strong level" in e for e in zone.evidence)


def test_low_side_mirror():
    closes = [20 - c for c in strong_high_path()]  # flip the whole path
    map_ = run(path(closes))
    zones = [z for z in map_.zones if z.kind == "strong_low"]
    assert zones, "the mirror image must produce a strong low"
    assert zones[0].side == LOW
    assert any(z.kind == "weak_high" for z in map_.zones)


# -------------------------------------------------------------- mitigation
def test_returning_into_the_block_mitigates_the_high():
    closes = strong_high_path() + [9.6, 10.0, 10.6, 11.2, 11.8, 12.3, 12.9]
    map_ = run(path(closes))
    zone = next(z for z in map_.zones if z.kind == "strong_high")
    assert zone.top == pytest.approx(13.1)
    assert zone.status == "mitigated"
    assert zone.mitigated_at is not None
    assert map_.active() == [] or all(z.key != zone.key for z in map_.active())


def test_closing_above_the_high_invalidates_it():
    # 12.0 stays under the block, then one candle closes above the high: the
    # high is broken, so invalidation wins over the touch that same candle made
    closes = strong_high_path() + [9.6, 10.0, 11.0, 12.0, 13.4]
    map_ = run(path(closes))
    zone = next(z for z in map_.zones if z.kind == "strong_high")
    assert zone.status == "invalidated"
    assert zone.invalidated_price == pytest.approx(13.4)
    assert zone.invalidated_at is not None


def test_level_mode_waits_for_the_exact_extreme():
    closes = strong_high_path() + [9.6, 10.0, 12.5, 12.9]
    settings = {**DEFAULTS, "mitigation_mode": "level"}
    map_ = run(path(closes), settings)
    zone = next(z for z in map_.zones if z.kind == "strong_high")
    assert zone.status == "active", "12.9 is inside the block but not the exact high - not mitigated"


def test_zone_is_never_dropped_while_it_is_active():
    map_ = run(path(strong_high_path()))
    before = {z.key for z in map_.active()}
    for i, close in enumerate([9.0, 9.1, 8.9], start=len(strong_high_path())):
        map_.add(bar(i, close))
    assert before <= {z.key for z in map_.zones}
    assert all(z.status in {"active", "mitigated", "invalidated"} for z in map_.zones)


# ------------------------------------------------------------- soundness
def test_zone_keys_are_unique_and_evidence_is_a_list():
    map_ = run(path([10, 11, 12, 13, 12, 11, 10, 11, 12, 13.5, 12.5, 11, 10.2, 9.5, 9.2, 9.4]))
    keys = [z.key for z in map_.zones]
    assert len(keys) == len(set(keys))
    for zone in map_.zones:
        assert isinstance(zone.as_dict()["evidence"], list)
        assert zone.bottom <= zone.top


def test_snapshot_is_json_ready():
    import json

    map_ = run(path(strong_high_path()))
    payload = map_.snapshot()
    json.dumps(payload)
    assert payload["symbol"] == "TESTUSDT"
    assert payload["timeframe"] == "5m"
    assert payload["bars"] == len(strong_high_path())


# ------------------------------------------- round 5: order block + quality
def real_path(closes: list[float]) -> list[dict]:
    """Candles with a real open (the previous close), so bodies and colours exist."""
    bars, prev = [], closes[0]
    for i, close in enumerate(closes):
        bars.append(bar(i, close, open_=prev))
        prev = close
    return bars


def orders(zone, bars, lookback=8):
    """The opposite-colour candle with the biggest body, latest wins on a tie."""
    k = next(i for i, b in enumerate(bars) if b["close_time"] >= zone.time)
    window = list(enumerate(bars[max(0, k - lookback): k + 1]))
    pool = [
        (i, b)
        for i, b in window
        if (float(b["close"]) >= float(b["open"])) == (zone.side == HIGH)
        and ((float(b["low"]) < zone.level) if zone.side == HIGH else (float(b["high"]) > zone.level))
    ]
    if not pool:
        return None
    best = max(pool, key=lambda pair: (abs(float(pair[1]["close"]) - float(pair[1]["open"])), pair[0]))
    return best[1]


def test_high_block_starts_at_the_exact_high_and_marks_the_order_block_candle():
    bars = real_path(strong_high_path())
    map_ = run(bars)
    zone = next(z for z in map_.zones if z.kind == "strong_high")
    assert zone.top == pytest.approx(zone.level), "the rectangle starts at the exact high"
    assert zone.bottom < zone.top, "the rectangle has real height"
    assert zone.block_src == "order block candle"
    candle = orders(zone, bars)
    assert candle is not None
    assert zone.ob["time"] == candle["close_time"], "the marked candle is the biggest opposite-colour body"
    assert float(candle["close"]) >= float(candle["open"]), "at a HIGH the marked candle is a green/up candle"
    assert zone.bottom == pytest.approx(float(candle["low"])), "the block reaches that candle's low"


def test_low_block_mirrors_it_with_a_red_candle():
    bars = real_path([20 - c for c in strong_high_path()])
    map_ = run(bars)
    zone = next(z for z in map_.zones if z.kind == "strong_low")
    assert zone.bottom == pytest.approx(zone.level)
    candle = orders(zone, bars)
    assert zone.ob["time"] == candle["close_time"]
    assert float(candle["close"]) < float(candle["open"]), "at a LOW the marked candle is a red/down candle"
    assert zone.top == pytest.approx(float(candle["high"]))


def test_block_never_collapses_to_a_line():
    bars = path([10.0] * 20)
    map_ = run(bars)
    for zone in map_.zones:
        assert zone.top > zone.bottom


def test_regime_separates_a_trend_from_a_range():
    map_trend = run(path([100 + i * 0.8 for i in range(60)]))
    assert map_trend.regime()["state"] == "trend_up"
    assert map_trend.regime()["er"] > 0.5

    map_chop = run(path([100 + (2 if i % 2 else -2) for i in range(60)]))
    assert map_chop.regime()["state"] == "range", "an alternating tape is not a trend"


def test_every_zone_carries_a_grade_and_its_reason():
    map_ = run(real_path(strong_high_path() * 2))
    assert map_.zones, "the path produces zones"
    for zone in map_.zones:
        payload = zone.as_dict()
        assert payload["grade"] in {"A+", "A", "B", "C"}
        assert 0 <= payload["score"] <= 100
        assert payload["trend"]["state"] in {"trend_up", "trend_down", "range", "unknown"}
        assert payload["block_atr"] is None or payload["block_atr"] > 0


def test_a_thin_block_scores_below_a_thick_one():
    """The study says block thickness is the feature that matters, so it must
    move the score. Same zone, same trend, only the block size changes."""
    # warm the ATR up first: the map needs a few candles before it has one
    bars = real_path([10.0] * 20 + strong_high_path())
    map_ = run(bars)
    zone = next(z for z in map_.zones if z.kind == "strong_high")
    assert zone.block_atr is not None, "after a warm-up every zone is measurable"
    zone.block_atr = 0.1
    map_.score_zone(zone)
    thin_score = zone.score
    zone.block_atr = 1.4
    map_.score_zone(zone)
    assert zone.score > thin_score, "a thicker block must score higher"
    assert zone.score > 0

    # and the calibrated weights put a real 1m kind in a sensible place
    zone.kind = "rejection_block_bear" if zone.side == "high" else "rejection_block_bull"
    map_.score_zone(zone)
    assert zone.grade in {"A+", "A"}
