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
CSV = ROOT / "data" / "btc" / "BTCUSDT-1m-2025-01.csv"
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
