"""Every signal this app has ever produced, kept in a database.

The page is a *window* on the scanner: a zone lives in memory while it is active,
then it is mitigated or invalidated and - after a while - it is gone. The user's
complaint was exact: "signals come and then they vanish again". A signal that was
printed once should stay printable.

So every signal is written to SQLite the moment it is created, and the same row is
updated all the way through its life:

    formed  ->  waiting for price  ->  filled  ->  target | stopped
                                            \\->  mitigated / invalidated

Nothing is ever deleted. ``/api/journal`` reads it back, with filters and a CSV
export, and the page's *Journal* drawer shows the whole history including the
signals the live list has already dropped.

Design notes
------------
* stdlib ``sqlite3`` only - the app has no new dependency, and the file lives next
  to the candles (``data/journal.sqlite3``), which is already git-ignored.
* WAL + ``synchronous=NORMAL``: a push every few seconds must never fsync-stall
  the scanner.
* The full zone is *also* stored as JSON in ``snapshot`` so a row is readable on
  its own years later - the columns are for filtering, the snapshot is the record.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

# What "open" means, in one place (round 13): a plan that has not been decided by
# its own target or stop. Entering the block fills the plan - the trade is *live*
# from that moment - so ``mitigated`` with a filled plan is open, not closed.
OPEN_SQL = ("plan_state IN ('waiting','filled') AND COALESCE(result,'') NOT IN ('target','stopped') "
            "AND status <> 'interrupted'")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    key         TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    market      TEXT NOT NULL DEFAULT 'futures',
    timeframe   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    side        TEXT NOT NULL,
    direction   TEXT,
    grade       TEXT,
    prob        REAL,
    band_win    REAL,
    band_n      INTEGER,
    cond_met    INTEGER,
    cond_total  INTEGER,
    level       REAL,
    entry       REAL,
    stop        REAL,
    target      REAL,
    rr          REAL,
    risk_pct    REAL,
    fee_r       REAL,
    net_r       REAL,
    tradable    INTEGER DEFAULT 0,
    signal      INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'active',
    plan_state  TEXT DEFAULT 'waiting',
    result      TEXT,
    result_r    REAL,
    formed_at   INTEGER,
    first_seen  INTEGER,
    last_seen   INTEGER,
    closed_at   INTEGER,
    note        TEXT,
    snapshot    TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_formed ON signals(formed_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, timeframe);
CREATE INDEX IF NOT EXISTS idx_signals_signal ON signals(signal, formed_at DESC);
"""

# the columns a row is filtered/sorted by (the rest lives in ``snapshot``)
FIELDS = (
    "key", "symbol", "market", "timeframe", "kind", "side", "direction", "grade",
    "prob", "band_win", "band_n", "cond_met", "cond_total", "level", "entry",
    "stop", "target", "rr", "risk_pct", "fee_r", "net_r", "tradable", "signal",
    "status", "plan_state", "result", "result_r", "formed_at", "first_seen",
    "last_seen", "closed_at", "note",
)

# A database written by an older build is upgraded in place - the archive is the
# one thing in this app that must never be thrown away to add a column.
MIGRATIONS = (("note", "ALTER TABLE signals ADD COLUMN note TEXT"),)


# facts that are settled once and must never be blanked by a later update
FINAL_FIELDS = ("result", "result_r", "closed_at")


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None if value is None else float(value)
        return float(value)
    except (TypeError, ValueError):
        return None


def _row(zone: dict, now: int) -> dict[str, Any]:
    """One zone -> one journal row. Everything a trader needs is a column."""
    plan = zone.get("plan") or {}
    measured = zone.get("measured") or {}
    side = str(zone.get("side") or "")
    return {
        "key": str(zone.get("key")),
        "symbol": str(zone.get("symbol") or ""),
        "market": str(zone.get("market") or "futures"),
        "timeframe": str(zone.get("timeframe") or ""),
        "kind": str(zone.get("kind") or ""),
        "side": side,
        "direction": "SHORT" if side == "high" else "LONG" if side == "low" else None,
        "grade": zone.get("grade"),
        "prob": _num(zone.get("prob")),
        "band_win": _num(measured.get("win")),
        "band_n": int(measured.get("n") or 0) or None,
        "cond_met": int(zone.get("cond_met") or 0),
        "cond_total": int(zone.get("cond_total") or 0),
        "level": _num(zone.get("level")),
        "entry": _num(plan.get("entry")),
        "stop": _num(plan.get("stop")),
        "target": _num(plan.get("target")),
        "rr": _num(plan.get("rr")),
        "risk_pct": _num(plan.get("risk_pct")),
        "fee_r": _num(zone.get("fee_r")),
        "net_r": _num(zone.get("net_r")),
        "tradable": 1 if zone.get("tradable") else 0,
        "signal": 1 if zone.get("signal") else 0,
        "status": str(zone.get("status") or "active"),
        "plan_state": str(zone.get("plan_state") or "waiting"),
        "result": zone.get("result"),
        "result_r": _num(zone.get("plan_result_r")),
        "formed_at": int(zone.get("time") or 0),
        "first_seen": now,
        "last_seen": now,
        "closed_at": int(zone.get("mitigated_at") or zone.get("invalidated_at") or 0) or None,
        "note": zone.get("note"),
        "snapshot": json.dumps(zone, default=str)[:20000],
    }


class SignalJournal:
    """A thin, thread-safe wrapper over one SQLite file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.problem: str | None = None
        self.enabled = path is not None
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self.recorded = 0
        self.updated = 0
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
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
        """Add the columns a newer build needs, without touching a single row."""
        assert self._db is not None
        have = {row[1] for row in self._db.execute("PRAGMA table_info(signals)")}
        for name, ddl in MIGRATIONS:
            if name not in have:
                self._db.execute(ddl)

    # ------------------------------------------------------------ writing
    def record(self, zone: dict, now: int | None = None) -> bool:
        """Insert a zone once, then keep its live fields up to date."""
        if not self.enabled or not zone.get("key"):
            return False
        row = _row(zone, int(now or time.time() * 1000))
        with self._lock:
            assert self._db is not None
            try:
                exists = self._db.execute(
                    "SELECT first_seen FROM signals WHERE key = ?", (row["key"],)
                ).fetchone()
                if exists:
                    updates = {k: v for k, v in row.items() if k not in ("first_seen", "snapshot")}
                    # A finished fact is never overwritten by a live one. The zone
                    # that comes back here still carries ``result: None`` until the
                    # plan resolves - so a plain UPDATE would erase "target, +1.6 R"
                    # on the very next push. That is exactly how a signal "loses its
                    # details". These three are write-once.
                    sets = ", ".join(
                        f"{k} = COALESCE(?, {k})" if k in FINAL_FIELDS else f"{k} = ?"
                        for k in updates
                    )
                    self._db.execute(
                        f"UPDATE signals SET {sets} WHERE key = ?",
                        (*updates.values(), row["key"]),
                    )
                    self.updated += 1
                else:
                    cols = ", ".join(FIELDS) + ", snapshot"
                    marks = ", ".join("?" for _ in range(len(FIELDS) + 1))
                    self._db.execute(
                        f"INSERT OR REPLACE INTO signals ({cols}) VALUES ({marks})",
                        tuple(row[k] for k in FIELDS) + (row["snapshot"],),
                    )
                    self.recorded += 1
                self._db.commit()
                return True
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return False

    def close_signal(self, key: str, status: str, at: int | None = None,
                     price: float | None = None, result: str | None = None,
                     result_r: float | None = None, note: str | None = None) -> bool:
        """Mark a signal finished. It stays in the database forever."""
        if not self.enabled:
            return False
        with self._lock:
            assert self._db is not None
            try:
                self._db.execute(
                    "UPDATE signals SET status = ?, closed_at = COALESCE(?, closed_at),"
                    # A *decided* outcome is final: a later run that replays the same
                    # zone, or a second event for the same key, must not rewrite
                    # "target +1.6 R" into "stopped -1 R". Before a decision, the
                    # row still follows the trade (mitigated -> target is normal).
                    " result = CASE WHEN result IN ('target','stopped') THEN result"
                    "               ELSE COALESCE(?, result) END,"
                    " result_r = CASE WHEN result IN ('target','stopped') THEN result_r"
                    "                 ELSE COALESCE(?, result_r) END,"
                    " plan_state = CASE WHEN ? IN ('target','stopped') THEN ? ELSE plan_state END,"
                    " note = COALESCE(?, note)"
                    " WHERE key = ?",
                    (status, at, result, result_r, result, result, note, key),
                )
                self._db.commit()
                self.updated += 1
                return True
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return False

    def reap(self, now: int | None = None, max_age_hours: float = 6.0) -> int:
        """Close the plans a *previous* run left open - and keep every one of them.

        A live restart throws the in-memory map away: the positions the scanner was
        following no longer exist, but their rows still said "active", so the page's
        "open now" grew by a dozen every restart and the drawer filled with signals
        that were never going to resolve. Reaping marks exactly those rows
        ``interrupted`` with a note, and never deletes them. Rows that are still
        being updated (``last_seen`` inside the window) are untouched.
        """
        if not self.enabled:
            return 0
        now = int(now if now is not None else time.time() * 1000)
        cutoff = now - int(max(0.0, float(max_age_hours)) * 3_600_000)
        with self._lock:
            assert self._db is not None
            try:
                cursor = self._db.execute(
                    "UPDATE signals SET status = 'interrupted',"
                    " note = COALESCE(note, 'the scanner restarted while this plan was open')"
                    " WHERE status = 'active' AND COALESCE(last_seen, formed_at, 0) < ?",
                    (cutoff,),
                )
                self._db.commit()
                return int(cursor.rowcount or 0)
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return 0

    # ------------------------------------------------------------ reading
    def list(
        self,
        limit: int = 200,
        symbol: str = "",
        timeframe: str = "",
        grade: str = "",
        status: str = "",
        min_prob: float = 0.0,
        signals_only: bool = False,
        since: int = 0,
        order: str = "formed",
        offset: int = 0,
    ) -> list[dict]:
        if not self.enabled:
            return []
        where, params = ["1=1"], []
        if symbol:
            where.append("symbol LIKE ?")
            params.append(f"%{symbol.upper()}%")
        if timeframe:
            where.append("timeframe = ?")
            params.append(timeframe)
        if grade:
            wanted = [g.strip().upper() for g in grade.split(",") if g.strip()]
            where.append("grade IN (%s)" % ",".join("?" for _ in wanted))
            params += wanted
        if status:
            if status == "open":
                where.append(OPEN_SQL)
            elif status == "closed":
                where.append(f"NOT ({OPEN_SQL})")
            else:
                where.append("status = ?")
                params.append(status)
        if min_prob:
            where.append("COALESCE(prob, 0) >= ?")
            params.append(float(min_prob))
        if signals_only:
            where.append("signal = 1")
        if since:
            where.append("formed_at >= ?")
            params.append(int(since))
        column = {"closed": "closed_at", "seen": "last_seen"}.get(order, "formed_at")
        sql = (
            f"SELECT {', '.join(FIELDS)} FROM signals WHERE {' AND '.join(where)}"
            f" ORDER BY {column} DESC, prob DESC LIMIT ? OFFSET ?"
        )
        params.append(max(1, min(int(limit), 5000)))
        params.append(max(0, int(offset)))
        with self._lock:
            assert self._db is not None
            try:
                return [dict(row) for row in self._db.execute(sql, params).fetchall()]
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return []

    def open_snapshots(self, hours: float = 26.0, limit: int = 400) -> list[dict]:
        """The plans that were still open when the scanner stopped - to adopt them.

        Round 13: a restart used to *interrupt* every open plan (kept in the table,
        but no longer followed), so the page came back with an empty live list even
        though the trades were still live on the exchange. This returns the stored
        zone snapshots for the rows that are still open, so the scanner can put
        them back on its map and go on following them.
        """
        if not self.enabled:
            return []
        since = int((time.time() - max(0.0, hours) * 3600.0) * 1000)
        sql = (
            f"SELECT snapshot FROM signals WHERE {OPEN_SQL} "
            "AND COALESCE(last_seen, formed_at) >= ? "
            "ORDER BY formed_at DESC LIMIT ?"
        )
        out: list[dict] = []
        with self._lock:
            assert self._db is not None
            try:
                for row in self._db.execute(sql, (since, max(1, int(limit)))).fetchall():
                    try:
                        item = json.loads(row["snapshot"] or "{}")
                    except Exception:
                        continue
                    if isinstance(item, dict) and item.get("key"):
                        out.append(item)
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
        return out

    def count(self, **kwargs: Any) -> int:
        """How many rows the same filters match - a page needs to know its size."""
        kwargs.pop("limit", None)
        kwargs.pop("offset", None)
        kwargs.pop("order", None)
        if not self.enabled:
            return 0
        where, params = ["1=1"], []
        symbol = str(kwargs.get("symbol") or "")
        if symbol:
            where.append("symbol LIKE ?")
            params.append(f"%{symbol.upper()}%")
        timeframe = str(kwargs.get("timeframe") or "")
        if timeframe:
            where.append("timeframe = ?")
            params.append(timeframe)
        status = str(kwargs.get("status") or "")
        if status:
            # Round 13: "open" and "closed" were silently ignored here, so the total
            # printed next to an "open now" list was the size of the whole archive.
            if status == "open":
                where.append(OPEN_SQL)
            elif status == "closed":
                where.append(f"NOT ({OPEN_SQL})")
            else:
                where.append("status = ?")
                params.append(status)
        if kwargs.get("signals_only"):
            where.append("signal = 1")
        grade = str(kwargs.get("grade") or "")
        if grade:
            wanted = [g.strip().upper() for g in grade.split(",") if g.strip()]
            where.append("grade IN (%s)" % ",".join("?" for _ in wanted))
            params += wanted
        with self._lock:
            assert self._db is not None
            try:
                return int(self._db.execute(
                    f"SELECT COUNT(*) FROM signals WHERE {' AND '.join(where)}", params
                ).fetchone()[0])
            except Exception as exc:
                self.problem = f"{type(exc).__name__}: {exc}"
                return 0

    def get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        with self._lock:
            assert self._db is not None
            row = self._db.execute("SELECT * FROM signals WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        try:
            out["snapshot"] = json.loads(out.get("snapshot") or "{}")
        except Exception:
            pass
        return out

    def stats(self) -> dict[str, Any]:
        """Counts for the page: never a guess, always from the table."""
        out: dict[str, Any] = {"enabled": self.enabled, "problem": self.problem,
                               "path": str(self.path) if self.path else None,
                               "recorded": 0, "updated": self.updated}
        if not self.enabled:
            return out
        with self._lock:
            assert self._db is not None
            try:
                total = self._db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
                signals = self._db.execute("SELECT COUNT(*) FROM signals WHERE signal = 1").fetchone()[0]
                open_ = self._db.execute(
                    "SELECT COUNT(*) FROM signals WHERE status = 'active'"
                ).fetchone()[0]
                decided = self._db.execute(
                    "SELECT COUNT(*) FROM signals WHERE result IN ('target','stopped')"
                ).fetchone()[0]
                wins = self._db.execute(
                    "SELECT COUNT(*) FROM signals WHERE result = 'target'"
                ).fetchone()[0]
                net = self._db.execute(
                    "SELECT AVG(result_r) FROM signals WHERE result IN ('target','stopped')"
                ).fetchone()[0]
                # The same numbers for the *signals only*. "Hit rate" over every
                # graded row answers a different question (the C band is archived
                # too) and reading it as the scoreboard overstates or understates
                # what the page actually told anyone to trade.
                decided_s = self._db.execute(
                    "SELECT COUNT(*) FROM signals WHERE signal = 1 AND result IN ('target','stopped')"
                ).fetchone()[0]
                wins_s = self._db.execute(
                    "SELECT COUNT(*) FROM signals WHERE signal = 1 AND result = 'target'"
                ).fetchone()[0]
                avg_s = self._db.execute(
                    "SELECT AVG(result_r) FROM signals WHERE signal = 1 AND result IN ('target','stopped')"
                ).fetchone()[0]
                last = self._db.execute("SELECT MAX(formed_at) FROM signals").fetchone()[0]
                # "the last 24 h" is anchored on the newest signal, not on the wall
                # clock: in a replay the newest signal is recorded market time, and
                # a window measured from *now* would always read zero
                anchor = int(last or time.time() * 1000)
                day = anchor - 86_400_000
                recent = self._db.execute(
                    "SELECT COUNT(*) FROM signals WHERE signal = 1 AND formed_at >= ?", (day,)
                ).fetchone()[0]
                interrupted = self._db.execute(
                    "SELECT COUNT(*) FROM signals WHERE status = 'interrupted'"
                ).fetchone()[0]
                by_grade = {
                    str(row[0]): int(row[1])
                    for row in self._db.execute(
                        "SELECT grade, COUNT(*) FROM signals WHERE signal = 1 GROUP BY grade"
                    ).fetchall()
                }
            except Exception as exc:
                out["problem"] = f"{type(exc).__name__}: {exc}"
                return out
        out.update(
            {
                "recorded": int(total),
                "signals": int(signals),
                "open": int(open_),
                "decided": int(decided),
                "wins": int(wins),
                "hit_rate": (wins / decided) if decided else None,
                "avg_r": net,
                "signal_decided": int(decided_s),
                "signal_wins": int(wins_s),
                "signal_hit_rate": (wins_s / decided_s) if decided_s else None,
                "signal_avg_r": avg_s,
                "last_formed_at": last,
                "signals_last_24h": int(recent),
                "interrupted": int(interrupted),
                "by_grade": by_grade,
            }
        )
        return out

    def csv(self, **kwargs: Any) -> str:
        """The journal as CSV - the user's own file, outside this app."""
        rows = self.list(**kwargs)
        head = ("formed_at", "symbol", "timeframe", "kind", "direction", "grade", "prob",
                "band_win", "cond_met", "cond_total", "entry", "stop", "target", "rr",
                "risk_pct", "fee_r", "net_r", "status", "plan_state", "result", "result_r")
        lines = [",".join(head)]
        for row in rows:
            lines.append(",".join("" if row.get(k) is None else str(row.get(k)) for k in head))
        return "\n".join(lines) + "\n"

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
        self.enabled = False


_JOURNAL: SignalJournal | None = None


def open_journal(path: str | Path | None) -> SignalJournal:
    """One journal per process, opened where the candles live."""
    global _JOURNAL
    if _JOURNAL is not None:
        _JOURNAL.close()
    _JOURNAL = SignalJournal(path)
    return _JOURNAL


def journal() -> SignalJournal:
    global _JOURNAL
    if _JOURNAL is None:
        _JOURNAL = SignalJournal(None)   # disabled until the app says where the file is
    return _JOURNAL
