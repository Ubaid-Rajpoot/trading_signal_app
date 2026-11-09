"""A real recorded chain-v8 setup must survive the complete production path.

The compact fixture is an unmodified BTCUSDT one-minute slice around the
2024-12-11 setup in the public historical regression corpus.  It is not a
synthetic alert: MEXC-style 3m derivation, indicators, structure, every shipped
v4.1 filter, publication integrity, lifecycle registration, and SQLite storage
all run here.
"""

from __future__ import annotations

import csv
import gzip
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.engine import CURRENT_CHAIN_VERSION  # noqa: E402
from bot.store import SignalStore  # noqa: E402
from run import load_config, make_engine  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "BTCUSDT-3m-chain8-2024-12.csv.gz"
EXPECTED_SIGNAL_MS = 1_733_923_979_999


def recorded_minutes() -> list[dict]:
    rows: list[dict] = []
    with gzip.open(FIXTURE, "rt", newline="") as handle:
        for row in csv.reader(handle):
            opened = int(row[0])
            rows.append(
                {
                    "symbol": "BTCUSDT",
                    "open_time": opened,
                    "close_time": opened + 59_999,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "trades": 0,
                    "closed": True,
                }
            )
    return rows


class DerivedThreeMinuteFeed:
    """The production MEXC contract shape: no native 3m, derive it from 1m."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.requests: list[tuple[str, int]] = []

    @staticmethod
    def exchange_interval(timeframe: str) -> str | None:
        return None if timeframe == "3m" else timeframe

    async def history(
        self, symbol: str, interval: str, limit: int = 500, end_time: int | None = None,
    ) -> list[dict]:
        assert symbol == "BTCUSDT" and interval == "1m"
        self.requests.append((interval, limit))
        rows = self.rows
        if end_time is not None:
            rows = [row for row in rows if row["close_time"] <= end_time]
        return rows[-limit:]


@pytest.mark.asyncio
async def test_production_calendar_window_requests_thirty_days_for_derived_3m():
    config = load_config()
    config["symbols"] = ["BTCUSDT"]
    config["timeframes"] = ["3m"]
    engine = make_engine(config)
    feed = DerivedThreeMinuteFeed(recorded_minutes())

    await engine.warm_symbol(feed, "BTCUSDT", force=True)

    assert engine.history_bars == 5000 and engine.history_days == 30
    assert feed.requests == [("1m", 43_209)]
    assert engine.kinds.get("signal") == 1


@pytest.mark.asyncio
async def test_real_history_reaches_publication_persistence_and_current_policy(tmp_path):
    config = load_config()
    config["symbols"] = ["BTCUSDT"]
    config["timeframes"] = ["3m"]
    config["history_bars"] = 1000
    config["history_days"] = 0
    engine = make_engine(config)
    store = SignalStore(tmp_path / "signals.sqlite3")
    engine.attach_store(store)
    feed = DerivedThreeMinuteFeed(recorded_minutes())

    await engine.warm_symbol(feed, "BTCUSDT", force=True)

    assert feed.requests == [("1m", 3009)], "the unsupported MEXC 3m cell must use real 1m bars"
    assert engine.kinds.get("signal") == 1
    assert engine.decision_funnel()["passed"] == 1
    assert len(engine.setup_payloads) == 1

    signal = next(iter(engine.setup_payloads.values()))
    setup = signal["setup"]
    assert signal["created_ms"] == EXPECTED_SIGNAL_MS
    assert signal["chain_version"] == CURRENT_CHAIN_VERSION == 8
    assert signal["chain_valid"] is True and signal["hvb_lag"] == 0
    assert signal["side"] == "LONG" and signal["hvb_side"] == "up"
    assert setup["swept_side"] == "low"
    assert setup["sweep_displacement"]["direction"] == "up"
    assert setup["break_displacement"]["direction"] == "up"
    assert setup["sweep_displacement"]["body_atr"] >= 1.0
    assert setup["break_displacement"]["body_atr"] >= 1.0

    archived = store.list(limit=10)
    assert len(archived) == 1
    assert archived[0]["key"] == signal["key"]
    assert archived[0]["chain_valid"] is True
