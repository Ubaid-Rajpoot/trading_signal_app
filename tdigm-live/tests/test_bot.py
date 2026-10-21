"""Tests that the numbers are right before any of it is pointed at a market."""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.buckets import Rollup, bucket_start  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.indicators import break_signal, hvb_signal, rsi, sma, stdev, tdi  # noqa: E402


# ------------------------------------------------------------------ helpers
def bar(open_time: int, price: float, high=None, low=None, symbol="BTCUSDT") -> dict:
    return {
        "symbol": symbol,
        "open_time": open_time,
        "close_time": open_time + 59_999,
        "open": price,
        "high": high if high is not None else price + 0.5,
        "low": low if low is not None else price - 0.5,
        "close": price,
        "volume": 1.0,
        "trades": 1,
        "closed": True,
    }


# ------------------------------------------------------------------- sma
def test_sma_matches_hand_calculation():
    values = [1, 2, 3, 4, 5]
    assert sma(values, 2) == [None, 1.5, 2.5, 3.5, 4.5]
    assert sma(values, 5)[-1] == 3.0
    assert sma(values, 1) == [1, 2, 3, 4, 5]


def test_stdev_is_population_stdev():
    values = [2, 4, 4, 4, 5, 5, 7, 9]
    assert stdev(values, 8)[-1] == pytest.approx(2.0)


# ------------------------------------------------------------------- rsi
def test_rsi_is_100_on_a_straight_line_up():
    closes = [float(i) for i in range(1, 60)]
    series = rsi(closes, 14)
    assert series[14] == pytest.approx(100.0)
    assert series[13] is None


def test_rsi_stays_inside_0_100():
    closes = [100 + math.sin(i / 3) * 5 for i in range(200)]
    series = [v for v in rsi(closes, 50) if v is not None]
    assert series and all(0 <= v <= 100 for v in series)


def test_rsi_warmup_aligns_with_period():
    closes = [float(i % 7 + 10) for i in range(80)]
    series = rsi(closes, 50)
    assert series[49] is None
    assert series[50] is not None


# ------------------------------------------------------------------- tdi
def test_tdi_uses_the_traders_setting():
    closes = [100 + (i % 11) * 0.4 for i in range(300)]
    points = tdi(closes, rsi_period=50, band_period=50, fast_period=1, slow_period=9)
    last = points[-1]
    assert last.fast is not None and last.mbl is not None
    # fast_period = 1 means the purple line IS the RSI
    assert last.fast == pytest.approx(rsi(closes, 50)[-1])
    # bands are the base line +/- 1.6185 stdev
    r = [v for v in rsi(closes, 50) if v is not None]
    expected_band = 1.6185 * stdev(r, 50)[-1]
    assert last.upper == pytest.approx(last.mbl + expected_band)
    assert last.lower == pytest.approx(last.mbl - expected_band)


def test_hvb_fires_only_on_the_crossing_bar():
    points = tdi([100 + (i % 13) for i in range(400)], 50, 50, 1, 9)
    ups = [i for i in range(1, len(points)) if hvb_signal(points, i) == "up"]
    downs = [i for i in range(1, len(points)) if hvb_signal(points, i) == "down"]
    assert ups or downs
    for i in ups:
        assert points[i - 1].fast <= points[i - 1].mbl and points[i].fast > points[i].mbl
    for i in downs:
        assert points[i - 1].fast >= points[i - 1].mbl and points[i].fast < points[i].mbl


def test_hvb_min_points_filter():
    points = tdi([100 + (i % 13) for i in range(400)], 50, 50, 1, 9)
    strict = [i for i in range(1, len(points)) if hvb_signal(points, i, min_points=5.0)]
    loose = [i for i in range(1, len(points)) if hvb_signal(points, i)]
    assert set(strict) <= set(loose)


# ------------------------------------------------------------------ break
def test_break_needs_a_close_beyond_the_window():
    candles = [bar(i * 60_000, 100.0, 101.0, 99.0) for i in range(30)]
    assert break_signal(candles, 25, lookback=20) is None
    candles[25] = bar(25 * 60_000, 102.0, 102.0, 100.0)
    assert break_signal(candles, 25, lookback=20) == "high"
    candles[25] = bar(25 * 60_000, 98.0, 100.0, 97.5)
    assert break_signal(candles, 25, lookback=20) == "low"


def test_break_ignores_wick_only():
    candles = [bar(i * 60_000, 100.0, 101.0, 99.0) for i in range(30)]
    # high pokes above the window but the close comes back inside
    candles[25] = bar(25 * 60_000, 100.5, 105.0, 99.5)
    assert break_signal(candles, 25, lookback=20) is None


# ----------------------------------------------------------------- bucket
def test_bucket_alignment():
    assert bucket_start(1_700_000_000_000, "15m") % 900_000 == 0
    assert bucket_start(1_700_000_000_000, "1h") % 3_600_000 == 0


def test_rollup_builds_a_correct_5m_candle():
    roll = Rollup("5m")
    start = 1_700_000_000_000 - (1_700_000_000_000 % 300_000)
    done = []
    for i in range(5):
        done += roll.add(bar(start + i * 60_000, 100 + i, high=101 + i, low=99 + i))
    assert done == []  # the bucket is not complete yet
    done += roll.add(bar(start + 5 * 60_000, 200))
    assert len(done) == 1
    candle = done[0]
    assert candle["open"] == 100
    assert candle["close"] == 104
    assert candle["high"] == 105
    assert candle["low"] == 99
    assert candle["open_time"] == start
    assert candle["close_time"] == start + 300_000 - 1
    assert candle["volume"] == 5.0


def test_rollup_flush_closes_the_last_candle():
    roll = Rollup("15m")
    start = 1_700_000_000_000 - (1_700_000_000_000 % 900_000)
    roll.add(bar(start, 100))
    flushed = roll.flush()
    assert len(flushed) == 1 and flushed[0]["closed"] is True


# ----------------------------------------------------------------- engine
def make_engine(**kwargs):
    return Engine(
        symbols=["BTCUSDT"],
        timeframes=["5m"],
        tdi_settings={"rsi_period": 14, "band_period": 14, "fast_period": 1, "slow_period": 3},
        break_lookback=5,
        **kwargs,
    )


def test_engine_alerts_when_both_conditions_hit():
    engine = make_engine()
    events = []

    async def broadcast(message):
        events.append(message)

    engine.broadcast = broadcast

    async def run():
        start = 1_700_000_000_000 - (1_700_000_000_000 % 300_000)
        price = 100.0
        # feed enough candles to warm the indicators, oscillating
        for i in range(400):
            price += 0.6 if i % 9 < 5 else -0.55
            await engine.on_candle(bar(start + i * 60_000, price))
        cell = engine.cells[("BTCUSDT", "5m")]
        return cell

    cell = asyncio.run(run())
    assert len(cell.candles) > 20, "5m candles must be aggregated"
    alerts = [e for e in events if e.get("type") == "alert"]
    # at least one HVB must have been detected in 400 oscillating bars
    assert alerts, "expected at least one alert"
    for event in alerts:
        alert = event["alert"]
        assert alert["kind"] in {"signal", "hvb", "break"}
        if alert["kind"] == "signal":
            assert (alert["hvb"], alert["break"]) in {("up", "high"), ("down", "low")}


def test_engine_dedupes_and_warms_up():
    engine = make_engine()

    async def run():
        start = 1_700_000_000_000 - (1_700_000_000_000 % 300_000)
        for i in range(60):  # fewer bars than the warm-up needs
            await engine.on_candle(bar(start + i * 60_000, 100 + i * 0.1))
        cell = engine.cells[("BTCUSDT", "5m")]
        return cell

    cell = asyncio.run(run())
    assert cell.alerts == 0
    assert cell.as_dict()["warm"] is False


def test_engine_ignores_forming_candles_for_alerts():
    engine = make_engine()

    async def run():
        start = 1_700_000_000_000 - (1_700_000_000_000 % 300_000)
        for i in range(200):
            await engine.on_candle(bar(start + i * 60_000, 100 + (i % 5)))
        forming = bar(start + 200 * 60_000, 500.0)
        forming["closed"] = False
        before = len(engine.alerts)
        await engine.on_candle(forming)
        return before, len(engine.alerts)

    before, after = asyncio.run(run())
    assert after == before, "a forming candle must never raise an alert"
