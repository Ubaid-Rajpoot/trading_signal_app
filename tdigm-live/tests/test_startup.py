"""The page has to come up at once, and a replay has to be warm before it plays.

Both were real failures: warm-up ran before uvicorn accepted connections (so a
120-instrument universe looked like a dead server), and a replay only knew the
history it had *played*, so the 1h row needed two hours of wall clock before it
could signal.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config_store  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.feed import ReplayFeed  # noqa: E402
from bot.server import create_app  # noqa: E402

CONFIG = {
    "symbols": ["BTCUSDT"],
    "exchange": "binance",
    "max_symbols": 3,
    "timeframes": ["1m", "1h"],
    "tdi": {"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9, "band_mult": 1.6185},
    "structure": {"pivot_len": 5, "sweep_max_wait": 60, "target_mode": "range"},
    "filters": {"structure": True, "hvb": True},
    "server": {"host": "127.0.0.1", "port": 8798},
}


def _csv(tmp_path: Path, minutes: int) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    rows = []
    start = 1_735_689_600_000
    for i in range(minutes):
        price = 100 + i * 0.01
        rows.append(f"{start + i * 60_000},{price},{price + 0.5},{price - 0.5},{price},1")
    (data / "BTCUSDT-1h-2025-01-01.csv").write_text("\n".join(rows) + "\n")
    return data


def test_replay_serves_history_from_the_prefix_it_skipped(tmp_path):
    data = _csv(tmp_path, 9_000)
    feed = ReplayFeed(data, ["BTCUSDT"], speed=1000, loop=False, skip_bars=8_000)

    # before a single bar is paced out, the play head already has history
    hourly = asyncio.run(feed.history("BTCUSDT", "1h", 600))
    assert len(hourly) >= 120, "a 1h cell must be warmable at t=0"
    assert hourly[-1]["open_time"] <= 1_735_689_600_000 + 8_000 * 60_000

    async def first_bar():
        stream = feed.candles()
        bar = await stream.__anext__()
        await stream.aclose()
        return bar

    bar = asyncio.run(first_bar())
    # the stream continues *after* the prefix - no look-ahead, no repeats
    assert bar["open_time"] == 1_735_689_600_000 + 8_000 * 60_000
    assert bar["open_time"] > hourly[-1]["open_time"]


def test_replay_history_never_reaches_past_the_play_head(tmp_path):
    data = _csv(tmp_path, 500)
    feed = ReplayFeed(data, ["BTCUSDT"], speed=1000, loop=False, skip_bars=200)
    bars = asyncio.run(feed.history("BTCUSDT", "1m", 600))
    assert bars and len(bars) == 200
    assert bars[-1]["open_time"] == 1_735_689_600_000 + 199 * 60_000


def test_the_page_and_the_state_answer_before_warmup_finishes(tmp_path, monkeypatch):
    data = _csv(tmp_path, 60)
    config_path = tmp_path / "config.json"
    config_store.write(config_path, json.loads(json.dumps(CONFIG)))
    feed = ReplayFeed(data, ["BTCUSDT"], speed=1000, loop=False)
    engine = Engine(
        symbols=["BTCUSDT"],
        timeframes=CONFIG["timeframes"],
        tdi_settings=CONFIG["tdi"],
        filters=CONFIG["filters"],
        structure=CONFIG["structure"],
    )

    release = asyncio.Event()

    async def slow_warmup(_feed, concurrency: int = 6):
        await release.wait()  # stands in for 600 REST calls

    monkeypatch.setattr(engine, "warmup", slow_warmup)
    app = create_app(CONFIG, feed, engine, config_path)

    started = time.monotonic()
    with TestClient(app) as client:
        assert time.monotonic() - started < 5, "startup must not wait for warm-up"
        assert client.get("/").status_code == 200
        body = client.get("/api/state").json()
        assert body["cells"], "the table is there while it warms"
        assert all(cell["warm"] is False for cell in body["cells"])
        assert client.get("/api/health").json()["warming"] == len(body["cells"])
    release.set()


def test_warmup_is_parallel_and_survives_one_bad_symbol(tmp_path):
    data = _csv(tmp_path, 60)
    feed = ReplayFeed(data, ["BTCUSDT"], speed=1000, loop=False)
    engine = Engine(
        symbols=["AAAUSDT", "BTCUSDT", "CCCUSDT"],
        timeframes=["1m"],
        tdi_settings=CONFIG["tdi"],
        filters=CONFIG["filters"],
        structure=CONFIG["structure"],
    )
    seen: list[str] = []

    async def fake_warm(_feed, symbol):
        seen.append(symbol)
        await asyncio.sleep(0)
        if symbol == "AAAUSDT":
            raise RuntimeError("exchange said no")

    engine.warm_symbol = fake_warm  # type: ignore[assignment]
    asyncio.run(engine.warmup(feed))
    assert sorted(seen) == ["AAAUSDT", "BTCUSDT", "CCCUSDT"], "one failure must not stop the rest"


def test_state_says_when_the_exchange_is_unreachable(tmp_path):
    data = _csv(tmp_path, 30)
    config_path = tmp_path / "config.json"
    config_store.write(config_path, json.loads(json.dumps(CONFIG)))
    feed = ReplayFeed(data, ["BTCUSDT"], speed=1000, loop=False)
    feed.source = "live"
    feed.connected = False
    feed.status = "reconnecting (3) ConnectionResetError"
    engine = Engine(
        symbols=["BTCUSDT"],
        timeframes=["1m"],
        tdi_settings=CONFIG["tdi"],
        filters=CONFIG["filters"],
        structure=CONFIG["structure"],
    )
    app = create_app(CONFIG, feed, engine, config_path)
    with TestClient(app) as client:
        body = client.get("/api/state").json()
        assert body["feed"]["connected"] is False
        assert "ConnectionResetError" in body["feed"]["status"]
        assert client.get("/api/state").headers["cache-control"] == "no-store, max-age=0"
