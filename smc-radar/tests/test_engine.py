"""The engine, the roll-up and the HTTP/WS surface - end to end on real candles.

The replay file is real Binance 1-minute data (see ``docs/data.md``); if it is
not on this machine the test is skipped rather than faked.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
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
    assert cell["bars"] == 240, "the map keeps lookback bars, no more"
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
