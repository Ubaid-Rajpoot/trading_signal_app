"""The feed in front of two markets - the bug that made futures look empty.

Round 6 shipped a ``CompositeFeed`` whose ``candles()`` was the *replay*
implementation (it iterated ``self.files``) and which had no ``universe()`` at
all. In live mode that meant: spot and futures symbols were accepted, the page
said they were streaming, and **not one candle ever arrived** - and adding a
symbol from the exchange list raised. These tests pin both halves down with two
fake sub-feeds, so the real MEXC sockets are not needed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.feed import CompositeFeed, FeedError, is_futures, market_of, to_futures, to_spot  # noqa: E402


class FakeFeed:
    """A minimal sub-feed: a few candles and a symbol list, then it stops."""

    source = "live"
    name = "fake"
    chunk_size = 100
    tf_map = {"1m": "1m"}

    def __init__(self, symbols, candles, universe=None, fail_universe=False):
        self._symbols = list(symbols)
        self._candles = list(candles)
        self._universe = universe or []
        self.fail_universe = fail_universe
        self.connected = True
        self.status = "fake"
        self.messages = 0
        self.parsed = 0
        self.frames = 0
        self.samples: list[str] = []

    @property
    def symbols(self):
        return list(self._symbols)

    def add_symbols(self, symbols):
        added = [s for s in symbols if s not in self._symbols]
        self._symbols += added
        return added

    def remove_symbols(self, symbols):
        drop = set(symbols)
        gone = [s for s in self._symbols if s in drop]
        self._symbols = [s for s in self._symbols if s not in drop]
        return gone

    def exchange_interval(self, timeframe, symbol=None):
        return timeframe

    def force_reconnect(self):
        return None

    async def candles(self):
        for candle in self._candles:
            yield candle
            await asyncio.sleep(0)

    async def universe(self):
        if self.fail_universe:
            raise FeedError("contract.mexc.com timed out")
        return list(self._universe)

    async def history(self, symbol, interval, limit=500, include_open=False):
        return [c for c in self._candles if c["symbol"] == symbol][-limit:]


def candle(symbol, t, price=100.0):
    return {"symbol": symbol, "open_time": t, "close_time": t + 59_999,
            "open": price, "high": price + 1, "low": price - 1, "close": price, "closed": True}


def test_a_composite_feed_streams_both_markets_at_once():
    spot = FakeFeed(["BTCUSDT"], [candle("BTCUSDT", 1000), candle("BTCUSDT", 1060)])
    futures = FakeFeed(["BTC_USDT"], [candle("BTC_USDT", 1000, 200.0)])
    comp = CompositeFeed({"spot": spot, "futures": futures})

    async def go():
        got = []
        async for c in comp.candles():
            got.append(c)
            if len(got) == 3:
                break
        return got

    got = asyncio.run(asyncio.wait_for(go(), timeout=10))
    assert len(got) == 3, "every candle of both feeds arrives on the one stream"
    assert {c["symbol"] for c in got} == {"BTCUSDT", "BTC_USDT"}, "both markets are represented"


def test_a_composite_feed_keeps_streaming_after_one_market_ends():
    """One feed finishing must not silence the other (the replay-forever case)."""
    spot = FakeFeed(["BTCUSDT"], [candle("BTCUSDT", 1000)])
    futures = FakeFeed(["BTC_USDT"], [candle("BTC_USDT", 1000), candle("BTC_USDT", 1060)])
    comp = CompositeFeed({"spot": spot, "futures": futures})

    async def go():
        got = []
        async for c in comp.candles():
            got.append(c)
            if c["symbol"] == "BTC_USDT" and len([x for x in got if x["symbol"] == "BTC_USDT"]) == 2:
                break
        return got

    got = asyncio.run(asyncio.wait_for(go(), timeout=10))
    assert len([c for c in got if c["symbol"] == "BTC_USDT"]) == 2


def test_the_composite_universe_covers_both_markets_and_tags_them():
    spot = FakeFeed(["BTCUSDT"], [], universe=[{"symbol": "BTCUSDT", "turnover": 5.0}])
    futures = FakeFeed(["BTC_USDT"], [], universe=[{"symbol": "BTC_USDT", "turnover": 9.0}])
    comp = CompositeFeed({"spot": spot, "futures": futures})
    items = asyncio.run(comp.universe())
    assert [i["market"] for i in items] == ["futures", "spot"], "biggest turnover first, tagged"
    assert {i["symbol"] for i in items} == {"BTCUSDT", "BTC_USDT"}


def test_one_broken_market_does_not_hide_the_other():
    """Adding a symbol used to fail outright when one market could not be listed."""
    spot = FakeFeed(["BTCUSDT"], [], universe=[{"symbol": "BTCUSDT", "turnover": 5.0}])
    futures = FakeFeed(["BTC_USDT"], [], fail_universe=True)
    comp = CompositeFeed({"spot": spot, "futures": futures})
    items = asyncio.run(comp.universe())
    assert [i["symbol"] for i in items] == ["BTCUSDT"]
    assert "futures" in comp.universe_errors and "timed out" in comp.universe_errors["futures"]


def test_a_completely_unreachable_exchange_raises_one_readable_error():
    comp = CompositeFeed({"spot": FakeFeed([], [], fail_universe=True),
                          "futures": FakeFeed([], [], fail_universe=True)})
    with pytest.raises(FeedError) as exc:
        asyncio.run(comp.universe())
    assert "spot" in str(exc.value) and "futures" in str(exc.value)


def test_symbol_names_carry_the_market():
    assert market_of("BTCUSDT") == "spot"
    assert market_of("BTC_USDT") == "futures"
    assert is_futures("ETH_USDT") and not is_futures("ETHUSDT")
    assert to_futures("BTCUSDT") == "BTC_USDT"
    assert to_futures("BTC_USDT") == "BTC_USDT", "already tagged: left alone"
    assert to_spot("BTC_USDT") == "BTCUSDT"


def test_adding_a_symbol_reaches_the_market_that_owns_it():
    """The round-7 complaint: "adding a symbol throws an error".

    A composite feed has no symbol list of its own - it has to hand each new name
    to the sub-feed for its market, and a futures contract must not be pushed
    into the spot feed.
    """
    spot = FakeFeed(["BTCUSDT"], [])
    futures = FakeFeed(["BTC_USDT"], [])
    comp = CompositeFeed({"spot": spot, "futures": futures})
    added = comp.add_symbols(["ETHUSDT", "SOL_USDT", "BTCUSDT"])
    assert added == ["ETHUSDT", "SOL_USDT"]
    assert spot.symbols == ["BTCUSDT", "ETHUSDT"]
    assert futures.symbols == ["BTC_USDT", "SOL_USDT"]
    assert set(comp.symbols) == {"BTCUSDT", "ETHUSDT", "BTC_USDT", "SOL_USDT"}


def test_removing_a_symbol_takes_it_out_of_every_market():
    spot = FakeFeed(["BTCUSDT", "ETHUSDT"], [])
    futures = FakeFeed(["BTC_USDT"], [])
    comp = CompositeFeed({"spot": spot, "futures": futures})
    removed = comp.remove_symbols(["ETHUSDT", "BTC_USDT"])
    assert removed == ["BTC_USDT", "ETHUSDT"]
    assert spot.symbols == ["BTCUSDT"] and futures.symbols == []
    assert comp.symbols == ["BTCUSDT"]

