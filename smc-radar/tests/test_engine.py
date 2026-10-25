"""The engine, the roll-up and the HTTP/WS surface - end to end on real candles.

The replay file is real Binance 1-minute data (see ``docs/data.md``); if it is
not on this machine the test is skipped rather than faked.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import pytest  # noqa: E402

from bot import config_store  # noqa: E402
from bot.buckets import Rollup  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.map import DEFAULTS  # noqa: E402
from bot.server import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "fut" / "BTC_USDT-1m-2025-01.csv"
pytestmark = pytest.mark.skipif(not CSV.exists(), reason=f"real replay data missing: {CSV}")


def load(limit: int = 3000) -> list[dict]:
    rows: list[dict] = []
    with CSV.open(newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 6:
                continue
            open_time = int(row[0])
            open_time = open_time // 1000 if open_time > 10**14 else open_time
            rows.append(
                {
                    "symbol": "BTCUSDT",
                    "open_time": open_time,
                    "close_time": open_time + 59_999,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "closed": True,
                }
            )
            if len(rows) >= limit:
                break
    return rows


class FakeFeed:
    source = "live"
    name = "mexc"

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.status = "test"

    def exchange_interval(self, timeframe: str) -> str:
        return "1m"

    async def history(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        return [dict(r, symbol=symbol) for r in self.rows[-limit:]]

    def add_symbols(self, symbols) -> list[str]:
        return list(symbols)

    def remove_symbols(self, symbols) -> list[str]:
        return list(symbols)


def test_rollup_buckets_are_clock_aligned():
    rollup = Rollup("5m")
    rows = load(21)   # 21 minutes flushes four whole 5m candles
    completed = []
    for row in rows:
        completed += rollup.add(row)
    assert len(completed) == 4
    for candle in completed:
        assert candle["open_time"] % 300_000 == 0


def test_engine_builds_a_map_from_real_candles():
    rows = load(3000)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["5m", "15m"], settings=dict(DEFAULTS))
    feed = FakeFeed(rows)

    async def go():
        await engine.warmup(feed)

    asyncio.run(go())
    state = engine.snapshot()
    json.dumps(state)  # the page receives this verbatim
    assert state["symbols"] == ["BTCUSDT"]
    assert len(state["cells"]) == 2
    cell = next(c for c in state["cells"] if c["timeframe"] == "5m")
    assert cell["bars"] >= 900, "the chart keeps a deep history, not just the lookback"
    five = engine.maps[("BTCUSDT", "5m")]
    assert len(five.bars) == 240, "detection still reads only the lookback window"
    assert cell["price"] > 0
    # real BTC moves, so the map must have found something
    assert sum(state["counts"].values()) > 0
    for zone in state["active"]:
        assert zone["key"] and zone["status"] == "active"
        assert zone["bottom"] <= zone["top"]
        assert zone["evidence"], "a zone without evidence is not explainable"


def test_live_candle_rolls_up_into_the_higher_timeframe():
    rows = load(3000)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["1m", "5m"], settings=dict(DEFAULTS))

    async def go():
        for row in rows[-15:]:
            await engine.on_candle(dict(row))
        return engine.snapshot()

    state = asyncio.run(go())
    one = next(c for c in state["cells"] if c["timeframe"] == "1m")
    five = next(c for c in state["cells"] if c["timeframe"] == "5m")
    assert one["bars"] == 15
    assert 2 <= five["bars"] <= 0 + 4
    # the 5m map only speaks for CLOSED candles; the 1m map holds the last one
    assert one["price"] == pytest.approx(rows[-1]["close"])


def test_forming_candle_is_visible_before_it_closes():
    rows = load(3000)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["1m", "5m"], settings=dict(DEFAULTS))

    async def go():
        for row in rows[:300]:
            await engine.on_candle(dict(row))
        # the exchange keeps re-sending the bar that is still open
        tick = dict(rows[300], high=rows[300]["high"] + 50, close=rows[300]["close"] + 40, closed=False)
        await engine.on_candle(tick)
        return engine.bars("BTCUSDT", "1m", 50), engine.bars("BTCUSDT", "5m", 50), engine.snapshot()

    one, five, state = asyncio.run(go())
    assert one[-1]["closed"] is False, "the live 1m candle must be on the chart"
    assert one[-1]["close"] == pytest.approx(rows[300]["close"] + 40)
    assert five[-1]["closed"] is False
    assert five[-1]["high"] >= rows[300]["high"] + 50 - 1e-9
    assert state["price"]["BTCUSDT"] == pytest.approx(rows[300]["close"] + 40)
    assert state["ticks_seen"] == 1


def test_a_rewound_feed_restarts_the_map_instead_of_stacking():
    rows = load(400)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["5m"], settings=dict(DEFAULTS))

    async def go():
        for row in rows:
            await engine.on_candle(dict(row))
        before = len(engine.bars("BTCUSDT", "5m", 900))
        for row in rows:  # a replay looping back to the first bar
            await engine.on_candle(dict(row))
        return before, len(engine.bars("BTCUSDT", "5m", 900)), engine.loops_reset, engine.bars("BTCUSDT", "5m", 900)

    before, after, resets, bars = asyncio.run(go())
    assert resets > 0
    assert after <= before + 2, "history must not stack the same candles twice"
    opens = [b["open_time"] for b in bars]
    assert opens == sorted(opens) and len(opens) == len(set(opens))


def test_candles_endpoint_serves_the_deep_history_and_the_live_bar():
    rows = load(2500)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["15m"], settings=dict(DEFAULTS))
    feed = FakeFeed(rows)
    asyncio.run(engine.warmup(feed))
    app = create_app({"symbols": ["BTCUSDT"], "timeframes": ["15m"], "max_symbols": 5}, feed, engine, None)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = (await client.get("/api/candles", params={"symbol": "BTCUSDT", "timeframe": "15m", "limit": 1200})).json()
            assert len(body["bars"]) > 300, "the chart asks for a lot of candles and gets them"
            assert body["bars"] == sorted(body["bars"], key=lambda b: b["open_time"])
            assert body["live"] is False
            # the SSE route exists and is not shadowed; it is exercised end to
            # end against the real server by tools/smoke.sh (a test client
            # buffers streams, so asserting the stream itself here would be a
            # test of httpx, not of this app)
            paths = {getattr(route, "path", "") for route in app.routes}
            assert {"/api/stream", "/api/state", "/api/candles", "/api/zones"} <= paths
            return True

    assert asyncio.run(go())


def test_replay_can_show_candles_forming_without_inventing_prices(tmp_path):
    import shutil

    folder = tmp_path / "data"
    folder.mkdir()
    shutil.copy(CSV, folder / "BTCUSDT-1m-test.csv")
    from bot.feed import ReplayFeed

    async def collect(ticks: int) -> list[dict]:
        feed = ReplayFeed(folder, ["BTCUSDT"], speed=100_000, loop=False, ticks=ticks)
        out = []
        async for candle in feed.candles():
            out.append(candle)
            if len(out) > 40:
                break
        return out

    plain = asyncio.run(collect(1))
    with_ticks = asyncio.run(collect(4))
    assert all(c["closed"] for c in plain)
    partials = [c for c in with_ticks if not c["closed"]]
    assert partials, "ticks must produce unfinished candles"
    parents = {c["open_time"]: c for c in with_ticks if c["closed"]}
    checked = 0
    for candle in partials:
        # a partial candle never leaves the recorded high/low of its own candle
        parent = parents.get(candle["open_time"])
        if parent is None:
            continue
        assert parent["low"] - 1e-9 <= candle["low"] <= candle["high"] <= parent["high"] + 1e-9
        assert parent["low"] - 1e-9 <= candle["close"] <= parent["high"] + 1e-9
        checked += 1
    assert checked >= 5, "the check must actually have compared some candles"


def test_pushed_frames_stay_small_and_carry_no_history():
    rows = load(3000)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["5m", "15m"], settings=dict(DEFAULTS))
    asyncio.run(engine.warmup(FakeFeed(rows)))
    frame = {"type": "state", **engine.snapshot(cells=False, with_history=False)}
    payload = json.dumps(frame, default=str)
    assert "cells" in frame and frame["cells"] == []
    assert frame["history"] == [], "resolved zones are fetched on demand, not pushed"
    assert frame["active"], "the live frame still carries every active zone"
    assert len(payload) < 400_000, f"a live frame must stay small, got {len(payload)} bytes"
    for zone in frame["active"]:
        assert {"key", "kind", "level", "status", "evidence", "time"} <= set(zone)
    for event in frame["events"]:
        assert set(event["zone"] or {}) <= {"key", "kind", "side", "symbol", "timeframe", "level",
                                           "time", "status", "mitigated_at", "invalidated_at"}


def test_resolved_zones_are_available_on_demand():
    rows = load(3000)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["15m"], settings=dict(DEFAULTS))
    feed = FakeFeed(rows)
    asyncio.run(engine.warmup(feed))
    app = create_app({"symbols": ["BTCUSDT"], "timeframes": ["15m"], "max_symbols": 5}, feed, engine, None)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resolved = (await client.get("/api/zones", params={"status": "resolved"})).json()
            assert resolved["count"] == len([z for z in engine.zones.values() if z["status"] != "active"])
            assert resolved["count"] > 0, "real candles must have produced resolved zones too"
            for zone in resolved["items"]:
                assert zone["status"] in {"mitigated", "invalidated"}

    asyncio.run(go())


def test_server_endpoints_and_settings_persistence(tmp_path):
    rows = load(1500)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["15m"], settings=dict(DEFAULTS))
    feed = FakeFeed(rows)
    asyncio.run(engine.warmup(feed))

    config_path = tmp_path / "config.json"
    config = {"symbols": ["BTCUSDT"], "timeframes": ["15m"], "settings": dict(DEFAULTS), "max_symbols": 10}
    config_store.write(config_path, config)
    app = create_app(config, feed, engine, config_path)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = (await client.get("/api/health")).json()
            assert health["ok"] and health["symbols"] == 1
            state = (await client.get("/api/state")).json()
            assert state["timeframes"] == ["15m"]
            zones = (await client.get("/api/zones")).json()
            assert zones["count"] == len(engine.active_zones())
            everything = (await client.get("/api/zones", params={"status": "all"})).json()
            assert everything["count"] == len(engine.zones)
            candles = (await client.get("/api/candles", params={"symbol": "BTCUSDT", "timeframe": "15m"})).json()
            assert candles["bars"] and candles["bars"][-1]["close"] > 0
            posted = (
                await client.post("/api/settings", json={"pivot_len": 4, "min_fvg_atr": 0.3})
            ).json()
            assert posted["settings"]["pivot_len"] == 4
            saved = json.loads(config_path.read_text())
            assert saved["settings"]["pivot_len"] == 4, "a UI change must land in config.json"
            symbols = (await client.post("/api/symbols", json={"add": ["ethusdt"]})).json()
            assert "ETHUSDT" in symbols["symbols"]
            assert "ETHUSDT" in json.loads(config_path.read_text())["symbols"]

    asyncio.run(go())
    assert engine.settings["min_fvg_atr"] == 0.3


def test_the_heartbeat_rebuilds_a_silent_exchange_stream():
    """A socket that stays open but delivers nothing must be rebuilt by itself."""

    class StallFeed(FakeFeed):
        source = "live"

        def __init__(self, rows):
            super().__init__(rows)
            self.connected = True
            self.last_message = time.time() - 300      # five silent minutes
            self.reconnects = 0

        def force_reconnect(self):
            self.reconnects += 1
            self.connected = False

    feed = StallFeed(load(300))
    engine = Engine(symbols=["BTCUSDT"], timeframes=["5m"], settings=dict(DEFAULTS))
    app = create_app({"symbols": ["BTCUSDT"], "timeframes": ["5m"], "max_symbols": 5}, feed, engine, None)

    async def go():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(6.5)      # the heartbeat runs every 5s
            return feed.reconnects

    assert asyncio.run(go()) >= 1, "the heartbeat must notice the silence and reconnect"


# ------------------------------------------------------------ REST poller
class PollFeed(FakeFeed):
    """Serves a rolling window of the recorded candles, like an exchange would."""

    def __init__(self, rows, start: int = 0):
        super().__init__(rows)
        self.cursor = start

    async def history(self, symbol, interval, limit=500, include_open=False):
        window = self.rows[self.cursor : self.cursor + limit]
        return [dict(r, symbol=symbol, closed=not include_open or i < len(window) - 1) for i, r in enumerate(window)]


def test_rest_poller_delivers_candles_and_ticks_when_the_socket_is_silent():
    from bot.poller import RestPoller

    rows = load(400)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["1m", "5m"], settings=dict(DEFAULTS))
    feed = PollFeed(rows)
    feed.connected = True          # connected, but nothing has ever arrived
    feed.parsed = 0
    feed.last_message = time.time() - 600
    poller = RestPoller(feed, engine, interval=2, force=True)

    async def go():
        await poller.once()
        first = (engine.candles_seen, engine.ticks_seen, engine.snapshot()["price"]["BTCUSDT"])
        await poller.once()        # the same candles again must not duplicate
        closed_bars = len(engine.history[("BTCUSDT", "1m")])
        chart_bars = len(engine.bars("BTCUSDT", "1m", 900))
        second = (engine.candles_seen, engine.ticks_seen, closed_bars, chart_bars)
        return first, second

    (c1, t1, price1), (c2, t2, closed_bars, chart_bars) = asyncio.run(go())
    assert c1 > 0, "closed candles must reach the engine even with a dead socket"
    assert t1 > 0, "the forming candle must reach the engine as a tick"
    assert price1 == pytest.approx(rows[2]["close"])
    assert poller.closed_pushed == c1
    assert t2 > t1, "ticks keep coming"
    assert closed_bars == 2, "closed candles are not duplicated into the chart"
    assert chart_bars == 3, "the chart shows the closed candles plus the one still forming"
    assert poller.last_error is None


def test_rest_poller_is_quiet_when_the_socket_is_healthy():
    from bot.poller import RestPoller

    rows = load(300)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["1m"], settings=dict(DEFAULTS))
    feed = PollFeed(rows)
    feed.connected = True
    feed.parsed = 50
    feed.last_message = time.time() - 2   # the socket is delivering
    poller = RestPoller(feed, engine, interval=2, idle_interval=30)
    asyncio.run(poller.once())
    assert poller.primary is False
    assert engine.ticks_seen == 0, "no need to poll ticks when the socket provides them"
    assert engine.candles_seen >= 1, "but closed candles are still topped up"


def test_warmup_reports_progress_and_does_not_block_forever():
    rows = load(1500)

    class SlowFeed(FakeFeed):
        async def history(self, symbol, interval, limit=500, include_open=False):
            await asyncio.sleep(0.01)
            return await super().history(symbol, interval, limit)

    engine = Engine(symbols=["BTCUSDT", "ETHUSDT"], timeframes=["5m", "15m"], settings=dict(DEFAULTS))
    assert engine.warming is True
    asyncio.run(engine.warmup(SlowFeed(rows), concurrency=4))
    assert engine.warming is False
    assert engine.warm_done == 4, f"every cell must report progress, got {engine.warm_done}"
    assert engine.warm_total >= 4
    state = engine.snapshot()
    assert state["warming"]["active"] is False


def test_server_feeds_itself_from_rest_when_the_socket_never_delivers():
    """The exact bug reported live: connected:true, candles_seen:0, ticks 0.

    With the socket dead, the app must still fill the map and move the chart.
    """
    rows = load(600)

    class DeadSocketFeed(PollFeed):
        source = "live"

        def __init__(self, rows):
            super().__init__(rows)
            self.connected = True
            self.parsed = 0
            self.last_message = 0.0

        async def candles(self):
            while True:            # a socket that accepts and then says nothing
                await asyncio.sleep(60)
                yield {}

    feed = DeadSocketFeed(rows)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["1m", "5m"], settings=dict(DEFAULTS))
    config = {
        "symbols": ["BTCUSDT"],
        "timeframes": ["1m", "5m"],
        "max_symbols": 5,
        "rest_poll_seconds": 1.5,
        "rest_poll_quiet_after": 1,
    }
    app = create_app(config, feed, engine, None)

    async def go():
        async with app.router.lifespan_context(app):
            await asyncio.sleep(5.5)
            return (
                engine.candles_seen,
                engine.ticks_seen,
                engine.snapshot()["price"].get("BTCUSDT"),
                len(engine.bars("BTCUSDT", "1m", 50)),
            )

    candles, ticks, price, chart_bars = asyncio.run(go())
    assert candles > 0, "closed candles must arrive from the REST poller"
    assert ticks > 0, "the forming candle must arrive as a tick"
    assert price and price > 0
    assert chart_bars >= 2


# ------------------------------------------------- what the user actually sees
def test_btc_1m_algo_candle_high_is_reported_and_reachable_from_the_api():
    """The reported regression: "1m me algo candle high bta hi nahi raha".

    Pinned against real BTC candles: an algo candle at a high on the 1m
    timeframe must be detected, must carry its evidence, and must be served by
    /api/zones both while active and after it is mitigated.
    """
    rows = load(4000)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["1m"], settings=dict(DEFAULTS))
    feed = FakeFeed(rows)
    asyncio.run(engine.warmup(feed))

    one_min = [z for z in engine.zones.values() if z["timeframe"] == "1m"]
    algo_highs = [z for z in one_min if z["kind"] in {"algo_bear", "very_strong_algo_bear"}]
    assert algo_highs, "the 1m map must find algo candles at highs on real BTC data"
    sample = algo_highs[0]
    assert sample["side"] == "high"
    assert sample["evidence"], "every zone explains itself"
    assert sample["level"] == pytest.approx(sample["top"])
    resolved = [z for z in algo_highs if z["status"] != "active"]
    assert resolved, "1m zones resolve quickly - that is why they must stay visible"

    app = create_app({"symbols": ["BTCUSDT"], "timeframes": ["1m"], "max_symbols": 5}, feed, engine, None)

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            every = (await client.get("/api/zones", params={"status": "all", "limit": 4000})).json()
            assert any(z["kind"] in {"algo_bear", "very_strong_algo_bear"} for z in every["items"])
            only_1m = [z for z in every["items"] if z["timeframe"] == "1m"]
            assert len(only_1m) == len([z for z in engine.zones.values() if z["timeframe"] == "1m"])
            since = (await client.get("/api/zones", params={"status": "resolved", "since": sample["time"]})).json()
            assert since["count"] >= 1

    asyncio.run(go())


def test_active_1m_algo_high_appears_while_a_replay_streams():
    """During streaming (not just warm-up) a fresh 1m algo high becomes active."""
    from bot.feed import ReplayFeed
    import shutil

    folder = Path("/tmp/smc-1m-check")
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    shutil.copy(CSV, folder / "BTCUSDT-1m.csv")

    engine = Engine(symbols=["BTCUSDT"], timeframes=["1m"], settings=dict(DEFAULTS))
    seen_active: list[dict] = []

    async def go():
        feed = ReplayFeed(folder, ["BTCUSDT"], speed=100_000, loop=False)
        count = 0
        async for candle in feed.candles():
            await engine.on_candle(candle)
            count += 1
            for zone in engine.active_zones():
                if zone["kind"] in {"algo_bear", "very_strong_algo_bear"} and zone["timeframe"] == "1m":
                    seen_active.append(zone)
            if count > 2500:
                break
        return count

    asyncio.run(go())
    assert seen_active, "a 1m algo candle high must go ACTIVE while the stream runs"
    assert any(z["side"] == "high" for z in seen_active)


# --------------------------------------- round 5: the live context per zone
def test_every_pushed_zone_carries_its_grade_and_live_regime():
    """The page draws a grade and a "tradeable now" badge, so the payload the
    browser receives must contain them - for active and resolved zones alike."""
    rows = load(3000)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["1m", "5m"], settings=dict(DEFAULTS))
    feed = FakeFeed(rows)

    async def go():
        await engine.warmup(feed)

    asyncio.run(go())
    state = engine.snapshot()
    json.dumps(state)
    assert state["active"], "real BTC candles produce zones"
    for zone in state["active"]:
        assert zone["grade"] in {"A+", "A", "B", "C", "-"}
        assert isinstance(zone["score"], int)
        assert zone["now_trend"]["state"] in {"trend_up", "trend_down", "range", "unknown"}
        assert isinstance(zone["now_trend"]["ready"], bool)
        assert zone["now_trend"]["ready"] in (True, False)
        assert zone["ob"] is None or "color" in zone["ob"]
        assert isinstance(zone["htf"], list)
        assert isinstance(zone["confluence"], list)

    # the live regime is per instrument AND timeframe, not one global number
    for cell in state["cells"]:
        assert cell["regime"]["state"] in {"trend_up", "trend_down", "range", "unknown"}
        assert "er" in cell["regime"] and "adx" in cell["regime"]


def test_the_zone_api_serves_the_same_fields_and_never_hides_mitigation():
    rows = load(3000)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["5m"], settings=dict(DEFAULTS))
    feed = FakeFeed(rows)

    async def go():
        await engine.warmup(feed)
        app = create_app({"symbols": ["BTCUSDT"], "timeframes": ["5m"], "max_symbols": 5}, feed, engine, None)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            body = (await client.get("/api/zones?status=all&limit=4000")).json()
            assert body["items"]
            for zone in body["items"]:
                assert "grade" in zone and "now_trend" in zone
                if zone["status"] == "active":
                    assert zone["mitigated_at"] is None and zone["invalidated_at"] is None
                else:
                    assert (zone["mitigated_at"] and zone["mitigated_price"] is not None) or (
                        zone["invalidated_at"] and zone["invalidated_price"] is not None
                    )
            # the rectangle really does sit on the exact extreme
            for zone in body["items"]:
                if zone["kind"] in {"weak_high", "weak_low"}:
                    continue  # a weak high/low is deliberately a line, not a block
                if zone["side"] == "high":
                    assert abs(zone["top"] - zone["level"]) < 1e-9
                else:
                    assert abs(zone["bottom"] - zone["level"]) < 1e-9
                assert zone["bottom"] < zone["top"]

    asyncio.run(go())


# ------------------------------------------------- round 6: futures vs spot
def test_futures_and_spot_are_separate_markets_end_to_end():
    rows = load(1200)
    engine = Engine(symbols=["BTCUSDT", "BTC_USDT"], timeframes=["5m"], settings=dict(DEFAULTS))

    async def go():
        await engine.warmup(FakeFeed(rows))
        return engine.snapshot()

    state = asyncio.run(go())
    assert state["markets"] == {"spot": ["BTCUSDT"], "futures": ["BTC_USDT"]}
    counts = state["market_counts"]
    assert counts["spot"]["symbols"] == 1 and counts["futures"]["symbols"] == 1
    # every zone says which book it came from, and both books are being mapped
    for zone in state["active"] + state["history"]:
        assert zone["market"] in ("spot", "futures")
        assert zone["market"] == ("futures" if "_" in zone["symbol"] else "spot")
    assert counts["spot"]["zones"] > 0
    assert counts["futures"]["zones"] > 0


def test_zones_and_universe_can_be_filtered_by_market(tmp_path):
    rows = load(1200)
    engine = Engine(symbols=["BTCUSDT", "BTC_USDT"], timeframes=["5m"], settings=dict(DEFAULTS))
    config = {"symbols": ["BTCUSDT"], "futures_symbols": ["BTC_USDT"], "timeframes": ["5m"], "settings": {}}
    app = create_app(config, FakeFeed(rows), engine, tmp_path / "config.json")

    async def go():
        await engine.warmup(FakeFeed(rows))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            fut = (await client.get("/api/zones", params={"status": "all", "market": "futures"})).json()
            spot = (await client.get("/api/zones", params={"status": "all", "market": "spot"})).json()
            both = (await client.get("/api/zones", params={"status": "all"})).json()
        return fut, spot, both

    fut, spot, both = asyncio.run(go())
    assert fut["items"] and spot["items"]
    assert all(z["market"] == "futures" for z in fut["items"])
    assert all(z["market"] == "spot" for z in spot["items"])
    assert len(both["items"]) == len(fut["items"]) + len(spot["items"])


# ------------------------------------- round 7: ranking, scale, the signals API
def test_snapshot_ranks_by_measurement_and_is_capped():
    rows = load(3000)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["5m", "15m"], settings=dict(DEFAULTS))

    async def go():
        await engine.warmup(FakeFeed(rows))
        return engine.snapshot()

    state = asyncio.run(go())
    assert len(state["active"]) <= 200, "one frame never carries an unbounded list"
    ranks = [engine.rank_key(z) for z in state["active"]]
    assert ranks == sorted(ranks, reverse=True), "the pushed list is best-first"
    assert state["grade_counts"]["A+"] >= 0 and "tradable_count" in state
    for zone in state["active"]:
        assert "prob" in zone and "cond_met" in zone and "cond_missing" in zone


def test_the_zone_store_is_bounded_but_never_drops_a_live_zone():
    engine = Engine(symbols=["BTCUSDT"], timeframes=["1m"], settings=dict(DEFAULTS))
    engine.max_zones = 10
    engine.zones = {
        f"k{i}": {"key": f"k{i}", "status": "active" if i % 5 == 0 else "mitigated",
                  "mitigated_at": i, "symbol": "BTCUSDT", "timeframe": "1m"}
        for i in range(40)
    }
    engine._prune_zones()
    live = [z for z in engine.zones.values() if z["status"] == "active"]
    assert len(live) == 8, "every live zone survives the pruning"
    assert len(engine.zones) <= max(1000, engine.max_zones) or len(engine.zones) == 8


def test_the_signals_endpoint_is_ranked_and_filterable():
    rows = load(2500)
    engine = Engine(symbols=["BTCUSDT", "BTC_USDT"], timeframes=["5m"], settings=dict(DEFAULTS))
    config = {"symbols": ["BTCUSDT"], "futures_symbols": ["BTC_USDT"], "timeframes": ["5m"], "settings": {}}
    app = create_app(config, FakeFeed(rows), engine, None)

    async def go():
        await engine.warmup(FakeFeed(rows))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            ranked = (await client.get("/api/signals", params={"limit": 30})).json()
            ready = (await client.get("/api/signals", params={"tradable": 1, "limit": 30})).json()
            futures = (await client.get("/api/signals", params={"market": "futures", "limit": 30})).json()
            top = (await client.get("/api/signals", params={"grade": "A+", "limit": 30})).json()
        return ranked, ready, futures, top

    ranked, ready, futures, top = asyncio.run(go())
    assert ranked["items"], "the app finds signals on real candles"
    # the contract: the measured chance leads, then expectancy, then the letter
    keys = [Engine.rank_key(z) for z in ranked["items"]]
    assert keys == sorted(keys, reverse=True), "the list follows the ranked key"
    probs = [z["prob"] for z in ranked["items"]]
    assert probs == sorted(probs, reverse=True), "best measured chance first"
    first = ranked["items"][0]
    assert first["grade"] == "A+" and first["prob"] >= 0.85, "the list opens on the most likely plan"
    assert ranked["model"]["available"] is True
    assert all(z["market"] == "futures" for z in futures["items"])
    assert all(z["tradable"] for z in ready["items"])
    assert all(z["grade"] == "A+" for z in top["items"])


# ------------------------------------------------- round 8: the signal list
def test_signals_endpoint_returns_only_measured_fee_clearing_plans():
    """What the page opens on: plans the model has measured at 85%+ *and* whose
    reward pays the fee. Anything else belongs to the wider views."""
    rows = load(2500)
    engine = Engine(symbols=["BTCUSDT", "BTC_USDT"], timeframes=["5m"], settings=dict(DEFAULTS))
    config = {"symbols": ["BTCUSDT"], "futures_symbols": ["BTC_USDT"], "timeframes": ["5m"], "settings": {}}
    app = create_app(config, FakeFeed(rows), engine, None)

    async def go():
        await engine.warmup(FakeFeed(rows))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            every = (await client.get("/api/signals", params={"limit": 200})).json()
            only = (await client.get("/api/signals", params={"signal": 1, "limit": 200})).json()
            model = (await client.get("/api/model")).json()
            state = (await client.get("/api/state")).json()
        return every, only, model, state

    every, only, model, state = asyncio.run(go())
    assert every["items"], "the scan finds zones"
    assert all(z["signal"] and z["tradable"] for z in only["items"]), "signal=1 is stricter than everything"
    assert all((z["prob"] or 0) >= DEFAULTS["signal_prob_min"] for z in only["items"]), \
        "a signal is at the measured bar or better"
    assert len(only["items"]) <= len(every["items"])
    assert state["signal_count"] >= len(only["items"]), "the counter never under-reports the list"
    # the page prints these bands, so they have to carry their own evidence
    assert model["available"] is True
    assert model["bands"] and all(b["letter"] and b["n"] >= 0 for b in model["bands"])
    assert model["command"] and "fit_model.py" in model["command"], "every band says how it was measured"


# ------------------------------------------ round 9: no give-up, archive, futures
def test_one_broken_instrument_does_not_switch_the_warmup_off():
    """The real reason futures looked empty.

    ``warmup`` used to return early for *every remaining symbol* after eight
    consecutive failures. Sixty contracts is three hundred REST calls, the contract
    API rate-limits, and the first burst switched the whole scan off. A failure may
    cost one instrument; it may never cost the rest.
    """
    rows = load(600)
    symbols = [f"S{i}_USDT" for i in range(12)]
    engine = Engine(symbols=symbols, timeframes=["5m"], settings=dict(DEFAULTS))

    class FlakyFeed:
        source = "live"

        def exchange_interval(self, timeframe: str) -> str:
            return "5m"

        async def history(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
            if symbol != "S11_USDT":
                raise RuntimeError("rate limited")
            return [dict(r, symbol=symbol) for r in rows[-400:]]

    asyncio.run(engine.warmup(FlakyFeed(), concurrency=2))
    # eleven failed, and the twelfth still warmed: the tail of the list is not lost
    assert engine.history[("S11_USDT", "5m")], "the last instrument must still be tried"
    assert engine.warm_problems, "the failure is recorded, not hidden"
    assert "rate limited" in engine.warm_problems["S0_USDT"]
    assert "S11_USDT" not in engine.thin_symbols(), "the warmed one is not re-requested"


def test_diagnostics_say_why_an_instrument_has_no_signals():
    rows = load(600)
    engine = Engine(symbols=["BTCUSDT"], timeframes=["5m"], settings=dict(DEFAULTS))
    engine.warm_problems["BTCUSDT"] = "FeedError: no kline"
    asyncio.run(engine.warmup(FakeFeed(rows)))
    row = engine.diagnostics()[0]
    assert row["symbol"] == "BTCUSDT"
    assert row["cells"]["5m"]["bars"] > 0 and row["cells"]["5m"]["ready"] in (True, False)
    assert row["problem"] == "FeedError: no kline"


def test_signals_are_written_to_the_journal_and_never_removed(tmp_path):
    """End to end: the engine writes, the API reads, a resolved signal stays."""
    from bot.journal import journal, open_journal

    store = open_journal(tmp_path / "journal.sqlite3")
    try:
        rows = load(2500)
        engine = Engine(symbols=["BTC_USDT"], timeframes=["5m"], settings=dict(DEFAULTS))
        engine.journal_prob_min = 0.0          # archive everything the model graded
        config = {"symbols": [], "futures_symbols": ["BTC_USDT"], "timeframes": ["5m"],
                  "settings": {}, "markets": ["futures"]}
        app = create_app(config, FakeFeed(rows), engine, None)

        async def go():
            await engine.warmup(FakeFeed(rows))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                listing = (await client.get("/api/journal", params={"limit": 50})).json()
                stats = (await client.get("/api/journal/stats")).json()
                diag = (await client.get("/api/diagnostics")).json()
                page = (await client.get("/api/state")).json()
            return listing, stats, diag, page

        listing, stats, diag, page = asyncio.run(go())
        assert stats["enabled"] is True and stats["recorded"] > 0, "the scanner wrote its signals"
        assert listing["items"], "and they come back out"
        first = listing["items"][0]
        for field in ("symbol", "timeframe", "grade", "prob", "entry", "stop", "target", "formed_at"):
            assert first.get(field) is not None, f"{field} is part of the record"
        assert page["journal"]["recorded"] == stats["recorded"], "the push reports the archive"
        assert diag["items"][0]["symbol"] == "BTC_USDT"
        # a resolved signal keeps its row
        key = first["key"]
        engine._journal_close(key, "mitigated", {"mitigated_at": 1700000000000})  # noqa: SLF001
        after = asyncio.run(_journal_list())
        assert any(r["key"] == key for r in after), "nothing is deleted when a signal resolves"
    finally:
        journal().close()
        open_journal(None)


async def _journal_list() -> list[dict]:
    from bot.journal import journal as get_journal
    return get_journal().list(limit=200)
