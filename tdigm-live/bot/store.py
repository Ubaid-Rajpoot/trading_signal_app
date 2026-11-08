"""Every setup this bot has ever found, kept in SQLite.

The page used to be a *window* on the scanner: a setup existed in memory while the
process ran, and a restart erased the record of it. The trader's complaint is the
one smc-radar already answered - a signal that was printed once should stay
printable, and a server that went down must not take the history with it.

So every published setup is written to SQLite the moment it is created, and the
same row is updated all the way through its life:

    pending  ->  active  ->  win | stopped
                    \\----->  expired | invalidated

Nothing is deleted. ``/api/signals`` reads it back with filters, the page's
*History* view shows the whole archive including the setups the live list has
already dropped, and the numbers on the board (win rate, R per trade) are computed
from these rows - not from whatever happens to be in memory.

Design notes
------------
* stdlib ``sqlite3`` only: no new dependency, and the file sits next to the
  candles (``data/tdigm.sqlite3``), which is already git-ignored.
* WAL + ``synchronous=NORMAL``: a write every few seconds must never fsync-stall
  the scanner.
* The whole setup is also stored as JSON in ``payload`` so a row is readable on
  its own years later - the columns are for filtering, the payload is the record.
* A decided outcome is write-once: a later re-scan that replays the same setup
  must never rewrite "target, +1.8 R" into "expired".
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    key          TEXT PRIMARY KEY,
    symbol       TEXT NOT NULL,
    market       TEXT NOT NULL DEFAULT 'futures',
    timeframe    TEXT NOT NULL,
    side         TEXT NOT NULL,
    grade        TEXT,
    state        TEXT NOT NULL DEFAULT 'pending',
    valid        INTEGER NOT NULL DEFAULT 1,
    mitigated    INTEGER NOT NULL DEFAULT 0,
    entry        REAL,
    stop         REAL,
    target       REAL,
    target_2r    REAL,
    target_rr    REAL,
    target_rr_price REAL,
    target_model TEXT,
    leg_target   REAL,
    leg_target_ms INTEGER,
    leg_start_ms INTEGER,
    leg_break_ms INTEGER,
    rr           REAL,
    r_now        REAL,
    score        REAL,
    depth        INTEGER,
    volume_ratio REAL,
    hvb_side     TEXT,
    hvb_lag      INTEGER,
    break_kind   TEXT,
    zone_high    REAL,
    zone_low     REAL,
    swept_level  REAL,
    bos_level    REAL,
    trend_dir    INTEGER,
    trend_clean  INTEGER,
    created_ms   INTEGER NOT NULL,
    state_since  INTEGER,
    touched_ms   INTEGER,
    closed_ms    INTEGER,
    chain_version INTEGER NOT NULL DEFAULT 1,
    lifecycle_version INTEGER NOT NULL DEFAULT 1,
    mitigation_kind TEXT,
    state_reason TEXT,
    bars         INTEGER DEFAULT 0,
    evidence     TEXT,
    payload      TEXT,
    first_seen   INTEGER,
    last_seen    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_ms DESC);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, timeframe, created_ms DESC);
CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state, created_ms DESC);
CREATE INDEX IF NOT EXISTS idx_signals_grade ON signals(grade, created_ms DESC);
"""

# columns added after the first release: an old database is upgraded in place,
# because the archive is the one thing here that must never be thrown away
MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("grade", "ALTER TABLE signals ADD COLUMN grade TEXT"),
    ("hvb_lag", "ALTER TABLE signals ADD COLUMN hvb_lag INTEGER"),
    ("mitigated", "ALTER TABLE signals ADD COLUMN mitigated INTEGER NOT NULL DEFAULT 0"),
    ("market", "ALTER TABLE signals ADD COLUMN market TEXT NOT NULL DEFAULT 'futures'"),
    ("session", "ALTER TABLE signals ADD COLUMN session TEXT"),
    ("target_rr", "ALTER TABLE signals ADD COLUMN target_rr REAL"),
    ("target_rr_price", "ALTER TABLE signals ADD COLUMN target_rr_price REAL"),
    ("target_model", "ALTER TABLE signals ADD COLUMN target_model TEXT"),
    ("leg_target", "ALTER TABLE signals ADD COLUMN leg_target REAL"),
    ("leg_target_ms", "ALTER TABLE signals ADD COLUMN leg_target_ms INTEGER"),
    ("leg_start_ms", "ALTER TABLE signals ADD COLUMN leg_start_ms INTEGER"),
    ("leg_break_ms", "ALTER TABLE signals ADD COLUMN leg_break_ms INTEGER"),
    ("chain_version", "ALTER TABLE signals ADD COLUMN chain_version INTEGER NOT NULL DEFAULT 1"),
    ("lifecycle_version", "ALTER TABLE signals ADD COLUMN lifecycle_version INTEGER NOT NULL DEFAULT 1"),
    ("mitigation_kind", "ALTER TABLE signals ADD COLUMN mitigation_kind TEXT"),
    ("state_reason", "ALTER TABLE signals ADD COLUMN state_reason TEXT"),
)

# a decided outcome is final - these are never overwritten by a later "live" push
FINAL_STATES = ("win", "stopped", "breakeven")
CURRENT_CHAIN_VERSION = 7
CURRENT_LIFECYCLE_VERSION = 7
CURRENT_TARGET_MODEL = "hvb_bos_leg_extreme_v1"


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return float(value) if value is not None else None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def row_of(payload: dict, market: str = "futures") -> dict[str, Any]:
    """One setup payload -> one row. Everything a trader filters on is a column."""
    zone = payload.get("zone") or {}
    depth = payload.get("depth") or {}
    now = int(time.time() * 1000)
    return {
        "key": str(payload.get("key") or ""),
        "symbol": str(payload.get("symbol") or ""),
        "market": str(payload.get("market") or market),
        "timeframe": str(payload.get("timeframe") or ""),
        "side": str(payload.get("side") or ""),
        "session": payload.get("session"),
        "grade": payload.get("grade"),
        "state": str(payload.get("state") or "pending"),
        "valid": 1 if payload.get("valid") else 0,
        "mitigated": 1 if payload.get("mitigated") else 0,
        "entry": _num(payload.get("entry")),
        "stop": _num(payload.get("stop")),
        "target": _num(payload.get("target")),
        "target_2r": _num(payload.get("target_2r")),
        "target_rr": _num(payload.get("target_rr")),
        "target_rr_price": _num(payload.get("target_rr_price")),
        "target_model": payload.get("target_model"),
        "leg_target": _num(payload.get("leg_target")),
        "leg_target_ms": _int(payload.get("leg_target_ms")),
        "leg_start_ms": _int(payload.get("leg_start_ms")),
        "leg_break_ms": _int(payload.get("leg_break_ms")),
        "rr": _num(payload.get("rr")),
        "r_now": _num(payload.get("r_now")),
        "score": _num(payload.get("score")),
        "depth": _int(depth.get("value") if isinstance(depth, dict) else depth),
        "volume_ratio": _num(payload.get("volume_ratio")),
        "hvb_side": payload.get("hvb_side") or payload.get("hvb"),
        "hvb_lag": _int(payload.get("hvb_lag")),
        "break_kind": payload.get("break_kind"),
        "zone_high": _num(zone.get("high")),
        "zone_low": _num(zone.get("low")),
        "swept_level": _num(payload.get("swept_level")),
        "bos_level": _num(payload.get("bos_level")),
        "trend_dir": _int(payload.get("trend_dir")),
        "trend_clean": None if payload.get("trend_clean") is None else int(bool(payload["trend_clean"])),
        "created_ms": _int(payload.get("created_ms")) or now,
        "state_since": _int(payload.get("state_since")),
        "touched_ms": _int(payload.get("touched_ms")),
        "closed_ms": _int(payload.get("closed_ms")),
        "chain_version": _int(payload.get("chain_version")) or 1,
        "lifecycle_version": _int(payload.get("lifecycle_version")) or 1,
        "mitigation_kind": payload.get("mitigation_kind"),
        "state_reason": payload.get("state_reason"),
        "bars": _int(payload.get("bars")) or 0,
        "evidence": json.dumps(payload.get("reasons") or [], ensure_ascii=False),
        "payload": json.dumps(payload, ensure_ascii=False, default=str),
        "first_seen": now,
        "last_seen": now,
    }


COLUMNS = tuple(row_of({"key": "x", "symbol": "x", "timeframe": "1m", "side": "LONG"}).keys())


class SignalStore:
    """A thin, thread-safe wrapper over one SQLite file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.problem: str | None = None
        self.enabled = path is not None
        self.recorded = 0
        self.updated = 0
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()
        except Exception as exc:  # a broken disk must never stop the scanner
            self.problem = f"{type(exc).__name__}: {exc}"
            self._db = None
            self.enabled = False

    def _migrate(self) -> None:
        assert self._db is not None
        have = {row[1] for row in self._db.execute("PRAGMA table_info(signals)")}
        for name, ddl in MIGRATIONS:
            if name not in have:
                self._db.execute(ddl)

    # ------------------------------------------------------------- writing
    def record(self, payload: dict, market: str = "futures") -> bool:
        """Insert a setup once, then keep its live fields up to date."""
        if not self.enabled or not payload.get("key"):
            return False
        row = row_of(payload, market)
        with self._lock:
            assert self._db is not None
            try:
                existing = self._db.execute(
                    "SELECT state, closed_ms, lifecycle_version, payload FROM signals WHERE key = ?", (row["key"],)
                ).fetchone()
                if existing is None:
                    cols = ", ".join(COLUMNS)
                    marks = ", ".join("?" for _ in COLUMNS)
                    self._db.execute(
                        f"INSERT OR REPLACE INTO signals ({cols}) VALUES ({marks})",
                        tuple(row[c] for c in COLUMNS),
                    )
                    self.recorded += 1
                else:
                    # a decided outcome is final: a re-scan replaying the same
                    # setup must not turn "win" back into "pending"
                    final = existing["state"] in FINAL_STATES
                    lifecycle_upgrade = int(row.get("lifecycle_version") or 1) > int(
                        existing["lifecycle_version"] or 1
                    )
                    updates = {
                        k: v
                        for k, v in row.items()
                        if k not in ("first_seen", "created_ms")
                    }
                    if final and not lifecycle_upgrade:
                        # The outcome is write-once within one lifecycle rule, but
                        # a version upgrade deliberately replays and re-scores it.
                        # A deeper re-scan may
                        # correct immutable structural evidence (for example the
                        # swept endpoint of the paired leg). Merge that evidence
                        # into JSON while retaining the decided lifecycle.
                        try:
                            old_payload = json.loads(existing["payload"] or "{}")
                            new_payload = json.loads(row["payload"] or "{}")
                        except json.JSONDecodeError:
                            old_payload, new_payload = {}, {}
                        for key in (
                            "state", "display_state", "valid", "mitigated", "closed_ms", "state_since",
                            "touched_ms", "bars", "r_now", "last_price", "be_ms",
                            "entry", "stop", "stop0", "risk0", "target", "target_2r",
                            "target_rr", "target_rr_price", "target_model", "leg_target",
                            "leg_target_ms", "leg_start_ms", "leg_break_ms", "rr", "zone", "zones",
                            "mitigation_kind", "mitigation_zone", "state_reason",
                            "breakeven_at_r", "chain_valid", "chain_errors",
                            "integrity_checked_ms", "invalid_reason",
                        ):
                            if key in old_payload:
                                new_payload[key] = old_payload[key]
                        updates["payload"] = json.dumps(new_payload, ensure_ascii=False, default=str)
                        for key in (
                            "state", "valid", "mitigated", "closed_ms", "state_since",
                            "touched_ms", "bars", "r_now", "entry", "target", "target_rr",
                            "target_rr_price", "target_model", "leg_target", "leg_target_ms",
                            "leg_start_ms", "leg_break_ms", "stop", "rr", "zone", "mitigation_kind",
                            "state_reason",
                        ):
                            updates.pop(key, None)
                    sets = ", ".join(f"{k} = ?" for k in updates)
                    self._db.execute(
                        f"UPDATE signals SET {sets} WHERE key = ?",
                        (*updates.values(), row["key"]),
                    )
                    self.updated += 1
                self._db.commit()
                return True
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return False

    def record_many(self, payloads: Iterable[dict], market: str = "futures") -> int:
        return sum(1 for payload in payloads if self.record(payload, market))

    def quarantine(self, key: str, problems: list[str]) -> bool:
        """Mark an archive row non-tradeable without deleting its audit trail.

        This is intentionally separate from ``record``: a structurally invalid
        historical row must be quarantined even when its old lifecycle happened
        to reach a final state.  Its original fields stay in JSON alongside the
        integrity explanation, so the bad row remains inspectable.
        """
        if not self.enabled or self._db is None or not key:
            return False
        with self._lock:
            try:
                row = self._db.execute("SELECT payload FROM signals WHERE key = ?", (key,)).fetchone()
                if row is None:
                    return False
                try:
                    payload = json.loads(row["payload"] or "{}")
                except json.JSONDecodeError:
                    payload = {"key": key}
                payload.update(
                    {
                        "valid": False,
                        "state": "invalidated",
                        "chain_valid": False,
                        "chain_errors": list(problems),
                        "state_reason": "integrity quarantine: " + "; ".join(problems),
                    }
                )
                self._db.execute(
                    "UPDATE signals SET valid = 0, state = 'invalidated', state_reason = ?, payload = ?, last_seen = ? WHERE key = ?",
                    (payload["state_reason"], json.dumps(payload, ensure_ascii=False, default=str),
                     int(time.time() * 1000), key),
                )
                self._db.commit()
                return True
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return False

    # ------------------------------------------------------------- reading
    def get(self, key: str) -> dict[str, Any] | None:
        """Read one complete immutable signal payload by its archive key."""
        if not self.enabled or self._db is None or not key:
            return None
        with self._lock:
            try:
                row = self._db.execute("SELECT payload FROM signals WHERE key = ?", (key,)).fetchone()
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return None
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            return None
        payload["stored"] = True
        return payload

    def list(
        self,
        limit: int = 200,
        *,
        symbol: str = "",
        timeframe: str = "",
        grade: str = "",
        state: str = "",
        side: str = "",
        valid: int | None = None,
        since: int = 0,
        search: str = "",
    ) -> list[dict]:
        if not self.enabled or self._db is None:
            return []
        where: list[str] = []
        args: list[Any] = []
        if symbol:
            where.append("symbol = ?")
            args.append(symbol.upper())
        if search:
            where.append("symbol LIKE ?")
            args.append(f"%{search.upper()}%")
        if timeframe:
            where.append("timeframe = ?")
            args.append(timeframe)
        if grade:
            where.append("grade = ?")
            args.append(grade.upper())
        if side:
            where.append("side = ?")
            args.append(side.upper())
        if state:
            states = [s.strip().lower() for s in state.split(",") if s.strip()]
            if states:
                where.append("state IN (" + ", ".join("?" for _ in states) + ")")
                args.extend(states)
        if valid is not None:
            where.append("valid = ?")
            args.append(int(bool(valid)))
        if since:
            where.append("created_ms >= ?")
            args.append(int(since))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"SELECT payload FROM signals{clause} ORDER BY created_ms DESC LIMIT ?"
        args.append(max(1, min(50000, int(limit))))
        with self._lock:
            try:
                rows = self._db.execute(sql, tuple(args)).fetchall()
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return []
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                continue
            payload["stored"] = True
            out.append(payload)
        return out

    def all_payloads(self) -> list[dict]:
        """Read every archived payload for startup integrity auditing.

        The visible archive is deliberately paged; an integrity boundary is not.
        A malformed row must not escape quarantine because it happened to be row
        50,001 in a long-running database.
        """
        if not self.enabled or self._db is None:
            return []
        with self._lock:
            try:
                rows = self._db.execute("SELECT payload FROM signals ORDER BY created_ms DESC").fetchall()
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return []
        out: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                continue
            payload["stored"] = True
            out.append(payload)
        return out

    def export(self, limit: int = 5000) -> list[dict]:
        """The columns as flat rows - for the CSV, and for anyone auditing."""
        if not self.enabled or self._db is None:
            return []
        cols = [c for c in COLUMNS if c != "payload"]
        with self._lock:
            try:
                rows = self._db.execute(
                    f"SELECT {', '.join(cols)} FROM signals ORDER BY created_ms DESC LIMIT ?",
                    (max(1, min(50000, int(limit))),),
                ).fetchall()
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return []
        return [{k: row[k] for k in cols} for row in rows]

    def counts(self) -> dict[str, int]:
        """Rows by state, by grade and in total - what the archive actually holds."""
        if not self.enabled or self._db is None:
            return {}
        with self._lock:
            try:
                by_state = {row[0]: row[1] for row in self._db.execute(
                    "SELECT state, COUNT(*) FROM signals GROUP BY state")}
                by_grade = {row[0] or "?": row[1] for row in self._db.execute(
                    "SELECT grade, COUNT(*) FROM signals GROUP BY grade")}
                total = self._db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
                valid = self._db.execute("SELECT COUNT(*) FROM signals WHERE valid = 1").fetchone()[0]
                unmitigated = self._db.execute(
                    "SELECT COUNT(*) FROM signals WHERE valid = 1 AND mitigated = 0"
                ).fetchone()[0]
                current_version = self._db.execute(
                    "SELECT COUNT(*) FROM signals WHERE lifecycle_version = ? "
                    "AND chain_version = ? AND target_model = ?",
                    (CURRENT_LIFECYCLE_VERSION, CURRENT_CHAIN_VERSION, CURRENT_TARGET_MODEL),
                ).fetchone()[0]
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return {}
        return {
            "total": int(total),
            "valid": int(valid),
            "unmitigated": int(unmitigated),
            "current_lifecycle": int(current_version),
            "by_state": by_state,
            "by_grade": by_grade,
            "recorded": self.recorded,
            "updated": self.updated,
        }

    def results(self) -> dict[str, Any]:
        """The measured record of the archive: wins, losses and R per trade.

        Computed from the same rows the page shows, so the number on the board is
        the number in the database - not a separate calculation that can drift.
        """
        if not self.enabled or self._db is None:
            return {}
        with self._lock:
            try:
                rows = self._db.execute(
                    "SELECT state, grade, timeframe, side, session, r_now, entry, stop, target, "
                    "target_rr, target_model, mitigated, chain_version, lifecycle_version FROM signals "
                    "WHERE lifecycle_version = ? AND chain_version = ? AND target_model = ?",
                    (CURRENT_LIFECYCLE_VERSION, CURRENT_CHAIN_VERSION, CURRENT_TARGET_MODEL),
                ).fetchall()
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return {}

        def r_of(row: sqlite3.Row) -> float:
            """Risk units won or lost from the trade's actual frozen prices.

            Current-policy wins resolve at their own confirmation-leg H/L, so R is
            variable. Unfilled invalidations never enter the sample. ``r_now`` is
            authoritative; the price calculation is only for migrated v5 rows.
            """
            if row["state"] == "breakeven":
                return 0.0
            stored = row["r_now"] if "r_now" in row.keys() else None
            if stored is not None:
                return float(stored)
            if row["state"] == "win":
                entry, stop, target = row["entry"], row["stop"], row["target"]
                if entry is not None and stop is not None and target is not None:
                    risk = abs(float(entry) - float(stop))
                    move = (
                        float(entry) - float(target)
                        if str(row["side"]).upper() == "SHORT"
                        else float(target) - float(entry)
                    )
                    if risk > 0 and move > 0:
                        return move / risk
                return float(row["target_rr"] or 0.0)
            return -1.0

        def summarise(rows: list[sqlite3.Row]) -> dict[str, Any]:
            trades = [r for r in rows if bool(r["mitigated"])]
            wins = sum(1 for r in trades if r["state"] == "win")
            losses = sum(1 for r in trades if r["state"] == "stopped")
            scratches = sum(1 for r in trades if r["state"] == "breakeven")
            decided = wins + losses
            total_r = sum(r_of(r) for r in trades if r["state"] in FINAL_STATES)
            scored = [r_of(r) for r in trades if r["state"] in FINAL_STATES]
            plain = len(scored)
            mean = total_r / plain if plain else 0.0
            # The honest error bar. With ~30 trades a +0.3R average has a 95%
            # interval of roughly -0.3R..+0.9R, i.e. it could easily be nothing -
            # and a page that prints the average without this is lying by
            # omission.
            se = 0.0
            if plain > 1:
                var = sum((x - mean) ** 2 for x in scored) / (plain - 1)
                se = (var / plain) ** 0.5
            t_stat = (mean / se) if se else 0.0
            low, high = mean - 1.96 * se, mean + 1.96 * se
            return {
                "n": len(rows),
                "mitigated": len(trades),
                "wins": wins,
                "losses": losses,
                "decided": decided,
                "breakeven": scratches,
                "expired": sum(1 for r in rows if r["state"] == "expired"),
                "win_rate": round(wins / decided, 4) if decided else None,
                "r_per_trade": round(total_r / decided, 3) if decided else None,
                "scored": plain,
                "se": round(se, 4),
                "t": round(t_stat, 2),
                "ci_low": round(low, 3),
                "ci_high": round(high, 3),
                # one of: "not enough trades", "not proven" (interval crosses 0),
                # "positive" / "negative" (interval away from 0)
                "verdict": (
                    "not enough trades" if plain < 10 else
                    "not proven" if low <= 0.0 <= high else
                    "positive" if low > 0 else "negative"
                ),
            }

        out: dict[str, Any] = {
            "all": summarise(list(rows)),
            "r_basis": (
                "HVB/BOS confirmation-leg H/L frozen immediately before mitigation; "
                "invalidated/unfilled setups excluded"
            ),
            "chain_version": CURRENT_CHAIN_VERSION,
            "lifecycle_version": CURRENT_LIFECYCLE_VERSION,
            "target_model": CURRENT_TARGET_MODEL,
            "target_rr": None,
        }
        # every dimension the page filters on is also a dimension the scanner is
        # *judged* on: these are the buckets a trader would actually split by
        for dimension in ("grade", "timeframe", "side", "session"):
            buckets: dict[str, list] = {}
            for row in rows:
                buckets.setdefault(str(row[dimension] or "?"), []).append(row)
            out[f"by_{dimension}"] = {name: summarise(group)
                                      for name, group in sorted(buckets.items())}
        return out

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                try:
                    self._db.commit()
                    self._db.close()
                finally:
                    self._db = None
