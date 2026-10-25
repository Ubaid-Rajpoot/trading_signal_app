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
        # round 8: "-" is a real answer - the model measured this plan below 60%
        # and the page says so instead of inventing a letter
        assert payload["grade"] in {"A+", "A", "B", "C", "-"}
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


# ------------------------------------------------- round 6: the trade plan
def test_every_real_zone_carries_a_tradeable_plan():
    """Round 6: entry at the block edge, stop beyond the level, target at the
    liquidity pool - and never a plan on a weak (flat) marker."""
    map_ = run(real_path(strong_high_path() * 2))
    seen = 0
    for zone in map_.zones:
        if zone.kind in ("weak_high", "weak_low"):
            assert zone.plan is None, "a flat weak marker is not a trade"
            continue
        seen += 1
        plan = zone.plan
        assert plan, f"{zone.kind} carries a plan"
        assert plan["side"] == ("short" if zone.side == HIGH else "long")
        # entry is the near edge of the block: the limit fills on the retest
        assert plan["entry"] == pytest.approx(zone.bottom if zone.side == HIGH else zone.top)
        # the stop is beyond the exact high/low, never inside it
        if zone.side == HIGH:
            assert plan["stop"] > zone.level
            assert plan["stop"] > plan["entry"]
        else:
            assert plan["stop"] < zone.level
            assert plan["stop"] < plan["entry"]
        if plan["target"] is None:
            assert plan["rr"] is None, "no pool in front means no R:R, not a made-up one"
            assert plan["target_source"] is None
            continue
        # the target is the next pool IN FRONT of the trade
        if zone.side == HIGH:
            assert plan["target"] < plan["entry"]
        else:
            assert plan["target"] > plan["entry"]
        risk = abs(plan["stop"] - plan["entry"])
        reward = abs(plan["entry"] - plan["target"])
        assert risk > 0 and reward > 0
        assert plan["rr"] == pytest.approx(reward / risk, abs=0.01)  # the API rounds R:R to 2 dp
        assert plan["target_source"], "every target names the pool it came from"
    assert seen > 0


def test_a_plan_fills_at_the_block_edge_and_ends_at_its_target():
    bars = real_path(strong_high_path() * 2)
    map_ = run(bars)
    zone = next(
        z for z in map_.zones
        if z.side == HIGH and z.plan and z.plan["target"] is not None and z.plan["rr"] > 0.5
    )
    zone.status = "active"
    zone.plan_state = "waiting"
    entry, target = float(zone.plan["entry"]), float(zone.plan["target"])
    n = len(bars)

    # a candle comes back into the block and stops well above the target
    back = (entry + target) / 2.0
    map_.add(bar(n, back, open_=entry + (zone.top - entry) / 2, high=entry + 0.01, low=back - 0.01))
    assert zone.plan_state == "filled", "the retest fills the limit at the block edge"
    assert zone.plan_filled_price == pytest.approx(entry)

    # the next candle reaches the pool the plan named
    map_.add(bar(n + 1, target + 0.01, open_=back, high=back + 0.01, low=target - 0.01))
    assert zone.plan_state == "target"
    assert zone.plan_result_r == pytest.approx(zone.plan["rr"])


def test_a_close_through_the_level_stops_the_plan():
    bars = real_path(strong_high_path() * 2)
    map_ = run(bars)
    zone = next(z for z in map_.zones if z.side == HIGH and z.plan and z.plan["target"] is not None)
    zone.status = "active"
    zone.plan_state = "waiting"
    n = len(bars)
    # price trades up through the block and closes above the exact high
    map_.add(bar(n, zone.top + 1.0, high=zone.top + 1.05, low=float(zone.plan["entry"])))
    assert zone.plan_state == "stopped", "a close through the high invalidates the short"
    assert zone.plan_result_r == -1.0


# ------------------------------- round 7: conditions, probability and the fee
def test_every_zone_is_checked_against_all_ten_conditions():
    from bot.map import CONDITION_NAMES

    map_ = run(real_path(strong_high_path() * 2))
    assert len(CONDITION_NAMES) == 10
    checked = 0
    for zone in map_.zones:
        if zone.kind.startswith("weak_"):
            continue
        checked += 1
        assert set(zone.conds) == set(CONDITION_NAMES), "no condition is left unchecked"
        assert zone.cond_met == sum(1 for v in zone.conds.values() if v)
        assert 2 <= zone.cond_met <= 10
    assert checked > 0


def test_the_grade_is_the_measured_probability_not_the_condition_count():
    """A setup with fewer conditions can still be the better trade - that is what
    the 90-day study found, so the letter has to follow the measurement."""
    from bot.model import model

    zone = run(real_path(strong_high_path() * 2)).zones[0]
    m = model()
    assert m.available, m.problem
    prob = m.probability(zone)
    assert prob is not None and 0.0 < prob < 1.0
    cuts = DEFAULTS["prob_cuts"]
    assert m.grade_for(prob, DEFAULTS) == (
        "A+" if prob >= cuts["a_plus"] else "A" if prob >= cuts["a"] else "B" if prob >= cuts["b"]
        else "C" if prob >= cuts.get("c", 0.60) else "-"
    )
    # the letter moves with the probability, not with the count of conditions
    zone.cond_met = 3
    assert m.grade_for(0.99, DEFAULTS) == "A+"
    assert m.grade_for(0.30, DEFAULTS) == "-"


def test_a_plan_smaller_than_the_fee_is_never_marked_tradable():
    from bot.model import SignalModel

    zone = run(real_path(strong_high_path() * 2)).zones[0]
    zone.plan = {"rr": 2.0, "risk_pct": 0.02}      # 0.02% stop vs a 0.08% round trip
    SignalModel().rate(zone, DEFAULTS)
    assert zone.fee_r > 1.0, "a stop this tight costs more than the risk itself"
    assert zone.tradable is False

    zone.plan = {"rr": 2.0, "risk_pct": 0.6}       # a stop the fee cannot swallow
    SignalModel().rate(zone, DEFAULTS)
    assert zone.fee_r <= 0.30
    assert zone.tradable is True


def test_a_plan_with_no_target_gets_no_probability_free_pass():
    from bot.model import SignalModel

    zone = run(real_path(strong_high_path() * 2)).zones[0]
    zone.plan = {"rr": None, "risk_pct": 1.0}
    SignalModel().rate(zone, DEFAULTS)
    assert zone.net_r is None and zone.tradable is False


# ------------------------------------------------- round 8: the plan that pays
def _zones_with_targets(settings: dict) -> list:
    map_ = run(real_path(strong_high_path() * 3), settings)
    return [z for z in map_.zones if z.plan and z.plan.get("target") is not None]


def test_the_target_is_never_a_pool_inside_the_noise():
    """A pool half a tick away pays a fraction of the risk - less than the fee.

    Round 8 asks for the *next meaningful* pool: with ``plan_target_atr_min`` the
    pools closer than that fraction of ATR are skipped, so the reward a signal
    offers is no longer dwarfed by what it risks.
    """
    classic = {**DEFAULTS, "plan_target_atr_min": 0.0}
    shipped = {**DEFAULTS, "plan_target_atr_min": 1.0}
    old = _zones_with_targets(classic)
    new = _zones_with_targets(shipped)
    assert old and new, "both settings must still produce plans"
    atr = {id(z): (z.plan["entry"], z.plan["target"]) for z in new}
    for zone in new:
        entry, target = atr[id(zone)]
        assert abs(entry - target) > 0, "a target is never the entry"
    old_rr = sum((z.plan.get("rr") or 0) for z in old) / len(old)
    new_rr = sum((z.plan.get("rr") or 0) for z in new) / len(new)
    assert new_rr > old_rr, f"the shipped plan must pay more per unit of risk ({new_rr:.2f} vs {old_rr:.2f})"


def test_the_stop_can_be_placed_beyond_the_red_box():
    """``plan_stop_mode`` is a measured choice, and the card has to say which one
    was used - a stop is never an unexplained number."""
    level = _zones_with_targets({**DEFAULTS, "plan_stop_mode": "level"})
    block = _zones_with_targets({**DEFAULTS, "plan_stop_mode": "block"})
    assert level and block
    for a, b in zip(level, block):
        assert abs(b.plan["risk"]) >= abs(a.plan["risk"]), "the block stop is never tighter"
    assert any("beyond the red box" in z.plan["stop_kind"] for z in block)
    assert all("beyond the level" in z.plan["stop_kind"] for z in level)


def test_the_fee_is_the_expected_one_not_the_worst_case():
    """A winner pays maker on both sides, a loser pays taker on the stop, so the
    cost that belongs in an expectancy is the weighted one."""
    from bot.model import SignalModel

    worst = SignalModel.fee_r(0.5)                  # no probability: the taker case
    likely = SignalModel.fee_r(0.5, prob=0.95)      # a 95% winner
    assert worst == pytest.approx((2 + 6) / 1e4 / 0.005, rel=1e-6)
    assert likely < worst, "a mostly-winning plan pays less in fees"
    assert likely == pytest.approx((0.95 * 4 + 0.05 * 8) / 1e4 / 0.005, rel=1e-6)


def test_only_a_high_measured_plan_is_a_signal():
    """The letters are accuracy bands now: A+ ≥90%, A ≥85%, B ≥75%, C ≥60%, and
    anything below C is honestly marked '-' instead of being given a letter."""
    from bot.model import SignalModel

    m = SignalModel()
    assert m.available, m.problem
    zone = run(real_path(strong_high_path() * 2)).zones[0]
    zone.plan = {"rr": 1.0, "risk_pct": 0.5, "target": 1.0, "stop": 2.0, "entry": 1.5}
    assert m.grade_for(0.95, DEFAULTS) == "A+"
    assert m.grade_for(0.86, DEFAULTS) == "A"
    assert m.grade_for(0.78, DEFAULTS) == "B"
    assert m.grade_for(0.62, DEFAULTS) == "C"
    assert m.grade_for(0.40, DEFAULTS) == "-"
    # and the band the card prints is the measured record of that letter
    band = m.band(0.86)
    assert band and band["letter"] == "A" and band["n"] >= 1 and 0 < band["win"] <= 1
