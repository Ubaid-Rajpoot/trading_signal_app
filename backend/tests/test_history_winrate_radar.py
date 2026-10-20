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
