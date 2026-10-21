"""Tests for the 30-day history pipeline, the win-rate study and the setup radar."""

import numpy as np
import pandas as pd
import pytest

from app.config import SCAN_TIMEFRAMES, TIMEFRAMES
from app.core import winrate
from app.core.backtest import BacktestConfig, run_backtest
from app.core.data.history import get_history
from app.core.data.synthetic import generate_candles
from app.core.signals import build_signal, scan
from app.core.strategies import get_strategy


def test_scalping_timeframes_exist():
    assert SCAN_TIMEFRAMES == ["1m", "3m", "5m", "15m", "30m"]
    for tf in SCAN_TIMEFRAMES:
        assert tf in TIMEFRAMES


def test_history_bar_count_matches_window():
    # 30 days of 5m candles = 8640 bars
    df, source = get_history("BTCUSDT", "5m", 30)
    assert source in ("live", "cache", "demo")
    assert 8000 <= len(df) <= 8700
    expected_span = 30 * 24 * 3600
    span = (df["time"].iloc[-1] - df["time"].iloc[0]) / 1000
    assert abs(span - expected_span) < 6 * 3600


def test_history_is_stable_and_cached():
    a, _ = get_history("ETHUSDT", "15m", 30)
    b, source = get_history("ETHUSDT", "15m", 30)
    assert source == "cache" or source == "demo"
    overlap = a.merge(b, on="time", suffixes=("_a", "_b")).tail(50)
    assert np.allclose(overlap["close_a"], overlap["close_b"])


@pytest.mark.parametrize("timeframe", SCAN_TIMEFRAMES)
def test_demo_prices_agree_across_timeframes(timeframe):
    df = generate_candles("SOLUSDT", timeframe, 200)
    reference = generate_candles("SOLUSDT", "1m", 200)
    assert abs(df["close"].iloc[-1] / reference["close"].iloc[-1] - 1) < 0.02


def test_winrate_report_shape():
    report = winrate.report("BTCUSDT", days=30, timeframes=["1m", "5m"])
    assert report["symbol"] == "BTCUSDT"
    assert len(report["rows"]) == 2
    for row in report["rows"]:
        assert "error" not in row, row
        assert row["bars"] > 100
        assert row["source"] in ("live", "cache", "demo")
        if row["trades"]:
            assert 0 <= row["win_rate_pct"] <= 100
            assert row["wins"] + row["losses"] == row["trades"]
    assert "win_rate_pct" in report["summary"]
    assert report["caveat"]


def test_winrate_is_cached(monkeypatch):
    first = winrate.report("XRPUSDT", days=7, timeframes=["15m"], refresh=True)
    calls = {"n": 0}
    original = winrate._run_one

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(winrate, "_run_one", counting)
    second = winrate.report("XRPUSDT", days=7, timeframes=["15m"])
    assert calls["n"] == 0, "second call should be served from cache"
    assert second["generated_at"] == first["generated_at"]


def test_strategy_without_stops_never_exits_on_a_stop(candles):
    strategy = get_strategy("tdi_shift")
    params = strategy.resolve_params({"confluence_window": 3, "hvb_step": 1.0})
    prepared = strategy.prepare(candles, params)
    triggers = strategy.rules(prepared, params)
    result = run_backtest(prepared, triggers, strategy, params, BacktestConfig(), include_curve=False)
    assert all(trade.exit_reason in ("signal", "end_of_data") for trade in result.trades)
    assert all(trade.stop != trade.stop for trade in result.trades), "no stop price should be set"


def test_setup_level_is_on_the_correct_side():
    for tf in ("1m", "5m", "15m"):
        signal = build_signal("BTCUSDT", tf, "tdi_shift", limit=500)
        setup = signal["setup"]
        assert setup["status"] in ("TRIGGERED", "ARMED", "FORMING", "IDLE")
        if setup["level"] is not None and setup["direction"]:
            if setup["direction"] == "LONG":
                assert setup["level"] > signal["price"]
            else:
                assert setup["level"] < signal["price"]
            assert setup["distance_pct"] is not None


def test_scan_rows_expose_setup_fields():
    result = scan(["BTCUSDT", "ETHUSDT"], "5m", "tdi_shift")
    for row in result["rows"]:
        assert row.get("setup_status") in ("TRIGGERED", "ARMED", "FORMING", "IDLE")


def test_backtest_metrics_are_json_safe_for_signal_only_mode():
    import json

    from app.utils import sanitize

    report = winrate.report("DOGEUSDT", days=7, timeframes=["15m"], refresh=True)
    json.dumps(sanitize(report))  # would raise on NaN / numpy leakage


def test_probe_reports_the_data_trail(monkeypatch, tmp_path):
    from app.core.data import history

    monkeypatch.setattr(history, "history_cache_path", lambda *a: tmp_path / "none.csv")
    info = history.probe("BTCUSDT", "15m", days=30)

    assert info["symbol"] == "BTCUSDT"
    assert info["timeframe"] == "15m"
    assert info["bars_expected"] == 2880
    assert info["bars"] > 2000
    assert info["first_candle"] < info["last_candle"]
    # one entry per configured host, each with a verdict
    assert {h["host"] for h in info["hosts"]} == set(history.BINANCE_HOSTS)
    assert all({"ok", "ms", "detail"} <= set(h) for h in info["hosts"])
    assert info["source"] in ("live", "cache", "demo")
    assert info["is_real"] is (info["source"] in ("live", "cache"))


def test_probe_never_claims_demo_is_real(monkeypatch, tmp_path):
    from app.core.data import history

    monkeypatch.setattr(history, "history_cache_path", lambda *a: tmp_path / "none.csv")
    monkeypatch.setattr(history, "BINANCE_HOSTS", ["https://127.0.0.1:9"])
    info = history.probe("BTCUSDT", "1h", days=2)
    assert info["hosts_ok"] == []
    assert info["source"] == "demo"
    assert info["is_real"] is False


def test_real_only_refuses_to_invent_numbers(monkeypatch, tmp_path):
    """--real-only must fail loudly rather than report demo win rates."""
    from app.core import winrate
    from app.core.data import history

    monkeypatch.setattr(history, "history_cache_path", lambda *a: tmp_path / "none.csv")
    monkeypatch.setattr(history, "BINANCE_HOSTS", ["https://127.0.0.1:9"])
    monkeypatch.setattr(winrate, "get_history", history.get_history)

    payload = winrate.report("BTCUSDT", days=2, timeframes=["15m"], allow_demo=False, refresh=True)
    assert payload["rows"][0].get("error")
    assert payload["summary"]["trades"] == 0
    assert payload["is_real_data"] is False
    assert payload["caveat"].startswith("NO DATA")


def test_trades_detail_is_auditable(monkeypatch, tmp_path):
    """Every trade row carries timestamps so it can be checked on a chart."""
    from app.core import winrate
    from app.core.data import history

    monkeypatch.setattr(history, "history_cache_path", lambda *a: tmp_path / "none.csv")
    detail = winrate.trades_detail("BTCUSDT", days=30, timeframe="15m")

    assert detail["source"] == "demo"
    assert detail["wins"] + detail["losses"] == len(detail["trades"])
    if detail["trades"]:
        t = detail["trades"][0]
        assert t["entry_time_utc"] <= t["exit_time_utc"]
        assert t["entry_price"] > 0 and t["exit_price"] > 0
        assert t["exit_reason"]
        expected = round(detail["wins"] / len(detail["trades"]) * 100, 2)
        assert detail["win_rate_pct"] == expected
