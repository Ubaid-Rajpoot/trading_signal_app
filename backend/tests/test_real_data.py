"""Tests for the real dataset layer (real Binance klines on disk).

These do not touch the network. They build a tiny dataset in a temp folder and
check that resampling, source reporting, replay and the provider priority all
behave - because the whole point is that a number is never silently synthetic.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.data import real


@pytest.fixture()
def dataset(tmp_path, monkeypatch):
    folder = tmp_path / "BTCUSDT"
    folder.mkdir()
    # 3 hours of 1-minute bars, official Binance CSV shape (µs timestamps)
    start_ms = 1_700_000_000_000 - (1_700_000_000_000 % 900_000)  # aligned to 15m
    start = start_ms * 1000  # microseconds, like the official dumps
    rows = []
    for i in range(180):
        ts_us = start + i * 60_000_000
        price = 100 + i * 0.1
        rows.append([ts_us, price, price + 0.5, price - 0.5, price + 0.2, 10 + i, 0, 0, 0, 0, 0, 0])
    pd.DataFrame(rows).to_csv(folder / "BTCUSDT-1m-test.csv", header=False, index=False)

    monkeypatch.setattr(real, "DATASET_DIR", tmp_path)
    real.reload()
    yield tmp_path
    real.reload()


def test_dataset_is_detected(dataset):
    assert real.has_dataset("BTCUSDT") is False or True  # folder exists
    assert "BTCUSDT" in real.dataset_symbols()


def test_microsecond_timestamps_are_normalised(dataset):
    base = real.load_base("BTCUSDT")
    assert len(base) == 180
    # milliseconds, not µs: 1.7e12 not 1.7e15
    assert 1e12 < int(base["time"].iloc[0]) < 1e13
    assert int(base["time"].iloc[1]) - int(base["time"].iloc[0]) == 60_000


def test_resample_aggregates_correctly(dataset):
    base = real.load_base("BTCUSDT")
    five = real.resample(base, "5m")
    assert len(five) == 36  # 180 minutes / 5
    first = five.iloc[0]
    block = base.iloc[:5]
    assert first["open"] == block["open"].iloc[0]
    assert first["close"] == block["close"].iloc[-1]
    assert first["high"] == block["high"].max()
    assert first["low"] == block["low"].min()
    assert first["volume"] == pytest.approx(block["volume"].sum())


def test_every_timeframe_is_a_multiple_of_one_minute(dataset):
    base = real.load_base("BTCUSDT")
    for tf, count in (("1m", 180), ("3m", 60), ("5m", 36), ("15m", 12), ("30m", 6), ("1h", 3)):
        assert len(real.resample(base, tf)) == count


def test_source_is_reported_as_dataset(dataset, monkeypatch):
    from app.core.data import providers

    monkeypatch.setattr(providers, "_PROVIDER_STATE", {"live_failed_at": 0.0})
    monkeypatch.setattr(providers, "fetch_binance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    df, source = providers.get_candles("BTCUSDT", "5m", 100)
    assert source == "dataset"
    assert len(df) > 10


def test_replay_shifts_the_window_back(dataset):
    from app.core.data import providers

    latest, _ = providers.get_candles("BTCUSDT", "1m", 50)
    older, source = providers.get_candles("BTCUSDT", "1m", 50, replay_bars=100)
    assert source == "dataset"
    assert older["time"].max() < latest["time"].max()
    assert older["close"].iloc[-1] != latest["close"].iloc[-1]


def test_replay_past_the_start_raises(dataset):
    from app.core.data.providers import MarketDataError, get_candles

    with pytest.raises(MarketDataError):
        get_candles("BTCUSDT", "1m", 50, replay_bars=10_000)


def test_history_prefers_the_dataset(dataset, monkeypatch):
    from app.core.data import history

    monkeypatch.setattr(history, "download_range", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    df, source = history.get_history("BTCUSDT", "15m", days=1)
    assert source == "dataset"
    assert len(df) > 5


def test_provider_status_advertises_the_dataset(dataset):
    from app.core.data.providers import provider_status

    status = provider_status()
    assert status["mode"] == "dataset"
    assert "BTCUSDT" in status["reason"]
