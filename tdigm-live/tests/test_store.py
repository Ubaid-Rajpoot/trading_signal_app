"""The archive: a signal that was printed once must stay printable.

The complaint behind this file is the one smc-radar already answered - the page
was a window on the scanner, and a restart erased the record of everything found
so far. These tests pin what the database has to guarantee:

* a setup is written the moment it is found, and its whole life is written too;
* a restart shows the archive before a single candle arrives;
* a decided outcome is final (a re-scan must not turn a win into "pending");
* the archive survives a second process opening it (that is the whole point).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.engine import Engine  # noqa: E402
from bot.store import SCHEMA, SignalStore, row_of  # noqa: E402

PAYLOAD = {
    "key": "BTCUSDT|1h|1700000000000",
    "symbol": "BTCUSDT",
    "timeframe": "1h",
    "side": "SHORT",
    "grade": "A",
    "state": "pending",
    "valid": True,
    "mitigated": False,
    "created_ms": 1_700_000_000_000,
    "entry": 100.0,
    "stop": 104.0,
    "target": 92.0,
    "target_2r": None,
    "target_model": "hvb_bos_leg_extreme_v1",
    "leg_target": 92.0,
    "leg_target_ms": 1_699_999_900_000,
    "leg_start_ms": 1_699_996_400_000,
    "leg_break_ms": 1_700_000_000_000,
    "rr": 2.0,
    "score": 100.0,
    "zone": {"high": 101.0, "low": 99.0, "mid": 100.0},
    "depth": {"value": 2, "label": "internal"},
    "hvb_side": "down",
    "hvb_lag": 0,
    "break_kind": "MS",
    "swept_level": 104.0,
    "swept_side": "high",
    "sweep_ms": 1_699_996_400_000,
    "bos_level": 98.0,
    "bos_ms": 1_700_000_000_000,
    "hvb_ms": 1_700_000_000_000,
    "strategy": "sweep",
    "setup": {
        "side": "SHORT", "swept_side": "high", "swept_level": 104.0,
        "sweep_time": 1_699_996_400_000, "sweep_high": 105.0, "sweep_low": 99.0,
        "broke_level": 98.0, "break_time": 1_700_000_000_000,
    },
    "volume_ratio": 1.8,
    "trend_dir": -1,
    "trend_clean": True,
    "reasons": ["swept the high at 104", "broke structure at 98 (MS)"],
}


def current_payload(**updates):
    """A complete chain-v7 archive row, including both displacement proofs."""
    sweep_time = PAYLOAD["setup"]["sweep_time"]
    break_time = PAYLOAD["setup"]["break_time"]
    sweep_displacement = {
        "model": "directed_body_over_prior_atr_v1", "event_time": sweep_time,
        "expected_direction": "up", "direction": "up",
        "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.5,
        "body": 4.5, "prior_atr": 3.0, "body_atr": 1.5,
        "min_body_atr": 1.0, "passes": True,
    }
    break_displacement = {
        "model": "directed_body_over_prior_atr_v1", "event_time": break_time,
        "expected_direction": "down", "direction": "down",
        "open": 100.0, "high": 101.0, "low": 96.0, "close": 97.0,
        "body": 3.0, "prior_atr": 2.0, "body_atr": 1.5,
        "min_body_atr": 1.0, "passes": True,
    }
    setup = {
        **PAYLOAD["setup"],
        "leg_model": "directional_outer_v1",
        "approach_model": "direct_from_paired_endpoint_v1",
        "approach_start_time": sweep_time - 1,
        "approach_end_time": sweep_time,
        "approach_bars": 1,
        "approach_pivot_count": 0,
        "swept_count": 1,
        "swept_levels": [{"side": "high", "price": 104.0}],
        "swept_level_time": sweep_time - 2,
        "broke_level_time": sweep_time - 1,
        "displacement_model": "directed_body_over_prior_atr_v1",
        "sweep_open": 100.0, "sweep_high": 105.0, "sweep_low": 99.0, "sweep_close": 104.5,
        "break_open": 100.0, "break_high": 101.0, "break_low": 96.0, "break_close": 97.0,
        "sweep_displacement": sweep_displacement,
        "break_displacement": break_displacement,
    }
    payload = {
        **PAYLOAD,
        "chain_version": 7,
        "lifecycle_version": 7,
        "hvb_model": "mbl_centre_cross_v1",
        "hvb_prev_fast": 55.0,
        "hvb_prev_centre": 50.0,
        "hvb_fast": 45.0,
        "hvb_centre": 49.0,
        "displacement_model": "directed_body_over_prior_atr_v1",
        "sweep_displacement": sweep_displacement,
        "break_displacement": break_displacement,
        "setup": setup,
    }
    return {**payload, **updates}


def test_a_row_carries_everything_a_trader_filters_on():
    row = row_of(PAYLOAD)
    assert row["symbol"] == "BTCUSDT" and row["timeframe"] == "1h"
    assert row["zone_high"] == 101.0 and row["zone_low"] == 99.0
    assert row["depth"] == 2 and row["grade"] == "A"
    assert row["target_model"] == "hvb_bos_leg_extreme_v1" and row["leg_target"] == 92.0
    assert json.loads(row["evidence"])[0].startswith("swept the high")
    assert json.loads(row["payload"])["key"] == PAYLOAD["key"]


def test_a_setup_is_written_then_kept_up_to_date(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    assert store.enabled and store.problem is None

    assert store.record(PAYLOAD)
    rows = store.list()
    assert len(rows) == 1 and rows[0]["state"] == "pending" and rows[0]["stored"] is True

    store.record({**PAYLOAD, "state": "active", "valid": True, "mitigated": True})
    rows = store.list()
    assert len(rows) == 1, "the same setup is one row, not two"
    assert rows[0]["state"] == "active" and rows[0]["mitigated"] is True
    assert store.counts()["total"] == 1
    assert store.counts()["valid"] == 1 and store.counts()["unmitigated"] == 0


def test_one_signal_can_be_loaded_and_quarantined_without_deletion(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    store.record(PAYLOAD)
    assert store.get(PAYLOAD["key"])["setup"]["sweep_high"] == 105.0
    assert store.quarantine(PAYLOAD["key"], ["sweep time is missing"])
    row = store.get(PAYLOAD["key"])
    assert row["valid"] is False and row["state"] == "invalidated"
    assert row["chain_errors"] == ["sweep time is missing"]
    assert row["state_reason"] == "integrity quarantine: sweep time is missing"
    assert store.counts()["total"] == 1, "an integrity failure is retained for audit, not deleted"


def test_a_decided_outcome_is_final(tmp_path):
    """A re-scan replays history: it must never rewrite a win back to pending."""
    store = SignalStore(tmp_path / "sig.sqlite3")
    store.record(PAYLOAD)
    store.record({
        **PAYLOAD, "state": "win", "display_state": "win",
        "valid": False, "closed_ms": 1_700_000_500_000,
    })
    corrected = {
        **PAYLOAD,
        "state": "pending", "display_state": "active", "valid": True, "closed_ms": None,
        "entry": 99.0, "stop": 106.0, "target": 88.0,
        "swept_level": 103.0,
        "setup": {**PAYLOAD["setup"], "swept_level": 103.0, "sweep_high": 105.0},
    }
    store.record(corrected)
    row = store.list()[0]
    assert row["state"] == "win", "a decided result is never overwritten"
    assert row["display_state"] == "win", "a replay cannot relabel a decided row active"
    assert row["closed_ms"] == 1_700_000_500_000
    assert (row["entry"], row["stop"], row["target"]) == (100.0, 104.0, 92.0)
    assert row["swept_level"] == 103.0 and row["setup"]["swept_level"] == 103.0, (
        "a rescan may correct structural evidence without rewriting the outcome"
    )


def test_a_new_lifecycle_version_can_regrade_an_old_final_outcome(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    store.record({**PAYLOAD, "state": "win", "valid": False, "r_now": 2.0})
    upgraded = {
        **PAYLOAD,
        "lifecycle_version": 7,
        "state": "pending", "valid": True, "mitigated": False,
        "target": None, "rr": None, "r_now": None,
    }
    store.record(upgraded)
    row = store.get(PAYLOAD["key"])
    assert row["lifecycle_version"] == 7
    assert row["state"] == "pending" and row["target"] is None


def test_the_archive_outlives_the_process(tmp_path):
    path = tmp_path / "sig.sqlite3"
    first = SignalStore(path)
    first.record(PAYLOAD)
    first.close()

    second = SignalStore(path)
    rows = second.list()
    assert len(rows) == 1 and rows[0]["key"] == PAYLOAD["key"], (
        "opening the file again is the whole point: the signal survived the restart"
    )


def test_an_existing_database_is_migrated_for_confirmation_leg_evidence(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    new_columns = {
        "target_model", "leg_target", "leg_target_ms", "leg_start_ms", "leg_break_ms",
        "chain_version",
    }
    legacy_schema = "\n".join(
        line for line in SCHEMA.splitlines()
        if not line.strip().split(maxsplit=1)[0:1]
        or line.strip().split(maxsplit=1)[0] not in new_columns
    )
    with sqlite3.connect(path) as database:
        database.executescript(legacy_schema)

    store = SignalStore(path)
    assert store.enabled and store.record({**PAYLOAD, "lifecycle_version": 7})
    columns = {row[1] for row in store._db.execute("PRAGMA table_info(signals)").fetchall()}
    assert new_columns <= columns
    restored = store.get(PAYLOAD["key"])
    assert restored["target_model"] == "hvb_bos_leg_extreme_v1"
    assert restored["leg_target"] == 92.0


def test_filters_read_the_archive_not_the_process(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    store.record(PAYLOAD)
    store.record({**PAYLOAD, "key": "ETHUSDT|15m|1", "symbol": "ETHUSDT", "timeframe": "15m", "grade": "B"})
    assert [r["symbol"] for r in store.list(timeframe="1h")] == ["BTCUSDT"]
    assert [r["grade"] for r in store.list(grade="B")] == ["B"]
    assert [r["symbol"] for r in store.list(search="eth")] == ["ETHUSDT"]
    assert store.list(valid=0) == []


def test_results_use_variable_leg_target_r_and_exclude_legacy_or_unfilled_rows(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    current = {
        **PAYLOAD,
        "chain_version": 7,
        "lifecycle_version": 7,
        "target_model": "hvb_bos_leg_extreme_v1",
        "mitigated": True,
    }
    store.record({**current, "key": "A", "state": "win", "valid": False,
                  "target_rr": 2.25, "r_now": 2.25})
    store.record({**current, "key": "B", "state": "stopped", "valid": False,
                  "target_rr": 1.4, "r_now": -1.0})
    store.record({**current, "key": "C", "state": "pending", "valid": True,
                  "mitigated": False, "target_rr": None, "r_now": None})
    store.record({**PAYLOAD, "key": "legacy", "state": "win", "valid": False})
    store.record({**current, "key": "book", "state": "win", "valid": False,
                  "target_model": "book_plan", "r_now": 9.0})
    store.record({**current, "key": "wrong-chain", "state": "win", "valid": False,
                  "chain_version": 5, "r_now": 9.0})
    store.record({**current, "key": "future", "state": "win", "valid": False,
                  "lifecycle_version": 8, "r_now": 9.0})
    results = store.results()["all"]
    assert results["wins"] == 1 and results["losses"] == 1
    assert results["mitigated"] == 2
    assert results["win_rate"] == 0.5
    assert results["r_per_trade"] == 0.625, "(+2.25R - 1R) / 2 decided mitigated trades"
    full = store.results()
    assert full["target_rr"] is None and full["lifecycle_version"] == 7
    assert full["chain_version"] == 7
    assert full["target_model"] == "hvb_bos_leg_extreme_v1"
    assert "confirmation-leg H/L" in full["r_basis"]


def test_a_broken_disk_never_stops_the_scanner(tmp_path):
    bad = tmp_path / "nope" / "x.sqlite3"
    bad.parent.mkdir()
    (bad).write_text("not a database")  # sqlite will refuse this file
    store = SignalStore(bad)
    # either it opened a real file after all, or it degraded cleanly - never raise
    assert store.enabled is False or store.record(PAYLOAD)


def test_the_engine_writes_through_and_reads_back_on_boot(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    engine = Engine(
        symbols=["BTCUSDT"],
        timeframes=["1h"],
        tdi_settings={"rsi_period": 14, "band_period": 14, "fast_period": 1, "slow_period": 3},
        filters={"trend_context": False, "require_bos": False},
        structure={"pivot_len": 2},
        strategy="sweep",
    )
    engine.attach_store(store)
    engine.market = "futures"
    current = current_payload()
    engine._remember(current)  # the same call the current publish path makes

    # a restart: a brand new engine, the same database
    fresh = Engine(
        symbols=["BTCUSDT"],
        timeframes=["1h"],
        tdi_settings={"rsi_period": 14, "band_period": 14, "fast_period": 1, "slow_period": 3},
        filters={},
        structure={"pivot_len": 2},
        strategy="sweep",
    )
    fresh.attach_store(SignalStore(tmp_path / "sig.sqlite3"))
    restored = fresh.load_stored()
    assert restored == 1
    assert fresh.setup_payloads[PAYLOAD["key"]]["symbol"] == "BTCUSDT"
    assert fresh.open_setups(valid_only=True), "the archive row is on the board at boot"
    assert PAYLOAD["key"] in fresh.registry, "a valid row is a live state machine, not stale JSON"

    # A new post-restart candle invalidates the untouched short before entry and
    # writes that state back through the same payload.
    cell = fresh.cells[("BTCUSDT", "1h")]
    fresh._track_setups(cell, {"open": 103, "high": 105, "low": 102, "close": 104,
                               "close_time": PAYLOAD["created_ms"] + 3_600_000})
    assert fresh.setup_payloads[PAYLOAD["key"]]["state"] == "invalidated"


def test_restart_keeps_mitigation_visible_and_persists_the_later_resolution(tmp_path):
    path = tmp_path / "sig.sqlite3"
    active = current_payload(
        state="active", display_state="mitigated", valid=True, mitigated=True,
        touched_ms=PAYLOAD["created_ms"] + 60_000,
        state_since=PAYLOAD["created_ms"] + 60_000,
        risk0=4.0, stop0=104.0, target=92.0, rr=2.0,
    )
    first = SignalStore(path)
    assert first.record(active)
    first.close()

    engine = Engine(
        symbols=["BTCUSDT"], timeframes=["1h"],
        tdi_settings={"rsi_period": 14, "band_period": 14, "fast_period": 1, "slow_period": 3},
        filters={}, structure={"pivot_len": 2}, strategy="sweep",
    )
    store = SignalStore(path)
    engine.attach_store(store)
    assert engine.load_stored() == 1
    restored = engine.setup_payloads[PAYLOAD["key"]]
    assert restored["state"] == "active" and restored["display_state"] == "mitigated"
    assert PAYLOAD["key"] in engine.registry

    cell = engine.cells[("BTCUSDT", "1h")]
    engine._track_setups(cell, {
        "open": 99.0, "high": 100.0, "low": 91.0, "close": 92.0,
        "close_time": PAYLOAD["created_ms"] + 7_200_000,
    })
    resolved = store.get(PAYLOAD["key"])
    assert resolved["state"] == "win" and resolved["display_state"] == "win"
    assert resolved["mitigated"] is True and resolved["closed_ms"] is not None


def test_startup_audits_and_quarantines_a_legacy_signal_without_a_sweep(tmp_path):
    path = tmp_path / "sig.sqlite3"
    store = SignalStore(path)
    bad = {**PAYLOAD, "key": "XRPUSDT|1d|1769644800000", "symbol": "XRPUSDT",
           "timeframe": "1d", "sweep_ms": 0,
           "setup": {"side": "SHORT", "swept_side": "high", "swept_level": 2.0,
                     "broke_level": 1.8, "break_time": 1_769_644_800_000}}
    store.record(bad)
    engine = Engine(
        symbols=["XRPUSDT"], timeframes=["1d"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={"hvb": True}, structure={"pivot_len": 5}, strategy="sweep",
    )
    engine.attach_store(store)
    engine.load_stored()
    row = store.get(bad["key"])
    assert engine.integrity_checked == 1 and engine.integrity_quarantined == 1
    assert row["valid"] is False and row["state"] == "invalidated"
    assert any("sweep time" in reason for reason in row["chain_errors"])
    assert bad["key"] not in engine.registry


def test_startup_quarantines_a_versioned_signal_without_paired_endpoint_times(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    versioned = {**PAYLOAD, "chain_version": 2}
    store.record(versioned)
    engine = Engine(
        symbols=["BTCUSDT"], timeframes=["1h"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={"hvb": True}, structure={"pivot_len": 5}, strategy="sweep",
    )
    engine.attach_store(store)
    engine.load_stored()
    row = store.get(versioned["key"])
    assert row["state"] == "invalidated"
    assert any("paired-leg endpoint proof" in reason for reason in row["chain_errors"])
    assert versioned["key"] not in engine.registry


def test_startup_does_not_rehydrate_legacy_tradeable_without_single_sweep_proof(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    legacy = {
        **PAYLOAD,
        "chain_version": 2,
        "setup": {
            **PAYLOAD["setup"],
            "swept_level_time": PAYLOAD["setup"]["sweep_time"] - 2,
            "broke_level_time": PAYLOAD["setup"]["sweep_time"] - 1,
        },
    }
    store.record(legacy)
    engine = Engine(
        symbols=["BTCUSDT"], timeframes=["1h"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={"hvb": True}, structure={"pivot_len": 5}, strategy="sweep",
    )
    engine.attach_store(store)
    engine.load_stored()
    row = store.get(legacy["key"])
    assert row["state"] == "invalidated"
    assert any("exactly one" in reason or "swept-level proof" in reason
               for reason in row["chain_errors"])
    assert legacy["key"] not in engine.registry


def test_startup_quarantines_v3_without_single_swept_level_proof(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    versioned = {
        **PAYLOAD,
        "chain_version": 3,
        "setup": {
            **PAYLOAD["setup"],
            "swept_level_time": PAYLOAD["setup"]["sweep_time"] - 2,
            "broke_level_time": PAYLOAD["setup"]["sweep_time"] - 1,
        },
    }
    store.record(versioned)
    engine = Engine(
        symbols=["BTCUSDT"], timeframes=["1h"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={"hvb": True}, structure={"pivot_len": 5}, strategy="sweep",
    )
    engine.attach_store(store)
    engine.load_stored()
    row = store.get(versioned["key"])
    assert row["state"] == "invalidated"
    assert any("exactly one" in reason or "swept-level proof" in reason
               for reason in row["chain_errors"])
    assert versioned["key"] not in engine.registry


def test_startup_requires_deep_replay_for_a_tradeable_v3_row(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    old_current = {
        **PAYLOAD,
        "chain_version": 3,
        "lifecycle_version": 3,
        "setup": {
            **PAYLOAD["setup"],
            "swept_count": 1,
            "swept_levels": [{"side": "high", "price": 104.0}],
            "swept_level_time": PAYLOAD["setup"]["sweep_time"] - 2,
            "broke_level_time": PAYLOAD["setup"]["sweep_time"] - 1,
        },
    }
    store.record(old_current)
    engine = Engine(
        symbols=["BTCUSDT"], timeframes=["1h"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={"hvb": True}, structure={"pivot_len": 5}, strategy="sweep",
    )
    engine.attach_store(store)
    engine.load_stored()
    row = store.get(old_current["key"])
    assert row["state"] == "invalidated"
    assert any("deep replay required" in reason for reason in row["chain_errors"])
    assert old_current["key"] not in engine.registry


def test_startup_quarantines_v6_without_direct_sweep_approach_proof(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    versioned = {
        **PAYLOAD,
        "chain_version": 5,
        "lifecycle_version": 6,
        "setup": {
            **PAYLOAD["setup"],
            "leg_model": "directional_outer_v1",
            "swept_count": 1,
            "swept_levels": [{"side": "high", "price": 104.0}],
            "swept_level_time": PAYLOAD["setup"]["sweep_time"] - 2,
            "broke_level_time": PAYLOAD["setup"]["sweep_time"] - 1,
        },
    }
    store.record(versioned)
    engine = Engine(
        symbols=["BTCUSDT"], timeframes=["1h"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={"hvb": True}, structure={"pivot_len": 5}, strategy="sweep",
    )
    engine.attach_store(store)
    engine.load_stored()
    row = store.get(versioned["key"])
    assert row["state"] == "invalidated"
    assert any("direct paired-endpoint-to-sweep" in reason for reason in row["chain_errors"])
    assert versioned["key"] not in engine.registry


def test_startup_quarantines_current_row_without_mbl_cross_proof(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    versioned = {
        **PAYLOAD,
        "chain_version": 6,
        "lifecycle_version": 7,
        "setup": {
            **PAYLOAD["setup"],
            "leg_model": "directional_outer_v1",
            "approach_model": "direct_from_paired_endpoint_v1",
            "approach_start_time": PAYLOAD["setup"]["sweep_time"] - 1,
            "approach_end_time": PAYLOAD["setup"]["sweep_time"],
            "approach_bars": 1,
            "approach_pivot_count": 0,
            "swept_count": 1,
            "swept_levels": [{"side": "high", "price": 104.0}],
            "swept_level_time": PAYLOAD["setup"]["sweep_time"] - 2,
            "broke_level_time": PAYLOAD["setup"]["sweep_time"] - 1,
        },
    }
    store.record(versioned)
    engine = Engine(
        symbols=["BTCUSDT"], timeframes=["1h"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={"hvb": True}, structure={"pivot_len": 5}, strategy="sweep",
    )
    engine.attach_store(store)
    engine.load_stored()
    row = store.get(versioned["key"])
    assert row["state"] == "invalidated"
    assert any("moving-centre-band HVB crossing proof" in reason
               for reason in row["chain_errors"])
    assert versioned["key"] not in engine.registry


def test_startup_quarantines_v6_without_confirmation_leg_target_proof(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    versioned = {
        key: value for key, value in PAYLOAD.items()
        if key not in {"target_model", "leg_target", "leg_target_ms", "leg_start_ms", "leg_break_ms"}
    }
    versioned.update({
        "chain_version": 5,
        "lifecycle_version": 6,
        "setup": {
            **PAYLOAD["setup"],
            "leg_model": "directional_outer_v1",
            "approach_model": "direct_from_paired_endpoint_v1",
            "approach_start_time": PAYLOAD["setup"]["sweep_time"] - 1,
            "approach_end_time": PAYLOAD["setup"]["sweep_time"],
            "approach_bars": 1,
            "approach_pivot_count": 0,
            "swept_count": 1,
            "swept_levels": [{"side": "high", "price": 104.0}],
            "swept_level_time": PAYLOAD["setup"]["sweep_time"] - 2,
            "broke_level_time": PAYLOAD["setup"]["sweep_time"] - 1,
        },
    })
    store.record(versioned)
    engine = Engine(
        symbols=["BTCUSDT"], timeframes=["1h"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={"hvb": True}, structure={"pivot_len": 5}, strategy="sweep",
    )
    engine.attach_store(store)
    engine.load_stored()
    row = store.get(versioned["key"])
    assert row["state"] == "invalidated"
    assert any("confirmation-leg H/L target proof" in reason for reason in row["chain_errors"])
    assert versioned["key"] not in engine.registry


def test_startup_quarantines_v4_without_directional_outer_leg_proof(tmp_path):
    store = SignalStore(tmp_path / "sig.sqlite3")
    versioned = {
        **PAYLOAD,
        "chain_version": 4,
        "lifecycle_version": 4,
        "setup": {
            **PAYLOAD["setup"],
            "swept_count": 1,
            "swept_levels": [{"side": "high", "price": 104.0}],
            "swept_level_time": PAYLOAD["setup"]["sweep_time"] - 2,
            "broke_level_time": PAYLOAD["setup"]["sweep_time"] - 1,
        },
    }
    store.record(versioned)
    engine = Engine(
        symbols=["BTCUSDT"], timeframes=["1h"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={"hvb": True}, structure={"pivot_len": 5}, strategy="sweep",
    )
    engine.attach_store(store)
    engine.load_stored()
    row = store.get(versioned["key"])
    assert row["state"] == "invalidated"
    assert any("directional outer-leg proof" in reason for reason in row["chain_errors"])
    assert versioned["key"] not in engine.registry


def test_the_record_carries_an_honest_error_bar(tmp_path):
    """An average without a confidence interval is a claim the page cannot back."""
    store = SignalStore(tmp_path / "sig.sqlite3")
    for i in range(12):
        row = dict(
            PAYLOAD,
            key=f"BTCUSDT|1h|{1_700_000_000_000 + i}",
            state="win",
            mitigated=True,
            chain_version=7,
            lifecycle_version=7,
            r_now=1.5 if i % 2 else -1.0,
        )
        store.record(row)
    rows = store.results()["all"]
    assert {"se", "t", "ci_low", "ci_high", "verdict", "scored"} <= set(rows)
    assert rows["verdict"] in {"not enough trades", "not proven", "positive", "negative"}
    if rows["scored"] < 10:
        assert rows["verdict"] == "not enough trades"
    else:
        assert rows["ci_low"] <= rows["r_per_trade"] <= rows["ci_high"]
