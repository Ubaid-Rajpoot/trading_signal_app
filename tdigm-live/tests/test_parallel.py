"""Watching 500 instruments must not be a queue of 500 errands.

The complaint is "no setup for hours". A feed that warms one instrument at a time
is a big part of that: 500 symbols x 5 timeframes of REST calls, serial, and the
higher timeframes only start looking once their turn comes. These tests pin the
pool that replaced it - and the rule that a slow or broken instrument never holds
the rest of the queue.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.engine import Engine  # noqa: E402


def bars(n: int = 200, step: float = 1.0) -> list[dict]:
    out, price, t = [], 100.0, 1_600_000_000_000
    for i in range(n):
        nxt = price + step * (1 if i % 3 else -1)
        out.append({"open_time": t, "close_time": t + 59_999, "open": price,
                    "high": max(price, nxt) + 0.1, "low": min(price, nxt) - 0.1,
                    "close": nxt, "volume": 10.0, "closed": True})
        price, t = nxt, t + 60_000
    return out


class TimedFeed:
    """A feed whose history costs latency, and that records how many are in flight."""

    source = "live"
    name = "timed"
    market = "futures"
    connected = True

    def __init__(self, delay: float = 0.01, bad: set[str] | None = None) -> None:
        self.delay = delay
        self.bad = bad or set()
        self.in_flight = 0
        self.peak = 0
        self.calls = 0
        self.limits: list[int] = []

    def exchange_interval(self, tf: str) -> str:
        return tf

    async def history(self, symbol: str, interval: str, limit: int = 500):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.calls += 1
        self.limits.append(limit)
        try:
            await asyncio.sleep(self.delay)
            if symbol in self.bad:
                raise RuntimeError("HTTP 429 Too Many Requests")
            return bars()
        finally:
            self.in_flight -= 1


def engine_over(symbols: list[str], timeframes: list[str]) -> Engine:
    return Engine(
        symbols=symbols,
        timeframes=timeframes,
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1,
                      "slow_period": 9, "band_mult": 1.6185},
        filters={"structure": True, "hvb": False, "trend_context": False, "require_bos": False},
        structure={"pivot_len": 2, "sweep_max_wait": 60},
        strategy="sweep",
    )


@pytest.mark.asyncio
async def test_warm_up_runs_thirty_instruments_at_once():
    symbols = [f"COIN{i}USDT" for i in range(60)]
    feed = TimedFeed(delay=0.02)
    engine = engine_over(symbols, ["5m"])

    await engine.warmup(feed, concurrency=30)

    assert feed.peak == 30, f"expected a pool of 30, saw {feed.peak} in flight"
    assert feed.calls == 60
    assert engine.warming() == 0, "every instrument should have history afterwards"


@pytest.mark.asyncio
async def test_fast_startup_depth_does_not_replace_the_configured_deep_depth():
    feed = TimedFeed(delay=0)
    engine = engine_over(["BTCUSDT"], ["5m"])
    assert engine.history_bars == 5000

    await engine.warmup(feed, concurrency=1, history_bars=240)
    assert feed.limits == [240]
    assert engine.history_bars == 5000, "the fast pass must not weaken later deep rescans"

    await engine.rescan(feed, concurrency=1)
    assert feed.limits[-1] == 5000


def test_deep_history_uses_calendar_coverage_without_slowing_the_fast_pass():
    engine = Engine(
        symbols=["BTCUSDT"],
        timeframes=["1m", "3m", "5m", "15m"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1,
                      "slow_period": 9, "band_mult": 1.6185},
        history_bars=5000,
        history_days=30,
    )

    assert engine._history_depth("1m", 5000, calendar=True) == 43_200
    assert engine._history_depth("3m", 5000, calendar=True) == 14_400
    assert engine._history_depth("5m", 5000, calendar=True) == 8_640
    assert engine._history_depth("15m", 5000, calendar=True) == 5000
    assert engine._history_depth("1m", 1000, calendar=False) == 1000


@pytest.mark.asyncio
async def test_calendar_depth_is_used_by_deep_rescan_but_not_startup():
    feed = TimedFeed(delay=0)
    engine = Engine(
        symbols=["BTCUSDT"],
        timeframes=["5m"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1,
                      "slow_period": 9, "band_mult": 1.6185},
        filters={"structure": True, "hvb": True},
        history_bars=5000,
        history_days=30,
    )

    await engine.warmup(feed, concurrency=1, history_bars=1000)
    assert feed.limits == [1000]
    await engine.rescan(feed, concurrency=1)
    assert feed.limits[-1] == 8640


def test_decision_funnel_separates_setups_from_overlapping_rejection_hits():
    engine = engine_over(["BTCUSDT"], ["3m"])
    cell = engine.cells[("BTCUSDT", "3m")]
    cell.raw_setups = 3
    cell.hvb_aligned_setups = 2
    cell.filtered_setups = 2
    cell.passed_setups = 1
    cell.first_rejections = {"unclear structure": 2}
    cell.rejection_reasons = {"unclear structure": 2, "no HVB": 1, "low ADX": 1}
    cell.rejection_combinations = {
        "unclear structure + no HVB": 1,
        "unclear structure + low ADX": 1,
    }

    funnel = engine.decision_funnel()
    assert funnel["setups"] == 3
    assert funnel["filtered"] == 2 and funnel["passed"] == 1
    assert sum(funnel["first_rejections"].values()) == funnel["filtered"]
    assert sum(funnel["rejection_hits"].values()) == 4
    assert funnel["by_timeframe"]["3m"]["first_rejections"] == {"unclear structure": 2}


@pytest.mark.asyncio
async def test_one_broken_instrument_does_not_hold_the_queue():
    symbols = ["BADUSDT"] + [f"COIN{i}USDT" for i in range(29)]
    feed = TimedFeed(delay=0.005, bad={"BADUSDT"})
    engine = engine_over(symbols, ["5m"])

    await asyncio.wait_for(engine.warmup(feed, concurrency=30), timeout=15)

    warm = [s for s in symbols[1:] if engine.cells.get((s, "5m")) and len(engine.cells[(s, "5m")].candles) >= 120]
    assert len(warm) == 29, "the healthy instruments must still be warmed"
    assert engine.warm_failures, "and the failure must be recorded, not swallowed"


@pytest.mark.asyncio
async def test_a_rescan_uses_the_same_pool():
    symbols = [f"COIN{i}USDT" for i in range(40)]
    feed = TimedFeed(delay=0.01)
    engine = engine_over(symbols, ["5m"])
    await engine.warmup(feed, concurrency=30)

    feed.peak = 0
    await engine.rescan(feed, concurrency=30)
    assert feed.peak == 30, f"rescan should also keep 30 in flight, saw {feed.peak}"


@pytest.mark.asyncio
async def test_live_closed_bars_wait_for_history_and_are_drained_in_order():
    feed = TimedFeed(delay=0.05)
    engine = engine_over(["BTCUSDT"], ["1m"])
    engine.prepare_warmup(["BTCUSDT"])
    warming = asyncio.create_task(engine.warm_symbol(feed, "BTCUSDT"))
    while feed.in_flight == 0:
        await asyncio.sleep(0)

    live = bars()[-1] | {"symbol": "BTCUSDT"}
    live["open_time"] += 60_000
    live["close_time"] += 60_000
    await engine.on_candle(live)
    assert engine.candles_seen == 0, "the live close is buffered while its cell is rebuilt"

    await warming
    cell = engine.cells[("BTCUSDT", "1m")]
    assert cell.last_close_time == live["close_time"]
    assert engine.candles_seen == 1


@pytest.mark.asyncio
async def test_a_reconnect_cannot_apply_the_same_closed_bar_twice():
    engine = engine_over(["BTCUSDT"], ["1m"])
    candle = bars(1)[0] | {"symbol": "BTCUSDT"}
    await engine.on_candle(candle)
    await engine.on_candle(dict(candle))
    assert engine.candles_seen == 1
    assert len(engine.cells[("BTCUSDT", "1m")].candles) == 1
