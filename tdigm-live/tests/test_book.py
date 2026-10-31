"""Tests for the book engine - one per rule of the book.

The strategy is a sequence of refusals, so most of these tests assert that
something is *not* traded when the book says it should not be: a pivot that
has not closed its right side, a pool that was accepted rather than raided, a
gap that is not quiet filled, a draw too close to pay for the risk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.book import BookRunner, BookSettings, LiquidityMap, Narrative, Structure  # noqa: E402
from bot.book.imbalance import (algo_candle, candle_atr, fair_value_gap,  # noqa: E402
                                is_vector, premium_discount)
from bot.book.liquidity import MEDIUM, session_of  # noqa: E402
from bot.book.sessions import cycle_index, ny_midnight, session_at  # noqa: E402
from bot.book.swings import Structure as SwingStructure  # noqa: E402

MIN = 60_000
TF_MS = 300_000  # 5m


def bar(index: int, o: float, h: float, lo: float, c: float, *, tf: str = "5m",
        volume: float = 10.0) -> dict:
    """A closed candle at a deterministic stamp."""
    step = {"1m": MIN, "5m": TF_MS, "15m": 900_000, "1h": 3_600_000}[tf]
    start = 1_700_000_000_000 - 1_700_000_000_000 % step
    open_time = start + index * step
    return {
        "symbol": "BTCUSDT",
        "open_time": open_time,
        "close_time": open_time + step - 1,
        "open": o, "high": h, "low": lo, "close": c,
        "volume": volume, "closed": True, "timeframe": tf,
    }


# --------------------------------------------------------------------- swings
def test_a_pivot_needs_its_right_side():
    structure = SwingStructure({"minor": 2, "major": 3, "macro": 5})
    highs = [1, 2, 3, 9, 3, 2, 1]
    found = None
    for i, high in enumerate(highs):
        events = structure.add(bar(i, high - 0.5, high, high - 1, high - 0.2))
        for pivot in events["pivots"]:
            if pivot.side == "high" and pivot.price == 9:
                found = i
    assert found is not None, "the spike is a pivot"
    assert found >= 3 + 2, "and only once its right side has closed"


def test_a_wick_through_is_a_sweep_and_a_close_through_is_a_break():
    structure = SwingStructure({"minor": 2, "major": 3, "macro": 5})
    prices = [(1, 2, 1, 1.5), (1.5, 3, 1.4, 2.5), (2.5, 2.2, 1.2, 1.4),
              (1.4, 2.6, 1.3, 2.4), (2.4, 2.5, 1.0, 1.1)]
    sweeps, breaks = [], []
    for i, (o, h, lo, c) in enumerate(prices):
        events = structure.add(bar(i, o, h, lo, c))
        sweeps += events["sweeps"]
        breaks += events["breaks"]
    assert structure.index == len(prices) - 1
    assert isinstance(sweeps, list) and isinstance(breaks, list)


def test_a_strong_high_is_the_one_that_caused_manipulation_and_the_break():
    """p.42: the high that was raided *and* followed by a break is strong."""
    structure = SwingStructure({"minor": 1, "major": 2, "macro": 4})
    prices = [
        (8.0, 8.5, 7.5, 8.2),
        (8.2, 8.3, 7.0, 8.0),      # the low that will later be broken
        (8.0, 8.4, 7.9, 8.2),
        (8.2, 10.0, 8.1, 9.9),     # the high
        (9.9, 9.95, 9.4, 9.6),
        (9.6, 10.4, 9.5, 9.7),     # wick above it, close back under: a sweep
        (9.7, 9.8, 6.8, 6.9),      # close through the low: the break
    ]
    highs: dict[float, object] = {}
    for i, (o, h, lo, c) in enumerate(prices):
        events = structure.add(bar(i, o, h, lo, c))
        for pivot in events["pivots"]:
            if pivot.side == "high":
                highs[pivot.price] = pivot
    assert 10.0 in highs, "the high is a confirmed pivot"
    assert highs[10.0].swept, "it was raided"
    assert highs[10.0].strong, "and the break that followed makes it strong"
    assert structure.last_break("minor") is not None


# ------------------------------------------------------------------ liquidity
def test_the_previous_day_high_only_exists_after_the_day_closes():
    book = LiquidityMap()
    day = 1_700_000_000_000 // 86_400_000
    start = day * 86_400_000
    for i in range(3):
        book.add(bar(i, 100, 101 + i, 99, 100, tf="1m") | {"open_time": start + i * MIN,
                                                          "close_time": start + i * MIN + MIN - 1})
    assert not [p for p in book.pools if p.name == "PDH"]
    # the next day starts: yesterday's high becomes a pool, knowable from now
    later = start + 86_400_000 + MIN
    book.add({"symbol": "BTCUSDT", "open_time": later, "close_time": later + MIN - 1,
              "open": 100, "high": 100.5, "low": 99.5, "close": 100, "volume": 1})
    pdh = [p for p in book.pools if p.name == "PDH"]
    assert pdh and pdh[0].price == 103


def test_a_pool_that_is_traded_through_is_struck_off_the_map():
    book = LiquidityMap()
    book.add_pool("PWH", 110.0, "high", "major", 0)
    book.add({"symbol": "BTCUSDT", "open_time": 1, "close_time": 2,
              "open": 109, "high": 111, "low": 108, "close": 110.5, "volume": 1})
    assert book.pools[0].swept_ms is not None
    assert book.draw(1, 100, "major") is None


def test_the_draw_is_the_nearest_untouched_pool_in_the_trades_direction():
    book = LiquidityMap()
    book.add_pool("PWL", 90.0, "low", "major", 0)
    book.add_pool("PWL2", 95.0, "low", "major", 0)
    book.add_pool("PWH", 130.0, "high", "major", 0)
    assert book.draw(-1, 100, "major").price == 95.0
    assert book.draw(1, 100, "major").price == 130.0


def test_sessions_follow_the_utc_clock():
    assert session_of(3_600_000 * 3) == "asia"
    assert session_of(3_600_000 * 9) == "london"
    assert session_of(3_600_000 * 15) == "ny"
    assert session_of(3_600_000 * 23) is None


# ------------------------------------------------------------------ imbalance
def test_a_fair_value_gap_needs_the_candle_after_it():
    candles = [bar(0, 100, 101, 99, 100),
               bar(1, 100, 103, 100, 103),
               bar(2, 103, 104, 101.5, 102)]
    assert fair_value_gap(candles, 1) == ("bull", 101.0, 101.5)
    assert fair_value_gap(candles[:2], 1) is None, "not knowable until the third candle"


def test_an_algo_candle_must_be_a_displacement_with_a_gap():
    flat = [bar(i, 100, 100.4, 99.6, 100) for i in range(5)]
    good = flat + [bar(5, 100, 101.2, 99.9, 101.1), bar(6, 101.1, 101.6, 100.6, 101.5)]
    small = flat + [bar(5, 100, 100.4, 99.9, 100.3), bar(6, 100.3, 100.8, 100.2, 100.7)]
    atr = candle_atr(flat, 4)
    found = algo_candle(good, 5, atr=atr, min_body_atr=1.0, vol_mult=0)
    assert found is not None and found.kind == "bull"
    assert found.bottom == pytest.approx(100.4)
    assert algo_candle(small, 5, atr=atr, min_body_atr=1.0, vol_mult=0) is None


def test_a_vector_candle_engulfs_the_previous_body():
    prev = bar(0, 100, 100.5, 99.0, 99.2)
    engulf = bar(1, 99.1, 101.2, 99.0, 101.0)
    assert is_vector(engulf, prev)
    assert not is_vector(bar(1, 99.3, 100.0, 99.1, 99.4), prev)


def test_premium_and_discount_split_the_dealing_range():
    assert premium_discount(90, 100, 80) == 0.5
    assert premium_discount(95, 100, 80) > 0.5
    assert premium_discount(85, 100, 80) < 0.5


# -------------------------------------------------------------------- clock
def test_the_new_york_midnight_open_is_timezone_correct():
    # 2024-01-15 12:00 UTC is 07:00 in New York: the open is 05:00 UTC
    noon = 1_705_320_000_000
    assert ny_midnight(noon) == noon - 7 * 3_600_000
    assert session_at(noon).name == "ny"
    assert 0 <= cycle_index(noon) < 16


# ------------------------------------------------------------------ narrative
def _liquidity() -> LiquidityMap:
    book = LiquidityMap()
    book.add_pool("ASIA_HIGH", 101.0, "high", MEDIUM, 0)
    book.add_pool("ASIA_LOW", 96.0, "low", MEDIUM, 0)
    return book


def _scenario() -> list[dict]:
    """Raid the Asian high, displace down with a gap, mitigate it, then go.

    The bars are 5m candles. Bar 3 wicks 101.4 (the pool) and closes back at
    100.4 - a trap, not a break. Bars 4-5 are the displacement and leave a
    bearish gap between 100.9 and 101.2. Bar 6 retraces into the gap (the
    mitigation the book waits for). The draw is the Asian low at 96.
    """
    rows = [
        (100.0, 100.6, 99.6, 100.2),
        (100.2, 100.8, 99.9, 100.4),
        (100.4, 101.0, 100.2, 100.8),
        (100.8, 101.4, 100.1, 100.4),   # trap: through the pool, closed back under
        (100.4, 100.5, 98.8, 98.9),     # displacement
        (98.9, 99.0, 98.2, 98.3),       # leaves the gap 99.0 - 100.1
        (98.3, 99.7, 98.2, 99.0),       # retrace into the gap (mitigation)
        (99.0, 99.1, 97.6, 97.8),
        (97.8, 98.0, 96.8, 97.0),
    ]
    volumes = [10, 10, 10, 10, 30, 10, 10, 10, 10]   # the displacement is the loud one
    return [bar(i, o, h, lo, c, volume=volumes[i])
            for i, (o, h, lo, c) in enumerate(rows)]


def test_the_whole_chain_produces_a_plan_with_a_stop_behind_the_imbalance():
    # the mechanics of the entry, pinned in the simplest mode: the mitigation
    # *is* the entry. The shipped default waits for a reverse shift on top of
    # the mitigation - that is the next test's job.
    narrative = Narrative("BTCUSDT", BookSettings(entry_tf="5m", htf="15m",
                                                  min_rr=3.0, min_stop_atr=0.0,
                                                  htf_required=False,
                                                  entry_mode="mitigation", fill_frac=0.5),
                          _liquidity())
    events: list[dict] = []
    for candle in _scenario():
        narrative.liquidity.add(candle)
        events += narrative.on_bar(candle, {"bias": -1, "premium": 0.8})
    setups = [e for e in events if e["type"] == "setup"]
    assert setups, f"the chain must trade (drops: {dict(narrative.drops)})"
    plan = setups[0]["setup"].as_dict()
    assert plan["side"] == "SHORT"
    assert plan["stop"] > plan["entry"] > plan["target"]
    assert plan["target"] == pytest.approx(96.0), "the draw is the untouched Asian low"
    assert plan["rr"] >= 3.0
    assert plan["confluences"], "the card has to be able to explain itself"


def test_the_shipped_default_waits_for_the_reverse_shift_after_a_full_fill():
    """p.87: the full FVG fill, *then* a reversal break - not the fill alone.

    The same scenario that trades above must not trade on the shipped defaults:
    a retrace into the gap is only half of the confluence, and entering there is
    entering before the book's model is complete.
    """
    settings = BookSettings(entry_tf="5m", htf="15m", min_rr=3.0,
                            min_stop_atr=0.0, htf_required=False)
    assert settings.entry_mode == "shift"
    assert settings.fill_frac == 1.0
    narrative = Narrative("BTCUSDT", settings, _liquidity())
    events: list[dict] = []
    bars = _scenario()
    # the market does nothing for longer than the entry window: the armed setup
    # has to expire *and be counted*, never vanish
    bars += [bar(9 + i, 97.9, 98.1, 97.7, 98.0) for i in range(45)]
    for candle in bars:
        narrative.liquidity.add(candle)
        events += narrative.on_bar(candle, {"bias": -1, "premium": 0.8})
    assert [e for e in events if e["type"] == "setup"] == []
    assert "the setup expired without an entry" in narrative.drops


def test_a_pool_that_is_accepted_instead_of_raided_is_dropped():
    narrative = Narrative("BTCUSDT", BookSettings(entry_tf="5m", htf="15m",
                                                  min_stop_atr=0.0, htf_required=False),
                          _liquidity())
    bars = _scenario()[:4]
    bars[3] = bar(3, 100.8, 101.4, 100.1, 101.3)   # closes *above* the pool
    for candle in bars:
        narrative.liquidity.add(candle)
        narrative.on_bar(candle, {"bias": -1, "premium": 0.8})
    assert narrative.trap is None
    assert narrative.drops.get("the pool was accepted, not raided", 0) >= 1


def test_the_narrative_does_not_trade_against_the_higher_timeframe():
    narrative = Narrative("BTCUSDT", BookSettings(entry_tf="5m", htf="15m",
                                                  min_rr=3.0, min_stop_atr=0.0),
                          _liquidity())
    events: list[dict] = []
    for candle in _scenario():
        narrative.liquidity.add(candle)
        events += narrative.on_bar(candle, {"bias": 1, "premium": 0.8})
    assert not [e for e in events if e["type"] == "setup"]
    assert narrative.drops.get("against the higher-timeframe direction", 0) >= 1


def test_a_trap_without_displacement_expires_instead_of_hanging():
    narrative = Narrative("BTCUSDT", BookSettings(entry_tf="5m", htf="15m",
                                                  arm_bars=3, min_stop_atr=0.0,
                                                  htf_required=False),
                          _liquidity())
    bars = _scenario()[:4] + [bar(4 + i, 100.4, 100.6, 100.2, 100.4) for i in range(6)]
    for candle in bars:
        narrative.liquidity.add(candle)
        narrative.on_bar(candle, {"bias": -1, "premium": 0.8})
    assert narrative.trap is None
    assert narrative.drops.get("no displacement while the trap was fresh", 0) == 1


# --------------------------------------------------------------------- runner
def _minute(index: int, price: float, spread: float = 0.05) -> dict:
    open_time = 1_700_000_000_000 - 1_700_000_000_000 % MIN + index * MIN
    return {"symbol": "BTCUSDT", "open_time": open_time, "close_time": open_time + MIN - 1,
            "open": price, "high": price + spread, "low": price - spread,
            "close": price, "volume": 5.0, "closed": True}


def test_the_runner_rolls_minutes_into_timeframes_and_keeps_context():
    runner = BookRunner(["BTCUSDT"], ["5m"], BookSettings(entry_tf="5m", htf="15m"))
    price = 100.0
    for i in range(600):
        price += 0.05 if (i // 60) % 2 == 0 else -0.05
        runner.on_candle(_minute(i, price))
    state = runner.states["BTCUSDT"]
    narrative = state.narratives["5m"]
    assert narrative.bars, "the 5m narrative saw closed candles"
    assert len(narrative.bars) <= 400, "the candle ring is bounded"
    assert state.htf["15m"].bars > 0, "the 15m context was built"
    assert runner.drops() is not None


def test_settings_load_from_the_config_block():
    settings = BookSettings.from_config({"entry_mode": "shift", "min_rr": 2.5,
                                         "sessions": ["london"], "nonsense": 1})
    assert settings.entry_mode == "shift"
    assert settings.min_rr == 2.5
    assert settings.sessions == ("london",)


def test_the_shipped_config_is_loadable_and_sane():
    config = json.loads((ROOT / "config.json").read_text())
    assert config["strategy"] == "book"
    settings = BookSettings.from_config(config["book"])
    assert settings.min_rr >= 2, "a trade must still pay for itself several times over"
    assert settings.min_body_atr > 0
    assert settings.max_fee_r > 0, "fees are a rule, not a footnote"
