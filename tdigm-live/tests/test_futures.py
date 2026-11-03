"""Futures (perpetual) data, and the two endpoints the new page reads.

The trader trades the USDT-M perpetual, so the feed has to be the contract book,
not spot: the basis and the wicks differ, and a swing high on spot is not the
level the contract swept. The exchange names contracts ``BTC_USDT``; the bot
speaks ``BTCUSDT``. All of that translation lives in one place and is pinned
here, with recorded responses instead of a network call (the exchange is not
reachable from a test runner, and a test that needs the internet is not a test).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config_store  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.feed import (  # noqa: E402
    BinanceFuturesFeed,
    MexcFeed,
    MexcFuturesFeed,
    contract_symbol,
    make_feed,
    plain_symbol,
)
from bot.server import create_app  # noqa: E402


def test_symbol_names_round_trip():
    assert contract_symbol("BTCUSDT") == "BTC_USDT"
    assert contract_symbol("BTC_USDC") == "BTC_USDC"
    assert plain_symbol("BTC_USDT") == "BTCUSDT"
    assert contract_symbol("1000PEPEUSDT") == "1000PEPE_USDT"


def test_the_default_market_is_perpetuals_and_spot_is_still_there():
    assert isinstance(make_feed("mexc", ["BTCUSDT"]), MexcFuturesFeed)
    assert isinstance(make_feed("mexc", ["BTCUSDT"], market="futures"), MexcFuturesFeed)
    assert isinstance(make_feed("mexc", ["BTCUSDT"], market="spot"), MexcFeed)
    assert isinstance(make_feed("binance", ["BTCUSDT"]), BinanceFuturesFeed)
    assert make_feed("mexc", ["BTCUSDT"]).market == "futures"


def test_mexc_history_interval_is_translated_exactly_once():
    feed = MexcFuturesFeed(["BTCUSDT"])
    assert feed.exchange_interval("1h") == "1h", "history() translates 1h to Min60"
    assert feed.exchange_interval("3m") is None, "unsupported native candles are aggregated"


@pytest.mark.asyncio
async def test_engine_can_warm_a_mexc_native_timeframe(monkeypatch):
    """Regression: exchange_interval once returned Min60 and history mapped it
    again, leaving every MEXC cell empty while websocket frames kept arriving."""
    feed = MexcFuturesFeed(["BTCUSDT"])
    now_s = 1_800_000_000

    async def fake_get(url, params=None):
        assert params["interval"] == "Min60"
        stamps = [now_s - (130 - i) * 3600 for i in range(130)]
        n = len(stamps)
        return {"success": True, "data": {
            "time": stamps, "open": [100] * n, "high": [101] * n,
            "low": [99] * n, "close": [100.5] * n, "vol": [10] * n,
        }}

    monkeypatch.setattr("bot.feed.time.time", lambda: now_s)
    monkeypatch.setattr(feed, "_get_json", fake_get)
    engine = Engine(
        symbols=["BTCUSDT"], timeframes=["1h"], history_bars=120,
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={"hvb": False, "regime": False, "trend_context": False, "require_bos": False},
        structure={"pivot_len": 3}, strategy="sweep",
    )
    await engine.warm_symbol(feed, "BTCUSDT")
    assert len(engine.cells[("BTCUSDT", "1h")].candles) == 120
    assert not engine.warm_failures
    await feed.close()


@pytest.mark.asyncio
async def test_mexc_futures_history_parses_the_contract_kline_payload(monkeypatch):
    """Their kline endpoint is column-oriented: parallel arrays, seconds, names
    only ``start``/``end``. Getting this wrong loads nothing and looks like a
    dead market."""
    feed = MexcFuturesFeed(["BTCUSDT"])
    seen: dict = {}

    async def fake_get(url, params=None):
        seen["url"] = url
        seen["params"] = params
        return {
            "success": True,
            "code": 0,
            "data": {
                "time": [1_700_000_000, 1_700_000_060],
                "open": [100.0, 101.0],
                "close": [101.0, 102.0],
                "high": [101.5, 102.5],
                "low": [99.5, 100.5],
                "vol": [12, 13],
                "amount": [1, 2],
            },
        }

    monkeypatch.setattr(feed, "_get_json", fake_get)
    candles = await feed.history("BTCUSDT", "1m", 10)

    assert "BTC_USDT" in seen["url"] and "/contract/kline/" in seen["url"]
    assert seen["params"]["interval"] == "Min1"
    assert len(candles) == 2
    first = candles[0]
    assert first["symbol"] == "BTCUSDT", "the bot speaks plain symbols everywhere above the feed"
    assert first["open_time"] == 1_700_000_000_000, "seconds -> milliseconds"
    assert (first["open"], first["high"], first["low"], first["close"]) == (100.0, 101.5, 99.5, 101.0)
    assert first["volume"] == 12.0
    assert first["close_time"] - first["open_time"] == 59_999
    assert first["closed"] is True


@pytest.mark.asyncio
async def test_mexc_futures_pages_deep_history_instead_of_silently_truncating(monkeypatch):
    feed = MexcFuturesFeed(["BTCUSDT"])
    calls = []

    async def fake_get(url, params=None):
        calls.append(dict(params or {}))
        step = 60
        stamps = list(range(int(params["start"]), int(params["end"]) + 1, step))[-2000:]
        n = len(stamps)
        return {"success": True, "data": {
            "time": stamps, "open": [100] * n, "high": [101] * n,
            "low": [99] * n, "close": [100.5] * n, "vol": [10] * n,
        }}

    monkeypatch.setattr(feed, "_get_json", fake_get)
    candles = await feed.history("BTCUSDT", "1m", 2_500)
    assert len(candles) == 2_500
    assert len(calls) >= 2, "MEXC returns at most 2,000 contract bars per request"
    assert [row["open_time"] for row in candles] == sorted(row["open_time"] for row in candles)


@pytest.mark.asyncio
async def test_mexc_futures_history_uses_the_real_timeframe_close(monkeypatch):
    feed = MexcFuturesFeed(["BTCUSDT"])

    async def fake_get(url, params=None):
        old = 1_700_000_000
        return {"success": True, "data": {
            "time": [old], "open": [100], "high": [101], "low": [99],
            "close": [100.5], "vol": [10],
        }}

    monkeypatch.setattr(feed, "_get_json", fake_get)
    candle = (await feed.history("BTCUSDT", "4h", 1))[0]
    assert candle["close_time"] - candle["open_time"] == 4 * 60 * 60 * 1000 - 1


@pytest.mark.asyncio
async def test_mexc_futures_asks_for_an_interval_it_does_not_have(monkeypatch):
    """2h is built from 1m instead of silently returning nothing."""
    feed = MexcFuturesFeed(["BTCUSDT"])
    with pytest.raises(Exception) as exc:
        await feed.history("BTCUSDT", "2h", 10)
    assert "2h" in str(exc.value)


@pytest.mark.asyncio
async def test_mexc_futures_universe_is_contracts_with_turnover(monkeypatch):
    feed = MexcFuturesFeed(["BTCUSDT"])

    async def fake_get(url, params=None):
        if url.endswith("/contract/detail"):
            return {
                "success": True,
                "data": [
                    {"symbol": "BTC_USDT", "baseCoin": "BTC", "quoteCoin": "USDT"},
                    {"symbol": "DOGE_USDT", "baseCoin": "DOGE", "quoteCoin": "USDT"},
                    {"symbol": "BTC_USDC", "baseCoin": "BTC", "quoteCoin": "USDC"},
                    {"symbol": "DEAD_USDT", "baseCoin": "DEAD", "quoteCoin": "USDT"},
                ],
            }
        return {
            "success": True,
            "data": [
                {"symbol": "BTC_USDT", "lastPrice": 50000, "amount24": 900.0, "riseFallRate": 0.01},
                {"symbol": "DOGE_USDT", "lastPrice": 1, "amount24": 90.0, "riseFallRate": -0.02},
                {"symbol": "BTC_USDC", "lastPrice": 50000, "amount24": 5.0, "riseFallRate": 0},
            ],
        }

    monkeypatch.setattr(feed, "_get_json", fake_get)
    items = await feed.universe()
    assert [item["symbol"] for item in items] == ["BTCUSDT", "DOGEUSDT", "DEADUSDT"], (
        "turnover ranks contracts, but a missing ticker must not erase an enabled contract"
    )
    assert items[0]["market"] == "futures"
    assert items[-1]["ticker"] is False and items[-1]["turnover"] == 0
    assert feed.universe_diagnostics["eligible"] == 3
    assert feed.universe_diagnostics["missing_tickers"] == 1


@pytest.mark.asyncio
async def test_mexc_futures_universe_survives_a_failed_ticker_endpoint(monkeypatch):
    feed = MexcFuturesFeed(["BTCUSDT"])

    async def fake_get(url, params=None):
        if url.endswith("/contract/detail"):
            return {
                "success": True,
                "data": [
                    {"symbol": f"COIN{i}_USDT", "baseCoin": f"COIN{i}",
                     "quoteCoin": "USDT", "state": 0, "isHidden": False}
                    for i in range(520)
                ] + [
                    {"symbol": "PAUSED_USDT", "quoteCoin": "USDT", "state": 4},
                    {"symbol": "HIDDEN_USDT", "quoteCoin": "USDT", "state": 0,
                     "isHidden": "true"},
                ],
            }
        raise RuntimeError("ticker temporarily unavailable")

    monkeypatch.setattr(feed, "_get_json", fake_get)
    items = await feed.universe()
    assert len(items) == 520
    assert all(item["symbol"].endswith("USDT") for item in items)
    assert feed.universe_diagnostics["ticker_rows"] == 0
    assert feed.universe_diagnostics["ticker_error"]


@pytest.mark.asyncio
async def test_mexc_futures_universe_rejects_an_empty_or_changed_detail_shape(monkeypatch):
    feed = MexcFuturesFeed(["BTCUSDT"])

    async def fake_get(url, params=None):
        return {"success": True, "data": {"contracts": []}}

    monkeypatch.setattr(feed, "_get_json", fake_get)
    with pytest.raises(Exception, match="no data list"):
        await feed.universe()


def test_the_page_can_read_setups_and_a_chart(tmp_path):
    """The two endpoints the new UI is built on."""
    data = tmp_path / "data"
    data.mkdir()
    rows = []
    start = 1_700_000_000_000
    for i in range(200):
        price = 100 + (i % 7)
        rows.append(f"{start + i * 60_000},{price},{price + 1},{price - 1},{price},5")
    (data / "BTCUSDT-1m.csv").write_text("\n".join(rows) + "\n")

    from bot.feed import ReplayFeed

    config = {
        "symbols": ["BTCUSDT"],
        "exchange": "mexc",
        "market": "futures",
        "max_symbols": 10,
        "timeframes": ["5m"],
        "tdi": {"rsi_period": 14, "band_period": 14, "fast_period": 1, "slow_period": 3, "band_mult": 1.6185},
        "structure": {"pivot_len": 2, "sweep_max_wait": 60},
        "filters": {"structure": True, "hvb": False, "trend_context": False, "require_bos": False},
        "server": {"host": "127.0.0.1", "port": 8791},
    }
    config_path = tmp_path / "config.json"
    config_store.write(config_path, config)
    feed = ReplayFeed(data, ["BTCUSDT"], speed=1000, loop=False)
    engine = Engine(
        symbols=["BTCUSDT"],
        timeframes=["5m"],
        tdi_settings=config["tdi"],
        filters=config["filters"],
        structure=config["structure"],
    )
    app = create_app(config, feed, engine, config_path)
    with TestClient(app) as client:
        setups = client.get("/api/setups").json()
        assert "setups" in setups and "counts" in setups
        chart = client.get("/api/chart", params={"symbol": "BTCUSDT", "timeframe": "5m"}).json()
        assert "candles" in chart and "markers" in chart
        assert set(chart["markers"]) == {"bos", "hvb"}, "the chart overlay has both markers"
        state = client.get("/api/state").json()
        assert state["market"] == "futures"
        assert "setups_valid" in state and "backfilled" in state
