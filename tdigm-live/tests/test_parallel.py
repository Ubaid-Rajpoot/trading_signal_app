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

    def exchange_interval(self, tf: str) -> str:
        return tf

    async def history(self, symbol: str, interval: str, limit: int = 500):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.calls += 1
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
