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
        test_client.engine = engine  # type: ignore[attr-defined]
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


def test_selected_chart_is_anchored_by_archive_key_and_exposes_the_chain(client):
    created = 1_735_690_200_000
    key = f"BTCUSDT|5m|{created}"
    payload = {
        "key": key, "kind": "signal", "strategy": "sweep", "symbol": "BTCUSDT",
        "timeframe": "5m", "side": "SHORT", "created_ms": created, "time": created,
        "state": "pending", "valid": True, "entry": 98.0, "stop": 102.0, "target": 90.0,
        "swept_side": "high", "swept_level": 101.0, "sweep_ms": created - 300_000,
        "bos_level": 99.0, "bos_ms": created, "hvb_side": "down", "hvb_ms": created,
        "hvb_lag": 0,
        "setup": {"side": "SHORT", "swept_side": "high", "swept_level": 101.0,
                  "sweep_time": created - 300_000, "sweep_high": 102.0,
                  "broke_level": 99.0, "break_time": created},
    }
    client.engine.store.record(payload)  # type: ignore[attr-defined]
    body = client.get(
        "/api/chart", params={"symbol": "BTCUSDT", "timeframe": "5m", "key": key, "at": created}
    ).json()
    assert body["selected"]["key"] == key
    assert body["selected"]["chain_valid"] is True
    assert body["focus_index"] >= 0


def test_live_chart_returns_the_forming_candle_and_mark_to_market_rr(client):
    key = "BTCUSDT|5m|1735690200000"
    payload = {
        "key": key, "kind": "signal", "strategy": "sweep", "symbol": "BTCUSDT",
        "timeframe": "5m", "side": "SHORT", "created_ms": 1_735_690_200_000,
        "state": "active", "valid": True, "mitigated": True,
        "entry": 100.0, "stop": 104.0, "stop0": 104.0, "risk0": 4.0,
        "target": 90.0, "leg_target": 90.0, "touched_ms": 1_735_690_500_000,
        "target_model": "hvb_bos_leg_extreme_v1", "lifecycle_version": 6,
    }
    client.engine.setup_payloads[key] = payload  # type: ignore[attr-defined]
    cell = client.engine.cells[("BTCUSDT", "5m")]  # type: ignore[attr-defined]
    cell.candles.clear()
    cell.candles.append({
        "symbol": "BTCUSDT", "timeframe": "5m", "open_time": 1_735_690_800_000,
        "close_time": 1_735_691_099_999, "open": 100.0, "high": 101.0,
        "low": 98.0, "close": 99.0, "volume": 10.0, "closed": True,
    })
    cell.forming = {
        "symbol": "BTCUSDT", "timeframe": "5m", "open_time": 1_735_691_100_000,
        "close_time": 1_735_691_399_999, "open": 99.0, "high": 99.5,
        "low": 97.0, "close": 98.0, "volume": 4.0, "closed": False,
    }

    body = client.get(
        "/api/chart",
        params={"symbol": "BTCUSDT", "timeframe": "5m", "key": key, "live": 1},
    ).json()
    assert body["historical_window"] is False
    assert body["candles"][-1]["closed"] is False
    assert body["live"]["forming"] is True and body["live"]["price"] == 98.0
    assert body["live"]["planned_rr"] == 2.5
    assert body["live"]["current_r"] == 0.5
    assert body["live"]["remaining_rr"] == pytest.approx(1.3333)

    # The next exchange update replaces the same forming bar; the live endpoint
    # must move both the candle and R immediately without closing it.
    cell.forming.update({"close": 97.0, "low": 96.5, "volume": 6.0})
    moved = client.get(
        "/api/chart",
        params={"symbol": "BTCUSDT", "timeframe": "5m", "key": key, "live": 1},
    ).json()
    assert moved["candles"][-1]["c"] == 97.0
    assert moved["candles"][-1]["closed"] is False
    assert moved["live"]["current_r"] == 0.75
    assert moved["live"]["remaining_rr"] == pytest.approx(1.0)


def test_page_has_bounded_signal_scrolling_and_smc_style_chart_controls(client):
    page = client.get("/").text
    css = client.get("/app.css").text
    script = client.get("/app.js").text
    assert all(name in page for name in (
        "btn-chart-live", "btn-chart-in", "btn-chart-out", "btn-chart-reset",
        "chart-audit", "chart-current-r", "chart-remaining-rr",
    ))
    assert "min-height: 0" in css and "overscroll-behavior: contain" in css
    assert "tdigm.ui.preferences.v2" in script and "savePreferences" in script
    assert "BODY BREAK + HVB" in script and "HIGH SWEEP" in script
    assert "DIRECT SWEEP APPROACH" in script and "PAIRED LOW" in script
    assert "refreshLiveChart, 1000" in script and 'mode === "live" ? "&live=1"' in script


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


class FailingUniverseFeed(FakeExchangeFeed):
    async def universe(self):
        raise RuntimeError("contract detail unavailable")


class FlakyUniverseFeed(FakeExchangeFeed):
    def __init__(self):
        super().__init__()
        self.universe_calls = 0

    async def universe(self):
        self.universe_calls += 1
        if self.universe_calls == 1:
            raise RuntimeError("temporary detail failure")
        return await super().universe()


def _exchange_app(tmp_path, feed, **updates):
    config_path = tmp_path / "config.json"
    config = json.loads(json.dumps(CONFIG))
    config.update(updates)
    config_store.write(config_path, config)
    engine = Engine(
        symbols=config["symbols"],
        timeframes=config["timeframes"],
        tdi_settings=config["tdi"],
        filters=config["filters"],
        structure=config["structure"],
        history_bars=240,
    )
    return create_app(config, feed, engine, config_path), engine, config_path


def test_auto_universe_failure_is_visible_and_keeps_the_fallback(tmp_path):
    app, engine, _path = _exchange_app(
        tmp_path, FailingUniverseFeed(), auto_universe=True,
        universe_min_expected=10, startup_history_bars=120,
    )
    with TestClient(app) as client:
        body = client.get("/api/health").json()
        assert body["universe"]["mode"] == "configured_fallback"
        assert body["universe"]["attempts"] >= 1
        assert "contract detail unavailable" in body["universe"]["error"]
        assert body["symbols"] == 1 == len(engine.symbols)


def test_auto_universe_retry_recovers_from_the_fallback(tmp_path):
    feed = FlakyUniverseFeed()
    app, engine, _path = _exchange_app(
        tmp_path, feed, auto_universe=True, max_symbols=20,
        universe_min_expected=10, startup_history_bars=120,
    )
    with TestClient(app) as client:
        before = client.get("/api/health").json()["universe"]
        assert before["mode"] == "configured_fallback" and before["error"]
        recovered = client.post("/api/universe/refresh").json()
        assert recovered == {
            "ok": True, "error": None, "available": 50,
            "selected": 20, "mode": "discovered",
        }
        assert len(engine.symbols) == 20
        assert feed.universe_calls == 2


def test_auto_universe_replaces_fallback_and_reports_discovered_count(tmp_path):
    app, engine, config_path = _exchange_app(
        tmp_path, FakeExchangeFeed(), auto_universe=True, max_symbols=20,
        universe_min_expected=10, startup_history_bars=120,
    )
    with TestClient(app) as client:
        body = client.get("/api/health").json()
        listed = client.get("/api/universe").json()
        assert body["universe"]["mode"] == "discovered"
        assert body["universe"]["available"] == listed["count"] == 50
        assert body["symbols"] == len(engine.symbols) == 20
        assert body["coverage"]["symbols_total"] == 20
    assert len(json.loads(config_path.read_text())["symbols"]) == 20


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


# ------------------------------------------------------------ notifications
def test_the_page_is_served_with_its_own_assets(client):
    """One HTML file, one stylesheet, one script - and the queue they share."""
    page = client.get("/")
    assert page.status_code == 200
    assert "/app.js" in page.text and "/notify.js" in page.text
    assert page.text.count('class="toast"') == 1, "there is exactly one toast slot"
    assert 'id="toasts"' not in page.text, "the old stack is gone"
    for asset in ("/app.js", "/app.css", "/notify.js"):
        assert client.get(asset).status_code == 200


def test_notifications_are_counted_even_when_nothing_is_shown(client):
    """A warning is recorded and counted, and it never becomes a toast."""
    body = client.get("/api/notifications").json()
    assert set(body["counts"]) == {"unread", "events", "filtered", "suppressed"}
    assert body["counts"]["events"] == len(body["events"])
    for event in body["events"]:
        assert event["severity"] != "action", "only an actionable event may be sent as a toast"
        assert event["category"] == "system"
    health = client.get("/api/health").json()["notifications"]
    assert health["events"] == body["counts"]["events"]
    # every engine message that is *not* worth keeping is counted, never dropped silently
    assert health["suppressed"] >= 0 and health["filtered"] >= 0


def test_marking_read_clears_the_badge(client):
    body = client.post("/api/notifications/read").json()
    assert body["ok"] is True
    assert body["counts"]["unread"] == 0
