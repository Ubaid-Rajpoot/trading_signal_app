"""The signal journal: a signal that was printed once can always be printed again.

The complaint that produced this file: "signals come and then they vanish again".
They vanished because the page only holds what the scanner has in memory right
now - a zone that is mitigated, then pruned, is gone. Every signal is therefore
written to SQLite when it forms and updated until it is finished, and nothing in
that table is ever deleted. These tests pin the storage, the life cycle and the
read side the page and the CSV export use.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.journal import SignalJournal  # noqa: E402


def zone(**over) -> dict:
    base = {
        "key": "BTC_USDT|5m|algo_bull|1700000000000|low",
        "symbol": "BTC_USDT",
        "market": "futures",
        "timeframe": "5m",
        "kind": "algo_bull",
        "side": "low",
        "grade": "A+",
        "prob": 0.93,
        "measured": {"letter": "A+", "win": 0.97, "n": 73},
        "cond_met": 8,
        "cond_total": 10,
        "level": 100.0,
        "fee_r": 0.18,
        "net_r": 0.55,
        "tradable": True,
        "signal": True,
        "status": "active",
        "plan_state": "waiting",
        "time": 1700000000000,
        "plan": {"side": "long", "entry": 99.5, "stop": 98.0, "target": 102.0,
                 "rr": 1.6, "risk_pct": 1.5},
    }
    base.update(over)
    return base


def test_a_signal_is_stored_with_every_detail_a_trader_needs(tmp_path):
    store = SignalJournal(tmp_path / "journal.sqlite3")
    assert store.enabled, store.problem
    assert store.record(zone())
    rows = store.list()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "BTC_USDT" and row["timeframe"] == "5m"
    assert row["entry"] == 99.5 and row["stop"] == 98.0 and row["target"] == 102.0
    assert row["grade"] == "A+" and row["prob"] == 0.93
    assert row["band_win"] == 0.97 and row["band_n"] == 73
    assert row["signal"] == 1 and row["tradable"] == 1


def test_signal_it_stays_in_the_database_after_it_is_gone_from_the_map(tmp_path):
    store = SignalJournal(tmp_path / "journal.sqlite3")
    store.record(zone())
    store.close_signal(zone()["key"], status="mitigated", at=1700000600000,
                       result="target", result_r=1.6)
    rows = store.list()
    assert len(rows) == 1, "resolving a signal must never delete it"
    row = rows[0]
    assert row["status"] == "mitigated"
    assert row["result"] == "target" and row["result_r"] == 1.6
    assert row["closed_at"] == 1700000600000
    # and the counter the page shows agrees with the table
    stats = store.stats()
    assert stats["signals"] == 1 and stats["decided"] == 1 and stats["wins"] == 1
    assert stats["hit_rate"] == 1.0


def test_the_journal_survives_a_restart(tmp_path):
    path = tmp_path / "journal.sqlite3"
    first = SignalJournal(path)
    first.record(zone())
    first.close()
    second = SignalJournal(path)
    assert [r["key"] for r in second.list()] == [zone()["key"]], "the file is the record"
    second.close()


def test_a_plan_that_moves_updates_the_same_row(tmp_path):
    store = SignalJournal(tmp_path / "journal.sqlite3")
    store.record(zone())
    store.record(zone(plan_state="filled", net_r=0.34))
    rows = store.list()
    assert len(rows) == 1, "one signal is one row, however often it changes"
    assert rows[0]["plan_state"] == "filled" and rows[0]["net_r"] == 0.34


def test_the_archive_can_be_filtered_and_exported(tmp_path):
    store = SignalJournal(tmp_path / "journal.sqlite3")
    store.record(zone())
    store.record(zone(key="ETH_USDT|1m|algo_bear|1700000060000|high", symbol="ETH_USDT",
                      timeframe="1m", side="high", grade="B", prob=0.78))
    store.record(zone(key="XRP_USDT|1m|algo_bear|1700000120000|high", symbol="XRP_USDT",
                      grade="C", prob=0.62, signal=False))
    assert len(store.list()) == 3
    assert [r["symbol"] for r in store.list(symbol="ETH")] == ["ETH_USDT"]
    assert len(store.list(grade="A+,B")) == 2
    assert len(store.list(signals_only=True)) == 2, "the C band is archived but is not a signal"
    assert len(store.list(min_prob=0.9)) == 1
    csv_text = store.csv()
    assert csv_text.splitlines()[0].startswith("formed_at,symbol,timeframe")
    assert "ETH_USDT" in csv_text and len(csv_text.splitlines()) == 4


def test_a_broken_path_never_stops_the_scanner(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    store = SignalJournal(blocker / "journal.sqlite3")   # cannot be created
    assert not store.enabled and store.problem
    assert store.record(zone()) is False
    assert store.list() == [] and store.stats()["enabled"] is False


def test_the_row_keeps_the_whole_zone_for_later(tmp_path):
    store = SignalJournal(tmp_path / "journal.sqlite3")
    store.record(zone())
    row = store.get(zone()["key"])
    assert row and isinstance(row["snapshot"], dict)
    assert row["snapshot"]["plan"]["target"] == 102.0
    assert json.dumps(row["snapshot"])  # serialisable: it came out of the JSON column


def test_a_later_update_never_erases_the_result(tmp_path):
    """The bug that made the archive lose its details.

    ``sync_journal`` re-writes a zone on every change. The zone object still says
    ``result: None`` after the plan has resolved, so a plain UPDATE blanked the
    stored outcome on the very next push - the row stayed, but "target, +1.6 R"
    was gone. Settled facts are now write-once.
    """
    store = SignalJournal(tmp_path / "journal.sqlite3")
    store.record(zone())
    store.close_signal(zone()["key"], status="active", result="target", result_r=1.6,
                       at=1700000600000)
    # the scanner pushes the same zone again, with no result on it
    store.record(zone(plan_state="target"))
    row = store.list()[0]
    assert row["result"] == "target", "the outcome survives the next push"
    assert row["result_r"] == 1.6 and row["closed_at"] == 1700000600000
    stats = store.stats()
    assert stats["decided"] == 1 and stats["avg_r"] == 1.6


def test_the_hit_rate_is_reported_for_signals_and_for_everything_else(tmp_path):
    """Two different questions, two different numbers.

    The archive keeps the C band as well (a 62% setup is worth reading, it is just
    not a signal). A single "hit rate" over both rows would be quoted as the
    scoreboard of what the page told anyone to trade - so the signal-only figures
    are counted separately and the drawer prints them first.
    """
    store = SignalJournal(tmp_path / "journal.sqlite3")
    good = zone()
    meh = zone(key="ETH_USDT|1m|algo_bear|1700000060000|high", symbol="ETH_USDT", grade="C",
               prob=0.62, signal=False)
    store.record(good)
    store.record(meh)
    store.close_signal(good["key"], status="active", result="target", result_r=1.6)
    store.close_signal(meh["key"], status="active", result="stopped", result_r=-1.0)
    stats = store.stats()
    assert stats["signal_decided"] == 1 and stats["signal_wins"] == 1
    assert stats["signal_hit_rate"] == 1.0 and stats["signal_avg_r"] == 1.6
    assert stats["decided"] == 2 and stats["hit_rate"] == 0.5
    assert round(stats["avg_r"], 3) == 0.3


def test_a_restart_reaps_what_it_can_no_longer_follow(tmp_path):
    """Signals must not pile up as "open" for a run that has already ended.

    A restart throws the in-memory map away, so the plans it held can never reach a
    target or a stop. Those rows used to stay ``active`` for ever: the drawer said
    "open now" about trades nobody was following, which reads as signals that both
    vanish and never close. Reaping marks them - and deletes nothing.
    """
    store = SignalJournal(tmp_path / "journal.sqlite3")
    now = 1_800_000_000_000
    old = zone(key="OLD|5m|algo_bull|1|low", symbol="OLD_USDT")
    fresh = zone(key="NEW|5m|algo_bull|2|low", symbol="NEW_USDT")
    store.record(old, now=now - 48 * 3_600_000)
    store.record(fresh, now=now - 60_000)
    assert store.stats()["open"] == 2
    assert store.reap(now=now, max_age_hours=6) == 1
    rows = {r["key"]: r for r in store.list()}
    assert len(rows) == 2, "nothing is deleted"
    assert rows[old["key"]]["status"] == "interrupted"
    assert "restarted" in (rows[old["key"]]["note"] or "")
    assert rows[fresh["key"]]["status"] == "active", "a row still being written is not touched"
    assert store.stats()["open"] == 1
    assert store.stats()["interrupted"] == 1


def test_an_old_database_gains_the_new_column_without_losing_a_row(tmp_path):
    """A build before the ``note`` column must upgrade in place."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE signals (key TEXT PRIMARY KEY, symbol TEXT, market TEXT, timeframe TEXT,"
        " kind TEXT, side TEXT, direction TEXT, grade TEXT, prob REAL, band_win REAL, band_n INTEGER,"
        " cond_met INTEGER, cond_total INTEGER, level REAL, entry REAL, stop REAL, target REAL,"
        " rr REAL, risk_pct REAL, fee_r REAL, net_r REAL, tradable INTEGER, signal INTEGER,"
        " status TEXT, plan_state TEXT, result TEXT, result_r REAL, formed_at INTEGER,"
        " first_seen INTEGER, last_seen INTEGER, closed_at INTEGER, snapshot TEXT);"
        "INSERT INTO signals (key, symbol, timeframe, kind, side, status) "
        "VALUES ('k1', 'BTC_USDT', '5m', 'algo_bull', 'low', 'active');"
    )
    db.commit()
    db.close()
    store = SignalJournal(path)
    assert store.enabled, store.problem
    rows = store.list()
    assert len(rows) == 1 and rows[0]["symbol"] == "BTC_USDT"
    assert rows[0]["note"] is None


def test_a_decided_outcome_is_final(tmp_path):
    """Two runs of the same candles must not flip a finished signal.

    After a restart the scanner can replay a zone whose row already says "target,
    +1.6 R"; a second resolution for the same key used to overwrite that with
    "stopped, -1 R" - which both lied about what the user saw and moved the
    archive's own hit rate between runs.
    """
    store = SignalJournal(tmp_path / "journal.sqlite3")
    store.record(zone())
    store.close_signal(zone()["key"], status="active", result="target", result_r=1.6, at=10)
    store.close_signal(zone()["key"], status="mitigated", result="stopped", result_r=-1.0, at=20)
    row = store.list()[0]
    assert row["result"] == "target" and row["result_r"] == 1.6
    # but a row that only reached "mitigated" may still go on to hit its target
    other = zone(key="X|5m|algo_bull|9|low", symbol="X_USDT")
    store.record(other)
    store.close_signal(other["key"], status="mitigated", result="mitigated")
    store.close_signal(other["key"], status="active", result="target", result_r=0.9)
    assert [r for r in store.list() if r["key"] == other["key"]][0]["result"] == "target"


def test_the_archive_pages(tmp_path):
    """A grid needs "1-12 of 200", so the read side must page and count."""
    store = SignalJournal(tmp_path / "journal.sqlite3")
    for i in range(25):
        store.record(zone(key=f"S{i}_USDT|5m|algo_bull|{i}|low", symbol=f"S{i}_USDT",
                          formed_at=1_700_000_000_000 + i))
    page1 = store.list(limit=10, offset=0)
    page2 = store.list(limit=10, offset=10)
    page3 = store.list(limit=10, offset=20)
    assert len(page1) == len(page2) == 10 and len(page3) == 5
    keys = {r["key"] for r in page1} | {r["key"] for r in page2} | {r["key"] for r in page3}
    assert len(keys) == 25, "paging never repeats or drops a row"
    assert store.count(signals_only=True) == 25
    # symbol is a search, not an exact match: "S1_" hits S1_ and S10_..S19_
    assert store.count(symbol="S1_") == 11
    assert store.count(symbol="S3_USDT") == 1
