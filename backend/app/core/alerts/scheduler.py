"""Background scanner.

Wakes up on a fixed cadence, evaluates the watchlist with the saved strategy,
persists every actionable signal exactly once per bar, and fans out alerts.
Runs in a daemon thread so it dies with the API process.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..signals import build_signal
from ..store import get_settings, record_signal
from .notifier import deliver

log = logging.getLogger("signalforge.scheduler")

_STATE: dict[str, Any] = {
    "running": False,
    "last_run": None,
    "last_duration_ms": None,
    "scans": 0,
    "signals_fired": 0,
    "alerts_sent": 0,
    "last_error": None,
}
_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()


def _scan_once() -> None:
    settings = get_settings()
    alerts = settings.get("alerts", {})
    if not alerts.get("enabled", True):
        return
    min_conf = int(alerts.get("min_confidence", 55))
    started = time.perf_counter()
    for symbol in settings.get("watchlist", []):
        try:
            signal = build_signal(
                symbol,
                settings.get("timeframe", "1h"),
                settings.get("strategy", "donchian"),
                limit=400,
            )
        except Exception as exc:  # a single bad symbol must not kill the loop
            _STATE["last_error"] = f"{symbol}: {exc}"
            log.warning("scan failed for %s: %s", symbol, exc)
            continue

        actionable = signal["action"] in ("BUY", "SELL") and signal["confidence"] >= min_conf
        if not actionable:
            continue
        signal_id = record_signal(signal)
        if signal_id is None:  # already stored for this bar
            continue
        _STATE["signals_fired"] += 1
        results = deliver(signal, signal_id)
        if any(r["status"] == "sent" for r in results):
            _STATE["alerts_sent"] += 1

    _STATE["last_run"] = int(time.time() * 1000)
    _STATE["last_duration_ms"] = int((time.perf_counter() - started) * 1000)
    _STATE["scans"] += 1


def _loop() -> None:
    while not _STOP.is_set():
        try:
            _scan_once()
        except Exception as exc:  # pragma: no cover
            _STATE["last_error"] = str(exc)
            log.exception("scheduler iteration failed")
        interval = max(20, int(get_settings().get("refresh_seconds", 60)))
        _STOP.wait(interval)


def start() -> None:
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="signalforge-scheduler", daemon=True)
        _THREAD.start()
        _STATE["running"] = True
        log.info("scheduler started")


def stop() -> None:
    _STOP.set()
    _STATE["running"] = False


def status() -> dict[str, Any]:
    return dict(_STATE)
