"""Tests for the market scan: the universe and the one-instrument scan step."""

from __future__ import annotations

import pytest

from app.core.data.symbols import universe
from app.core.signals import build_signal
from scripts_import import scan_module


def test_universe_falls_back_to_the_curated_list_when_offline(monkeypatch):
    from app.core.data import symbols as mod

    monkeypatch.setattr(mod, "_download", lambda quote: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(mod, "UNIVERSE_FILE", mod.UNIVERSE_FILE.with_name("does-not-exist.json"))
    info = mod.universe("USDT", 5)
    assert info["source"] == "curated"
    assert len(info["symbols"]) == 5
    assert all(s.endswith("USDT") for s in info["symbols"])


def test_universe_reports_a_live_source(tmp_path, monkeypatch):
    from app.core.data import symbols as mod

    rows = [{"symbol": s, "quote_volume": v} for s, v in (("BTCUSDT", 9e9), ("ETHUSDT", 5e9))]
    monkeypatch.setattr(mod, "_download", lambda quote: rows)
    monkeypatch.setattr(mod, "UNIVERSE_FILE", tmp_path / "universe.json")
    info = mod.universe("USDT", 10)
    assert info["source"] == "live"
    assert info["symbols"] == ["BTCUSDT", "ETHUSDT"]


def test_scan_one_returns_the_setup_fields():
    row = scan_module().scan_one("BTCUSDT", "15m", "tdi_shift", {}, 500)
    assert row["status"] in ("TRIGGERED", "ARMED", "FORMING", "BLOCKED", "IDLE", "ERROR")
    assert row["symbol"] == "BTCUSDT" and row["timeframe"] == "15m"
    assert row["source"] in ("live", "cache", "demo")
    if row["status"] != "ERROR":
        assert "price" in row


def test_scan_one_survives_a_bad_strategy():
    """One broken request must not abort the whole scan."""
    row = scan_module().scan_one("BTCUSDT", "15m", "no_such_strategy", {}, 300)
    assert row["status"] == "ERROR" and row["error"]
    assert row["symbol"] == "BTCUSDT"


def test_radar_top_scope_uses_the_universe(monkeypatch):
    from app.api import routes

    monkeypatch.setattr(routes, "symbol_universe", lambda *a, **k: {"symbols": ["BTCUSDT", "ETHUSDT"], "source": "curated"})
    result = routes.radar(symbols="", scope="top", limit=2, timeframes="15m", strategy="tdi_shift", params=None)
    assert result["scope"] == "top"
    assert result["symbols"] == ["BTCUSDT", "ETHUSDT"]
