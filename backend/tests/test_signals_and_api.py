import json

import pytest
from fastapi.testclient import TestClient

from app.core.signals import build_signal, scan
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_signal_payload_shape():
    signal = build_signal("BTCUSDT", "1h", "donchian", limit=400)
    assert signal["action"] in ("BUY", "SELL", "HOLD LONG", "HOLD SHORT", "EXIT", "WAIT")
    assert signal["source"] in ("live", "cache", "demo")
    assert 0 <= signal["confidence"] <= 100
    assert signal["plan"]["side"] in ("LONG", "SHORT")
    assert len(signal["chart"]["time"]) > 50
    # Chart arrays must line up with the time axis.
    n = len(signal["chart"]["time"])
    assert len(signal["chart"]["series"]["close"]) == n
    for line in signal["chart"]["lines"].values():
        assert len(line["values"]) == n


def test_long_plan_has_stop_below_entry_and_targets_above():
    signal = build_signal("ETHUSDT", "4h", "donchian", limit=500)
    plan = signal["plan"]
    if plan["side"] == "LONG":
        assert plan["stop"] < plan["entry"]
        assert all(tp > plan["entry"] for tp in plan["take_profits"])
    else:
        assert plan["stop"] > plan["entry"]
        assert all(tp < plan["entry"] for tp in plan["take_profits"])


def test_scan_ranks_actionable_signals_first():
    result = scan(["BTCUSDT", "ETHUSDT", "SOLUSDT"], "1h", "donchian")
    assert len(result["rows"]) == 3
    ranks = [r["action"] for r in result["rows"] if not r.get("error")]
    order = ["BUY", "SELL", "HOLD LONG", "HOLD SHORT", "EXIT", "WAIT"]
    assert ranks == sorted(ranks, key=lambda a: order.index(a))


def test_parameter_overrides_are_applied():
    default = build_signal("BTCUSDT", "1h", "donchian")
    custom = build_signal("BTCUSDT", "1h", "donchian", {"channel": 55, "adx_min": 0})
    assert default["params"]["channel"] == 20
    assert custom["params"]["channel"] == 55
    assert custom["params"]["adx_min"] == 0


def test_health_and_meta(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    meta = client.get("/api/meta").json()
    ids = [s["id"] for s in meta["strategies"]]
    assert "donchian" in ids and "custom" in ids
    assert meta["timeframes"]
    assert meta["settings"]["watchlist"]


def test_signal_endpoint_is_strict_json(client):
    response = client.get("/api/signal", params={"symbol": "BTCUSDT", "timeframe": "1h", "strategy": "donchian"})
    assert response.status_code == 200
    json.loads(response.text)  # would raise on NaN / numpy leakage


def test_backtest_endpoint(client):
    response = client.post(
        "/api/backtest",
        json={"symbol": "BTCUSDT", "timeframe": "1h", "strategy": "donchian", "bars": 500},
    )
    assert response.status_code == 200
    body = response.json()
    assert "metrics" in body and "trades" in body and "equity_curve" in body


def test_unknown_strategy_returns_404(client):
    response = client.get("/api/signal", params={"strategy": "does-not-exist"})
    assert response.status_code == 404


def test_settings_roundtrip_masks_token(client):
    saved = client.put(
        "/api/settings",
        json={"watchlist": ["BTCUSDT", "ETHUSDT"], "alerts": {"telegram_bot_token": "secret-token", "telegram_chat_id": "42"}},
    ).json()
    assert saved["watchlist"] == ["BTCUSDT", "ETHUSDT"]
    assert saved["alerts"]["telegram_bot_token"] == "••••••••"

    # Re-saving the masked value must not wipe the stored secret.
    again = client.put("/api/settings", json={"alerts": {"telegram_bot_token": "••••••••"}}).json()
    assert again["alerts"]["telegram_configured"] is True
    from app.core.store import get_settings
    assert get_settings()["alerts"]["telegram_bot_token"] == "secret-token"


def test_watchlist_cannot_be_empty(client):
    response = client.put("/api/settings", json={"watchlist": []})
    assert response.status_code == 400
