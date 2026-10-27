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

from bot.feed import (  # noqa: E402
    HTTP_HEADERS,
    CompositeFeed,
    FeedError,
    MexcFuturesFeed,
    is_futures,
    market_of,
    to_futures,
    to_spot,
)


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



# ------------------------------------------------- round 9: futures, on its own
def test_futures_only_builds_no_spot_feed_at_all():
    """The app is a futures scanner now: with no spot symbols there is no spot
    socket, no spot candles and nothing to warm."""
    from bot.feed import make_market_feed

    feed = make_market_feed("mexc", [], ["BTC_USDT", "ETH_USDT"])
    assert isinstance(feed, MexcFuturesFeed), type(feed).__name__
    assert feed.symbols == ["BTC_USDT", "ETH_USDT"]


def test_the_contract_interval_is_a_minute_window_not_always_one_minute():
    """The warm-up used to ask the contract API for ``Min15`` while computing the
    window with 60-second candles: 15m got ~66 bars instead of 1000, never reached
    the 240-bar lookback, and produced (almost) no zones. That is why futures
    looked empty next to spot."""
    from bot.feed import future_interval, future_seconds

    assert future_interval("5m") == "Min5"
    assert future_interval("15m") == "Min15"
    assert future_interval("1h") == "Min60"
    assert future_interval("Min5") == "Min5", "the exchange's own name is accepted too"
    assert future_interval("3m") is None, "MEXC has no 3m contract interval"
    assert future_seconds("Min5") == 300 and future_seconds("5m") == 300
    assert future_seconds("Min15") == 900 and future_seconds("Min1") == 60


# --------------------------------------- round 13: the body a decoder cannot read
def _fake_client(responses):
    """A stand-in for ``http_client()`` that answers with **raw bytes**.

    The transport no longer lets httpx decode anything (that is the fix), so the
    double has to look like the real thing: a stream of bytes plus the headers
    the exchange sent, including a ``content-encoding`` it may be lying about.
    """
    seen: list[dict] = []

    class FakeResponse:
        def __init__(self, status, body, error=None, encoding=""):
            self.status_code = status
            self._body = body
            self._error = error
            self.headers = {"content-encoding": encoding} if encoding else {}

        async def aiter_raw(self):
            if self._error:
                raise self._error
            if isinstance(self._body, bytes):
                payload = self._body
            else:
                import json as _json

                payload = _json.dumps(self._body).encode()
            half = max(1, len(payload) // 2)
            for chunk in (payload[:half], payload[half:]):
                if chunk:
                    yield chunk

    class FakeStream:
        def __init__(self, response):
            self._response = response

        async def __aenter__(self):
            return self._response

        async def __aexit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, **kwargs):
            self.headers = dict(kwargs.get("headers") or {})
            seen.append(self.headers)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, params=None):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return FakeStream(FakeResponse(*item))

    return FakeClient, seen


def test_the_scanner_never_asks_the_exchange_for_brotli():
    """The exact failure the user saw on "add symbol"::

        DecodingError: brotli: decoder process called with data when
        'can_accept_more_data()' is False

    Two halves to the fix: the scanner does not ask for ``br``, and - because the
    exchange may send it anyway - the transport reads raw bytes and decodes them
    itself, so there is no stateful decoder to get stuck half way.
    """
    assert "br" not in [v.strip() for v in HTTP_HEADERS["accept-encoding"].split(",")]
    assert "gzip" in HTTP_HEADERS["accept-encoding"]


def test_a_plain_json_body_labelled_as_brotli_is_read():
    """The real MEXC behaviour behind the crash: valid JSON, ``content-encoding: br``.

    Handed to a brotli decoder this raises "can_accept_more_data() is False"
    (there is nothing for the decoder to accept). It is plain JSON, so it is read.
    """
    from bot.feed import decode_body

    body = b'{"success":true,"data":[{"symbol":"BTC_USDT"}]}'
    assert decode_body(body, "br") == body
    assert decode_body(b"\n  " + body, "gzip") == b"\n  " + body


def test_a_really_brotli_compressed_body_is_decompressed(monkeypatch):
    brotli = pytest.importorskip("brotli")
    import bot.feed as feed_mod

    payload = b'{"ok":1}'
    FakeClient, _ = _fake_client([(200, brotli.compress(payload), None, "br")])
    monkeypatch.setattr(feed_mod, "http_client", FakeClient)
    feed = MexcFuturesFeed(["BTC_USDT"])
    assert asyncio.run(feed._get_json("https://contract.mexc.com/x")) == {"ok": 1}  # noqa: SLF001


def test_a_gzip_body_is_decompressed(monkeypatch):
    import gzip

    import bot.feed as feed_mod

    FakeClient, _ = _fake_client([(200, gzip.compress(b'{"ok":2}'), None, "gzip")])
    monkeypatch.setattr(feed_mod, "http_client", FakeClient)
    feed = MexcFuturesFeed(["BTC_USDT"])
    assert asyncio.run(feed._get_json("https://contract.mexc.com/x")) == {"ok": 2}  # noqa: SLF001


def test_a_body_that_stays_broken_names_the_url(monkeypatch):
    import bot.feed as feed_mod

    FakeClient, seen = _fake_client([(200, b"\x00\x01\x02 not json", None, "br")] * 2)
    monkeypatch.setattr(feed_mod, "http_client", FakeClient)
    feed = MexcFuturesFeed(["BTC_USDT"])
    with pytest.raises(FeedError) as err:
        asyncio.run(feed._get_json("https://contract.mexc.com/api/v1/contract/detail"))  # noqa: SLF001
    assert "could not be read" in str(err.value)
    assert "contract/detail" in str(err.value)
    assert "DecodingError" not in str(err.value), "a decoder name is no longer the message"
    assert len(seen) == 2, "a broken body is retried once"


def test_a_client_error_is_not_retried(monkeypatch):
    import bot.feed as feed_mod

    FakeClient, seen = _fake_client([(404, {"error": "nope"}, None)])
    monkeypatch.setattr(feed_mod, "http_client", FakeClient)
    feed = MexcFuturesFeed(["BTC_USDT"])
    with pytest.raises(FeedError) as err:
        asyncio.run(feed._get_json("https://contract.mexc.com/x", attempts=3))  # noqa: SLF001
    assert "HTTP 404" in str(err.value)
    assert len(seen) == 1, "a 404 costs one request, not three"


def test_a_rate_limit_is_retried(monkeypatch):
    import bot.feed as feed_mod

    FakeClient, seen = _fake_client([(429, {"error": "slow down"}, None), (200, {"ok": 1}, None)])
    monkeypatch.setattr(feed_mod, "http_client", FakeClient)
    feed = MexcFuturesFeed(["BTC_USDT"])
    assert asyncio.run(feed._get_json("https://contract.mexc.com/y")) == {"ok": 1}  # noqa: SLF001
    assert len(seen) == 2


def test_adding_a_symbol_never_500s_when_the_exchange_body_is_broken(monkeypatch):
    """The user-visible end of the decoder bug.

    The toast came from ``POST /api/universe/add`` returning the raw string
    ``DecodingError: brotli: ...``. The cause is fixed above; this pins the API
    contract around it: a broken exchange body is reported as data (HTTP 200,
    ``ok: false``, a readable error) and the app keeps running with the symbols it
    already has.
    """
    import httpx

    from bot.engine import Engine
    from bot.map import DEFAULTS
    from bot.server import create_app
    import tests.test_engine as engine_tests

    class BrokenUniverse(engine_tests.FakeFeed):
        universe_errors = {}

        async def universe(self):
            raise RuntimeError("DecodingError: brotli: decoder process called with data when "
                               "'can_accept_more_data()' is False")

    rows = engine_tests.load(600)
    engine = Engine(symbols=["BTC_USDT"], timeframes=["5m"], settings=dict(DEFAULTS))
    feed = BrokenUniverse(rows)
    config = {"symbols": [], "futures_symbols": ["BTC_USDT"], "timeframes": ["5m"],
              "settings": {}, "markets": ["futures"]}
    app = create_app(config, feed, engine, None)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            listed = await client.get("/api/universe")
            added = await client.post("/api/universe/add", json={"preset": "top50", "market": "futures"})
            state = await client.get("/api/state")
        return listed, added, state

    listed, added, state = asyncio.run(go())
    assert listed.status_code == 200 and added.status_code == 200, "a broken exchange list is not a 5xx"
    assert "DecodingError" in (listed.json()["error"] or "")
    body = added.json()
    assert body["ok"] is False and "brotli" in body["error"]
    assert state.status_code == 200 and state.json()["symbols"] == ["BTC_USDT"], "the scan keeps going"


def test_the_contract_list_survives_an_exchange_that_lies_about_brotli():
    """End to end over a real socket: MEXC's actual bad habit.

    A fake exchange answers ``/api/v1/contract/detail`` with plain JSON under
    ``content-encoding: br``, and ``/ticker`` the same. The old transport raised
    ``DecodingError: brotli: decoder process called with data ...`` and the page
    showed it as a toast; now the list simply arrives.
    """
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    detail = {"success": True, "data": [
        {"symbol": "BTC_USDT", "baseCoin": "BTC", "quoteCoin": "USDT", "maxLeverage": 200},
        {"symbol": "TON_USDT", "baseCoin": "TON", "quoteCoin": "USDT", "maxLeverage": 50},
    ]}
    tickers = {"success": True, "data": [
        {"symbol": "BTC_USDT", "lastPrice": 100000, "riseFallRate": 0.01, "amount24": 9e9},
        {"symbol": "TON_USDT", "lastPrice": 3.2, "riseFallRate": -0.02, "amount24": 2e8},
    ]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = detail if "detail" in self.path else tickers
            raw = _json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-encoding", "br")      # the lie
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        import bot.feed as feed_mod

        old = feed_mod.MEXC_CONTRACT_REST
        feed_mod.MEXC_CONTRACT_REST = f"http://127.0.0.1:{server.server_port}"
        try:
            feed = MexcFuturesFeed(["BTC_USDT"])
            items = asyncio.run(feed.universe())
        finally:
            feed_mod.MEXC_CONTRACT_REST = old
    finally:
        server.shutdown()

    assert [i["symbol"] for i in items] == ["BTC_USDT", "TON_USDT"]
    assert items[0]["turnover"] == 9e9 and items[0]["price"] == 100000
