"""The web controls have to actually change the running bot and the saved file."""

from __future__ import annotations

import json
import sys
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
    "timeframes": ["5m"],
    "tdi": {"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9, "band_mult": 1.6185},
    "structure": {"pivot_len": 5, "sweep_max_wait": 60, "target_mode": "range"},
    "filters": {"structure": True, "hvb": True},
    "server": {"host": "127.0.0.1", "port": 8799},
}


@pytest.fixture()
def client(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "BTCUSDT-1m-2025-01-01.csv").write_text(
        "1735689600000,100,101,99,100.5,12\n1735689660000,100.5,102,100,101,9\n"
    )
    config_path = tmp_path / "config.json"
    config = json.loads(json.dumps(CONFIG))
    config_store.write(config_path, config)
    feed = ReplayFeed(data, ["BTCUSDT"], speed=1000, loop=False)
    feed.source = "live"  # stands in for a live feed, so config writes are exercised
    engine = Engine(
        symbols=config["symbols"],
        timeframes=config["timeframes"],
        tdi_settings=config["tdi"],
        filters=config["filters"],
        structure=config["structure"],
    )
    app = create_app(config, feed, engine, config_path)
    with TestClient(app) as test_client:
        test_client.config_path = config_path  # type: ignore[attr-defined]
        test_client.feed = feed  # type: ignore[attr-defined]
        yield test_client


def test_health_reports_the_feed(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["source"] == "live"
    assert body["timeframes"] == ["5m"]


def test_the_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "TDIGM" in response.text
    assert "http" not in response.text.split("<script")[0].split("viewport")[0]  # no external assets


def test_adding_a_symbol_writes_the_config_file(client):
    response = client.post("/api/symbols", json={"add": ["ethusdt"]})
    body = response.json()
    assert body["added"] == ["ETHUSDT"]
    saved = json.loads(Path(client.config_path).read_text())  # type: ignore[attr-defined]
    assert saved["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert saved["timeframes"] == ["5m"], "the rest of the file must be untouched"


def test_the_symbol_limit_is_enforced_and_explained(client):
    client.post("/api/symbols", json={"add": ["ETHUSDT", "SOLUSDT"]})
    body = client.post("/api/symbols", json={"add": ["XRPUSDT", "ADAUSDT"]}).json()
    assert body["added"] == [] or len(body["symbols"]) <= 3
    assert len(body["symbols"]) <= 3
    assert any("limit" in warning for warning in body["warnings"])


def test_removing_a_symbol_writes_the_config_file(client):
    client.post("/api/symbols", json={"add": ["ETHUSDT"]})
    body = client.post("/api/symbols", json={"remove": ["BTCUSDT"]}).json()
    assert body["symbols"] == ["ETHUSDT"]
    saved = json.loads(Path(client.config_path).read_text())  # type: ignore[attr-defined]
    assert saved["symbols"] == ["ETHUSDT"]


def test_filters_are_validated_applied_and_saved(client):
    response = client.post("/api/filters", json={"hvb": False, "min_rr": 2, "sides": ["short"], "junk": 1})
    body = response.json()
    assert body["filters"]["hvb"] is False
    assert body["filters"]["min_rr"] == 2.0
    assert body["filters"]["sides"] == ["short"]
    saved = json.loads(Path(client.config_path).read_text())  # type: ignore[attr-defined]
    assert saved["filters"]["min_rr"] == 2.0
    assert "junk" not in saved["filters"]
    assert client.get("/api/state").json()["filters"]["config"]["hvb"] is False


def test_structure_settings_are_saved(client):
    body = client.post("/api/structure", json={"pivot_len": 3, "target_mode": "range"}).json()
    assert body["structure"]["pivot_len"] == 3
    saved = json.loads(Path(client.config_path).read_text())  # type: ignore[attr-defined]
    assert saved["structure"]["pivot_len"] == 3


def test_universe_says_why_it_is_empty_in_replay(client):
    body = client.get("/api/universe").json()
    assert body["items"] == []
    assert "replay" in (body["error"] or "").lower()


class FakeExchangeFeed(ReplayFeed):
    """A feed with an exchange universe but no network - for the 'add all' path."""

    name = "mexc"
    source = "live"

    def __init__(self):  # noqa: D401 - test double
        self._symbols = ["BTCUSDT"]
        self.queue = None
        self.connected = True
        self.status = "fake"
        self.frames = 0
        self._generation = 0
        self.chunk_size = 100

    async def history(self, symbol, interval, limit=500):
        return []

    async def candles(self):
        while True:
            import asyncio

            await asyncio.sleep(3600)
            yield {}

    async def universe(self):
        return [
            {"symbol": f"COIN{i}USDT", "base": f"COIN{i}", "quote": "USDT", "price": 1.0,
             "change": 1.0, "turnover": 1_000_000 - i}
            for i in range(50)
        ]


def test_add_all_from_the_universe_respects_the_limit_and_saves(tmp_path):
    config_path = tmp_path / "config.json"
    config = json.loads(json.dumps(CONFIG))
    config_store.write(config_path, config)
    feed = FakeExchangeFeed()
    engine = Engine(
        symbols=["BTCUSDT"],
        timeframes=["5m"],
        tdi_settings=config["tdi"],
        filters=config["filters"],
        structure=config["structure"],
    )
    app = create_app(config, feed, engine, config_path)
    with TestClient(app) as client:
        body = client.post("/api/universe/add", json={"preset": "all"}).json()
    assert body["ok"] is True
    assert len(body["added"]) == 2, "max_symbols is 3 and one is already watched"
    assert any("limit" in warning for warning in body["warnings"])
    saved = json.loads(config_path.read_text())
    assert len(saved["symbols"]) == 3
    assert "COIN0USDT" in saved["symbols"]


def test_replay_never_overwrites_the_saved_instrument_list(client):
    """A replay only has files for a few symbols - it must not shrink config.json."""
    client.feed.source = "replay"  # type: ignore[attr-defined]
    before = json.loads(Path(client.config_path).read_text())["symbols"]  # type: ignore[attr-defined]
    body = client.post("/api/symbols", json={"add": ["ETHUSDT"]}).json()
    assert "replay" in " ".join(body["warnings"]).lower()
    after = json.loads(Path(client.config_path).read_text())["symbols"]  # type: ignore[attr-defined]
    assert after == before


def test_centre_band_choice_is_applied_and_saved(client):
    body = client.post("/api/tdi", json={"hvb_reference": "fifty"}).json()
    assert body["hvb_reference"] == "fifty"
    assert "50" in body["label"]
    saved = json.loads(Path(client.config_path).read_text())  # type: ignore[attr-defined]
    assert saved["tdi"]["hvb_reference"] == "fifty"
    assert client.get("/api/state").json()["centre_reference"] == "fifty"
