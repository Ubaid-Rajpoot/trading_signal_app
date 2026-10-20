"""SQLite persistence for settings, fired signals and alert delivery log.

Kept deliberately dependency-free: one module, one file, WAL mode so the
scheduler thread and API workers do not fight over locks.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from ..config import DB_FILE, DEFAULT_TIMEFRAME, DEFAULT_WATCHLIST
from .strategies import DEFAULT_STRATEGY

_LOCK = threading.Lock()

DEFAULT_SETTINGS: dict[str, Any] = {
    "watchlist": list(DEFAULT_WATCHLIST),
    "timeframe": DEFAULT_TIMEFRAME,
    "strategy": DEFAULT_STRATEGY,
    "refresh_seconds": 60,
    "alerts": {
        "enabled": True,
        "min_confidence": 55,
        "telegram_enabled": False,
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "webhook_url": "",
    },
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                strategy TEXT NOT NULL,
                action TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                confidence INTEGER NOT NULL,
                bar_time INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                plan TEXT,
                checks TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                channel TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                created_at INTEGER NOT NULL
            );
            """
        )
        row = conn.execute("SELECT value FROM settings WHERE key='app'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('app', ?)",
                (json.dumps(DEFAULT_SETTINGS),),
            )


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def get_settings() -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='app'").fetchone()
    stored = json.loads(row["value"]) if row else {}
    merged = _deep_merge(DEFAULT_SETTINGS, stored)
    # Never leak the bot token through the API.
    token = merged["alerts"].get("telegram_bot_token") or ""
    merged["alerts"]["telegram_configured"] = bool(token and merged["alerts"].get("telegram_chat_id"))
    return merged


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    current = get_settings()
    patch = dict(patch or {})
    # A masked or empty token means "leave the stored secret alone".
    alerts_patch = dict(patch.get("alerts") or {})
    token = alerts_patch.get("telegram_bot_token")
    if token is not None and (token == "" or set(token) <= {"•"}):
        alerts_patch.pop("telegram_bot_token", None)
    if "alerts" in patch:
        # Always replace, so a stripped masked token can never be persisted.
        patch["alerts"] = alerts_patch

    merged = _deep_merge(current, patch)
    merged.pop("telegram_configured", None)
    if isinstance(merged.get("alerts"), dict):
        merged["alerts"].pop("telegram_configured", None)
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('app', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(merged),),
        )
    return get_settings()


def redacted_settings() -> dict[str, Any]:
    settings = get_settings()
    token = settings["alerts"].get("telegram_bot_token") or ""
    settings["alerts"]["telegram_bot_token"] = "••••••••" if token else ""
    return settings


def record_signal(signal: dict[str, Any]) -> int | None:
    """Store a signal, returning its id if new (None when it is a duplicate)."""
    side = signal.get("plan", {}).get("side") or signal.get("bias") or "NEUTRAL"
    dedupe = "|".join(
        [
            signal["symbol"],
            signal["timeframe"],
            signal["strategy"]["id"],
            str(signal["bar_time"]),
            side,
            signal["action"],
        ]
    )
    now = int(time.time() * 1000)
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO signals "
            "(dedupe_key, symbol, timeframe, strategy, action, side, price, confidence, "
            "bar_time, created_at, plan, checks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                dedupe,
                signal["symbol"],
                signal["timeframe"],
                signal["strategy"]["id"],
                signal["action"],
                side,
                float(signal["price"]),
                int(signal["confidence"]),
                int(signal["bar_time"]),
                now,
                json.dumps(signal.get("plan")),
                json.dumps(signal.get("checks")),
            ),
        )
        return int(cur.lastrowid) if cur.rowcount else None


def list_signals(limit: int = 100, symbol: str | None = None, strategy: str | None = None) -> list[dict]:
    query = "SELECT * FROM signals"
    clauses, args = [], []
    if symbol:
        clauses.append("symbol = ?")
        args.append(symbol.upper())
    if strategy:
        clauses.append("strategy = ?")
        args.append(strategy)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with _LOCK, _connect() as conn:
        rows = conn.execute(query, args).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["plan"] = json.loads(item["plan"]) if item.get("plan") else None
        item["checks"] = json.loads(item["checks"]) if item.get("checks") else []
        out.append(item)
    return out


def log_notification(signal_id: int | None, channel: str, status: str, detail: str = "") -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO notifications(signal_id, channel, status, detail, created_at) VALUES (?,?,?,?,?)",
            (signal_id, channel, status, detail[:500], int(time.time() * 1000)),
        )


def list_notifications(limit: int = 50) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications ORDER BY created_at DESC, id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [dict(r) for r in rows]


def signal_stats() -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM signals").fetchone()["c"]
        buys = conn.execute("SELECT COUNT(*) AS c FROM signals WHERE action IN ('BUY','SELL')").fetchone()["c"]
        last = conn.execute("SELECT MAX(created_at) AS t FROM signals").fetchone()["t"]
    return {"stored_signals": total, "actionable_signals": buys, "last_signal_at": last}
