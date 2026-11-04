"""HTTP + WebSocket server: the page, the state, the live push, and the controls.

Everything the page can change (instruments, filters) goes through here, is
applied to the running engine immediately, and is written back to
``config.json`` so it survives a restart. The exchange universe (every MEXC
symbol and its turnover) is fetched **server side** and cached - the browser
never calls an exchange directly.
"""

from __future__ import annotations

import asyncio
import os
import contextlib
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from . import config_store
from .notify import NotificationBus
from .store import SignalStore
from .engine import Engine

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


class Hub:
    """Keeps the connected browsers and pushes every update to all of them."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(ws)

    async def send_all(self, message: dict) -> None:
        if not self.clients:
            return
        payload = json.dumps(message, default=str)
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.unregister(ws)


def create_app(
    config: dict[str, Any],
    feed,
    engine: Engine,
    config_path: str | Path | None = None,
) -> FastAPI:
    hub = Hub()
    # every message the engine produces passes through the notification policy,
    # so the page gets one quiet, deduplicated stream instead of a toast per event
    notifier = NotificationBus(
        dedupe_window=float(config.get("notify", {}).get("dedupe_seconds", 900)),
        coalesce_window=float(config.get("notify", {}).get("coalesce_seconds", 120)),
        toast_window=float(config.get("notify", {}).get("toast_seconds", 60)),
        toast_burst=int(config.get("notify", {}).get("toast_burst", 2)),
    )

    async def publish(message: dict[str, Any]) -> None:
        """Route one engine message: state and chat straight out, the rest via policy."""
        kind = str(message.get("type") or "")
        if kind == "state":
            # the table is already ~90 kB: the alert list inside it is noise that
            # the page never renders any more (the inbox has its own endpoint)
            lean = {key: value for key, value in message.items() if key != "alerts"}
            await hub.send_all(lean)
            return
        if kind == "notify":
            await hub.send_all(message)
            return
        for envelope in notifier.absorb(message):
            await hub.send_all({"type": "notify", **envelope})

    engine.broadcast = publish
    engine.source = getattr(feed, "source", "live")
    engine.exchange = getattr(feed, "name", "binance")
    max_symbols = int(config.get("max_symbols", 2000))
    workers = max(1, int(config.get("workers", 30)))
    db_override = os.environ.get("TDIGM_DB")
    db_path = db_override or config.get("database") or "data/tdigm.sqlite3"
    # A config-relative database is stable whether run.py is started from the
    # repository root, tdigm-live/, systemd, or a container working directory.
    if not db_override and config_path and not Path(str(db_path)).is_absolute():
        db_path = str(Path(config_path).resolve().parent / str(db_path))
    store = SignalStore(None if config.get("database") == "" else db_path)
    engine.attach_store(store)
    restored = engine.load_stored()
    auto_universe = bool(config.get("auto_universe"))
    fallback_symbols = list(engine.symbols)
    universe_min_expected = max(1, int(config.get("universe_min_expected", 100)))
    universe_retry_seconds = max(10.0, float(config.get("universe_retry_seconds", 60)))
    universe_refresh_seconds = max(300.0, float(config.get("universe_refresh_seconds", 3600)))
    universe_cache: dict[str, Any] = {
        "at": 0.0,
        "last_attempt": 0.0,
        "items": [],
        "error": None,
        "attempts": 0,
        "successes": 0,
        "mode": "configured_fallback" if auto_universe else "manual",
        "selected": len(engine.symbols),
        "fallback_count": len(fallback_symbols),
    }
    universe_lock = asyncio.Lock()
    tasks: list[asyncio.Task] = []
    # (symbol, depth, force): shallow jobs for every new contract stay ahead of
    # their deep jobs, so a 700-contract add does not warm only the first 24.
    warm_queue: asyncio.Queue[tuple[str, int, bool]] = asyncio.Queue()
    startup_history_bars = max(
        120, min(engine.history_bars, int(config.get("startup_history_bars", 1000)))
    )
    if engine.strategy == "book":
        # The narrative engine owns one separate deep 1m replay; running it in
        # both phases would duplicate the same expensive work.
        startup_history_bars = engine.history_bars
    history_days = float(config.get("history_days", 0) or 0)
    rescan_state: dict[str, Any] = {"running": False, "result": None}
    chart_cache: dict[tuple[str, str, int, int], tuple[float, list[dict]]] = {}
    warm_pending: set[str] = set()

    def queue_warm(symbols: list[str]) -> None:
        """Warm newly added instruments one at a time.

        Adding 300 symbols must not fire 1,500 REST calls at once - the exchange
        would rate-limit us and every row would look broken. They warm in the
        background and show 'warming' until they are ready.
        """
        engine.prepare_warmup(symbols)
        for symbol in symbols:
            if symbol in warm_pending:
                continue
            warm_pending.add(symbol)
            warm_queue.put_nowait((symbol, startup_history_bars, False))

    async def warmer() -> None:
        """One of ``workers`` consumers of the warm-up queue.

        A single consumer meant every instrument the user added waited behind the
        one before it - 500 additions was a 500-step queue. Several consumers
        pull from the same queue, so an add is picked up within one turn and no
        instrument can starve the ones behind it: they all wait in the same FIFO.
        """
        while True:
            symbol, depth, force = await warm_queue.get()
            queue_deep = depth < engine.history_bars
            try:
                await engine.warm_symbol(
                    feed, symbol, force=force, history_bars=depth
                )
            except Exception:
                pass
            finally:
                warm_queue.task_done()
            if queue_deep and symbol in engine.symbols:
                # Appended behind all already queued shallow work: breadth first.
                warm_queue.put_nowait((symbol, engine.history_bars, True))
            else:
                warm_pending.discard(symbol)

    async def pump() -> None:
        """Read candles from the feed and hand them to the engine, forever."""
        try:
            async for candle in feed.candles():
                await engine.on_candle(candle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the server alive, surface the problem
            await publish({"type": "log", "level": "error", "message": f"feed stopped: {exc}"})
            raise

    async def setup_pusher() -> None:
        """State changes (a pending setup fills, wins, or dies) go out at once."""
        while True:
            await asyncio.sleep(0.5)
            while engine.setup_events:
                payload = engine.setup_events.pop(0)
                await publish({"type": "setup", "setup": payload})

    async def retry_sweeper() -> None:
        """Every minute, try again for any instrument whose history never came.

        Without this, one rate-limited minute at start-up meant that instrument
        was invisible for the whole session - which is how 500 symbols can sit
        there producing nothing.
        """
        while True:
            await asyncio.sleep(60)
            try:
                retried = await engine.retry_failed(feed)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            if retried:
                await engine._emit_state(force=True)  # noqa: SLF001

    async def universe_refresher() -> None:
        """Retry failed/partial discovery and periodically catch new listings."""
        if not auto_universe or not hasattr(feed, "universe"):
            return
        while True:
            delay = (
                universe_refresh_seconds
                if universe_cache.get("mode") == "discovered" and not universe_cache.get("error")
                else universe_retry_seconds
            )
            await asyncio.sleep(delay)
            try:
                await _refresh_auto_universe(initial=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # `_refresh_auto_universe` normally records this itself; this is
                # the final guard that keeps a refresh bug from killing retries.
                universe_cache["error"] = f"{type(exc).__name__}: {exc}"

    async def boot() -> None:
        """Warm in the background, then start reading candles.

        Warming used to run *before* uvicorn accepted a single connection, so a
        big universe meant the page looked dead (or "not running") for minutes.
        Now the server is up instantly, the page shows every cell as warming,
        and rows fill in as their history lands.
        """
        if auto_universe and hasattr(feed, "universe"):
            try:
                await _refresh_auto_universe(initial=True)
            except Exception as exc:
                universe_cache["error"] = f"{type(exc).__name__}: {exc}"
                await publish(
                    {
                        "type": "log",
                        "level": "warn",
                        "message": f"auto-universe apply failed; keeping fallback: {exc}",
                    }
                )
        # Subscribe immediately.  Closed bars are buffered per symbol by the
        # engine while that symbol's REST history is rebuilt, then drained in
        # order.  A 700-contract deep scan therefore never creates a blind live
        # window lasting several minutes.
        engine.prepare_warmup(list(engine.symbols))
        pump_task = asyncio.create_task(pump())
        try:
            try:
                # First make every contract/timeframe usable. Only then spend
                # requests on the full 5,000-bar historical replay. This keeps
                # low-ranked contracts from sitting blind behind the first batch.
                await engine.warmup(
                    feed, concurrency=workers, history_bars=startup_history_bars
                )
                if startup_history_bars < engine.history_bars:
                    await publish(
                        {
                            "type": "log",
                            "level": "info",
                            "message": (
                                f"fast warm-up complete; deep replaying {engine.history_bars} bars "
                                f"across {len(engine.symbols)} contracts"
                            ),
                        }
                    )
                    await engine.rescan(
                        feed, concurrency=workers, history_bars=engine.history_bars
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never take the page down over warm-up
                await publish(
                    {"type": "log", "level": "error", "message": f"warm-up failed: {exc}"}
                )
            await pump_task
        finally:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump_task

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks.append(asyncio.create_task(boot()))
        for _ in range(workers):
            tasks.append(asyncio.create_task(warmer()))
        tasks.append(asyncio.create_task(setup_pusher()))
        tasks.append(asyncio.create_task(retry_sweeper()))
        tasks.append(asyncio.create_task(universe_refresher()))
        try:
            # a first table immediately, so the browser paints something at once
            await engine._emit_state(force=True)  # noqa: SLF001 - same module boundary
            yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            tasks.clear()
            if hasattr(feed, "close"):
                with contextlib.suppress(Exception):
                    await feed.close()

    app = FastAPI(title="TDIGM liquidity scanner", lifespan=lifespan)
    # the table is ~90 kB of JSON and the page asks for it every few seconds
    # when it has to fall back to HTTP; over a proxy that is worth compressing
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # ------------------------------------------------------------------ page
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/app.js")
    async def app_js() -> FileResponse:
        return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")

    @app.get("/app.css")
    async def app_css() -> FileResponse:
        return FileResponse(WEB_DIR / "app.css", media_type="text/css")

    @app.get("/notify.js")
    async def notify_js() -> FileResponse:
        return FileResponse(WEB_DIR / "notify.js", media_type="text/javascript")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        store_counts = store.counts()
        return {
            "ok": True,
            "source": engine.source,
            "exchange": engine.exchange,
            "feed": getattr(feed, "status", "unknown"),
            "connected": getattr(feed, "connected", None),
            "frames": getattr(feed, "frames", 0),
            "clients": len(hub.clients),
            "warming": engine.warming(),
            "failed": len(engine.warm_failures),
            "coverage": {**engine.coverage(), "queued_symbols": warm_queue.qsize()},
            "decision_funnel": engine.decision_funnel(),
            "universe": {
                "auto": auto_universe,
                "mode": universe_cache["mode"],
                "available": len(universe_cache["items"]),
                "selected": len(engine.symbols),
                "fallback_count": universe_cache["fallback_count"],
                "attempts": universe_cache["attempts"],
                "successes": universe_cache["successes"],
                "fetched_at": universe_cache["at"],
                "last_attempt": universe_cache["last_attempt"],
                "error": universe_cache["error"],
                "diagnostics": getattr(feed, "universe_diagnostics", None),
            },
            "market": str(config.get("market", "futures")),
            "setups": len(engine.setup_payloads),
            "stored": store_counts.get("total", 0),
            "store": store.enabled,
            "workers": workers,
            "rescanning": bool(rescan_state.get("running")),
            "setups_valid": store_counts.get(
                "valid", len(engine.open_setups(valid_only=True, limit=10_000))
            ),
            "setups_unmitigated": store_counts.get(
                "unmitigated",
                sum(1 for row in engine.setup_payloads.values() if row.get("valid") and not row.get("mitigated")),
            ),
            "candles_seen": engine.candles_seen,
            "signals": engine.kinds.get("signal", 0),
            "integrity": {
                "checked": engine.integrity_checked,
                "quarantined": engine.integrity_quarantined,
            },
            "symbols": len(engine.symbols),
            "timeframes": engine.timeframes,
            "notifications": notifier.counts(),
        }

    @app.get("/api/state")
    async def state() -> JSONResponse:
        body = engine.snapshot()
        # the page must be able to say "the bot is fine, the exchange is not"
        body["feed"] = {
            "status": getattr(feed, "status", ""),
            "connected": getattr(feed, "connected", None),
            "source": getattr(feed, "source", "live"),
            "market": getattr(feed, "market", "futures"),
            "strategy": engine.strategy,
            "name": getattr(feed, "name", ""),
        }
        # the market the bot is configured for; `feed.market` may be "replay"
        body["market"] = str(config.get("market", "futures"))
        # never cache: a stale table looks exactly like a frozen bot
        return JSONResponse(
            body,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/api/notifications")
    async def notifications(since: int = 0, limit: int = 120) -> dict[str, Any]:
        """The inbox: grouped, deduplicated, already filtered by policy."""
        return {
            "events": notifier.list(since=since, limit=max(1, min(400, limit))),
            "counts": notifier.counts(),
        }

    @app.post("/api/notifications/read")
    async def notifications_read() -> dict[str, Any]:
        cleared = notifier.mark_read()
        return {"ok": True, "cleared": cleared, "counts": notifier.counts()}

    @app.get("/api/setups")
    async def setups(valid: int = 0, limit: int = 300) -> dict[str, Any]:
        """Every setup the engine knows about, newest first, with its state."""
        rows = engine.open_setups(valid_only=bool(valid), limit=max(1, min(1000, limit)))
        return {
            "setups": rows,
            "counts": engine.setup_counts(),
            "total": len(engine.setup_payloads),
            "market": getattr(feed, "market", "futures"),
            "strategy": engine.strategy,
        }

    @app.get("/api/integrity")
    async def integrity(limit: int = 200) -> dict[str, Any]:
        """Archive-chain audit: malformed rows are retained but never tradeable."""
        rows = [
            {"key": key, "errors": errors}
            for key, errors in list(engine.integrity_problems.items())[:max(1, min(1000, limit))]
        ]
        return {
            "checked": engine.integrity_checked,
            "quarantined": engine.integrity_quarantined,
            "problems": rows,
        }

    @app.get("/api/watch")
    async def watch(limit: int = 40) -> dict[str, Any]:
        """What the book is waiting for, so an empty table is explainable."""
        rows = engine.watching(limit=max(1, min(200, limit)))
        if engine.book is not None:
            funnel = {
                "mode": "book",
                "traps": engine.book.traps,
                "displacements": engine.book.armed,
                "plans": len(engine.book.plans),
                "drops": engine.book.drops(),
            }
        else:
            decisions = engine.decision_funnel()
            funnel = {
                "mode": "sweep",
                "sweeps": sum(cell.structure.sweep_count for cell in engine.cells.values()),
                "setups": decisions["setups"],
                "hvb_aligned": decisions["hvb_aligned"],
                "filtered": decisions["filtered"],
                "plans": decisions["passed"],
                "drops": dict(engine.filters.rejections),
            }
        return {
            "watching": rows,
            "funnel": funnel,
            "strategy": engine.strategy,
            "coverage": engine.coverage(),
        }

    @app.get("/api/signals")
    async def signals(
        limit: int = 300,
        symbol: str = "",
        timeframe: str = "",
        grade: str = "",
        state: str = "",
        side: str = "",
        valid: int | None = None,
        since: int = 0,
        search: str = "",
    ) -> dict[str, Any]:
        """The archive: every setup ever recorded, newest first.

        This is what makes the page survive a restart - it is read from SQLite, so
        it does not depend on any process still being up.
        """
        rows = store.list(
            limit=limit,
            symbol=symbol,
            timeframe=timeframe,
            grade=grade,
            state=state,
            side=side,
            valid=valid,
            since=since,
            search=search,
        )
        return {
            "signals": rows,
            "count": len(rows),
            "counts": store.counts(),
            "results": store.results(),
            "enabled": store.enabled,
            "problem": store.problem,
            "path": str(store.path) if store.path else None,
        }

    @app.get("/api/signals.csv")
    async def signals_csv(limit: int = 5000) -> Any:
        """The same rows as a CSV, because an archive you cannot take out is a trap."""
        import csv as _csv
        import io

        rows = store.export(limit=limit)
        buffer = io.StringIO()
        fields = ["created_ms", "symbol", "market", "timeframe", "side", "grade", "state",
                  "valid", "mitigated", "entry", "stop", "target", "target_rr", "target_rr_price", "rr",
                  "target_model", "leg_target", "leg_target_ms", "leg_start_ms", "leg_break_ms",
                  "approach_model", "approach_start_ms", "approach_end_ms", "approach_bars",
                  "approach_pivot_count", "hvb_model", "hvb_prev_fast", "hvb_prev_centre",
                  "hvb_fast", "hvb_centre",
                  "score", "r_now", "zone_low", "zone_high", "mitigation_kind", "state_reason",
                  "lifecycle_version", "swept_level", "bos_level",
                  "break_kind", "hvb_side", "hvb_lag", "volume_ratio", "trend_dir",
                  "closed_ms", "evidence"]
        writer = _csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        from fastapi.responses import Response

        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="tdigm-signals.csv"'},
        )

    @app.get("/api/chart")
    async def chart(
        symbol: str,
        timeframe: str,
        bars: int = 600,
        key: str = "",
        at: int = 0,
        refresh: int = 0,
        live: int = 0,
    ) -> JSONResponse:
        """TradingView-style setup audit or current, still-forming market view.

        Setup mode anchors exchange history around the event so its proof cannot
        disappear. ``live=1`` bypasses that immutable window and returns the
        in-memory tail plus the exchange's forming candle and mark-to-market R.
        """
        symbol = symbol.upper()
        live_mode = bool(live)
        if live_mode:
            selected = engine.setup_payloads.get(key) or (store.get(key) if key else None)
        else:
            selected = (store.get(key) if key else None) or engine.setup_payloads.get(key)
        if selected and (
            str(selected.get("symbol") or "").upper() != symbol
            or str(selected.get("timeframe") or "") != timeframe
        ):
            selected = None
        focus = int((selected or {}).get("created_ms") or at or 0)
        wanted = max(80, min(1000, int(bars)))
        override: list[dict] | None = None
        history_error: str | None = None
        if focus and not live_mode:
            cache_key = (symbol, timeframe, focus, wanted)
            cached = chart_cache.get(cache_key)
            if cached and not refresh and time.time() - cached[0] < 300:
                override = cached[1]
            else:
                try:
                    fetched = await engine.chart_history(
                        feed, symbol, timeframe, bars=wanted, at=focus
                    )
                    if fetched:
                        override = fetched
                        chart_cache[cache_key] = (time.time(), fetched)
                        if len(chart_cache) > 200:
                            oldest = min(chart_cache, key=lambda item: chart_cache[item][0])
                            chart_cache.pop(oldest, None)
                except Exception as exc:
                    history_error = f"{type(exc).__name__}: {exc}"
        body = engine.chart(
            symbol,
            timeframe,
            wanted,
            candles_override=override,
            selected=selected,
            live=live_mode,
        )
        body["history_error"] = history_error
        body["historical_window"] = override is not None
        body["server_time_ms"] = int(time.time() * 1000)
        live_body = body.get("live") or {}
        updated_ms = int(live_body.get("updated_ms") or 0)
        live_body["age_ms"] = max(0, body["server_time_ms"] - updated_ms) if updated_ms else None
        body["live"] = live_body
        return JSONResponse(body, headers={"Cache-Control": "no-store, max-age=0"})

    @app.post("/api/rescan")
    async def rescan(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        """Re-read every instrument's history and re-run the setup scan."""
        if rescan_state.get("running"):
            return {"ok": True, "message": "a re-scan is already running"}
        rescan_state["running"] = True
        rescan_state["result"] = None
        # by default a re-scan *adds* to what you are watching: it re-reads the
        # history, finds the setups in it, and does not throw away a live record
        # of one you already have on screen
        reset = bool((payload or {}).get("reset"))

        async def run() -> None:
            try:
                rescan_state["result"] = await engine.rescan(feed, reset=reset, concurrency=workers)
                await hub.send_all(
                    {
                        "type": "log",
                        "level": "info",
                        "message": f"re-scan finished: {rescan_state['result']['setups']} setup(s) in history",
                    }
                )
            except Exception as exc:  # a bad scan must not kill the app
                await hub.send_all({"type": "log", "level": "error", "message": f"re-scan failed: {exc}"})
            finally:
                rescan_state["running"] = False

        asyncio.create_task(run())
        return {"ok": True, "message": f"re-scanning {len(engine.symbols)} instrument(s)…"}

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {
            "symbols": engine.symbols,
            "timeframes": engine.timeframes,
            "exchange": engine.exchange,
            "filters": engine.filters.config,
            "structure": engine.structure_settings,
            "tdi": engine.tdi_settings,
            "break_lookback": engine.break_lookback,
            "max_symbols": max_symbols,
            "config_path": str(config_path) if config_path else None,
        }

    # --------------------------------------------------------------- symbols
    def _persist_symbols() -> str | None:
        if getattr(feed, "source", "live") == "replay":
            # A replay is a demonstration: it must never overwrite the real
            # instrument list with the handful of symbols it has files for.
            return "replay mode: this change applies to the running session only, config.json is untouched"
        if not config_path:
            return "no config path - changes are in memory only"
        try:
            config["symbols"] = list(engine.symbols)
            config_store.write(config_path, config)
            return None
        except Exception as exc:  # never lose the running bot over a write
            return f"could not write config: {exc}"

    @app.post("/api/symbols")
    async def set_symbols(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        add = config_store.clean_symbols(payload.get("add") or [])
        remove = config_store.clean_symbols(payload.get("remove") or [])
        replace = payload.get("replace")

        warnings: list[str] = []
        if replace is not None:
            wanted = config_store.clean_symbols(replace)
            if len(wanted) > max_symbols:
                wanted = wanted[:max_symbols]
                warnings.append(f"kept the first {max_symbols} symbols (max_symbols in config.json)")
            removed = engine.remove_symbols([s for s in engine.symbols if s not in set(wanted)])
            if removed:
                feed.remove_symbols(removed)
            add = [s for s in wanted if s not in engine.symbols]
            remove = []

        if remove:
            gone = engine.remove_symbols(remove)
            if gone:
                feed.remove_symbols(gone)
            if gone:
                await hub.send_all({"type": "log", "message": f"removed {', '.join(gone)}"})

        room = max_symbols - len(engine.symbols)
        if add and room <= 0:
            warnings.append(f"limit reached: {max_symbols} instruments (raise max_symbols in config.json)")
            add = []
        elif len(add) > room:
            warnings.append(f"added the first {room} of {len(add)} (max_symbols={max_symbols})")
            add = add[:room]

        added = engine.add_symbols(add)
        if added:
            feed.add_symbols(added)
            queue_warm(added)
            await hub.send_all(
                {"type": "log", "message": f"added {len(added)} instrument(s): {', '.join(added[:8])}"
                 + (" ..." if len(added) > 8 else "")}
            )

        problem = _persist_symbols()
        if problem:
            warnings.append(problem)
        await engine._emit_state(force=True)  # noqa: SLF001 - same module boundary
        return {
            "ok": True,
            "symbols": engine.symbols,
            "added": added,
            "removed": remove,
            "warnings": warnings,
        }

    # --------------------------------------------------------------- filters
    @app.post("/api/filters")
    async def set_filters(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        filters = config_store.validate_filters(payload)
        engine.set_filters(filters)
        if config_path:
            config["filters"] = filters
            try:
                config_store.write(config_path, config)
            except Exception as exc:
                return {"ok": True, "filters": filters, "warning": f"could not write config: {exc}"}
        await engine._emit_state(force=True)  # noqa: SLF001
        # the rules changed, so the setups on screen have to be re-derived
        asyncio.create_task(engine.rescan(feed, reset=True, concurrency=workers))
        return {"ok": True, "filters": filters, "summary": engine.filters.summary()}

    @app.post("/api/tdi")
    async def set_tdi(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Retained for compatibility; HVB is permanently bound to the MBL."""
        requested = str(payload.get("hvb_reference") or "mbl").lower()
        reference = engine.set_centre(requested)
        if config_path:
            config["tdi"] = {**config.get("tdi", {}), "hvb_reference": reference}
            try:
                config_store.write(config_path, config)
            except Exception as exc:
                return {"ok": True, "hvb_reference": reference, "warning": f"could not write config: {exc}"}
        await engine._emit_state(force=True)  # noqa: SLF001
        return {
            "ok": True,
            "hvb_reference": "mbl",
            "label": "moving middle band",
            "warning": (
                "fixed 50-axis HVB mode has been removed; only the moving middle-band cross is valid"
                if requested != "mbl" else None
            ),
        }

    @app.post("/api/structure")
    async def set_structure(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        structure = config_store.validate_structure(payload)
        engine.set_structure(structure)
        if config_path:
            config["structure"] = structure
            try:
                config_store.write(config_path, config)
            except Exception as exc:
                return {"ok": True, "structure": structure, "warning": f"could not write config: {exc}"}
        await engine._emit_state(force=True)  # noqa: SLF001
        return {"ok": True, "structure": structure}

    # -------------------------------------------------------------- universe
    async def _universe(force: bool = False) -> dict[str, Any]:
        now = time.time()
        if not force and universe_cache["items"] and now - universe_cache["at"] < 600:
            return universe_cache
        async with universe_lock:
            now = time.time()
            if not force and universe_cache["items"] and now - universe_cache["at"] < 600:
                return universe_cache
            universe_cache["attempts"] += 1
            universe_cache["last_attempt"] = now
            try:
                items = await feed.universe()
                if not isinstance(items, list) or not items:
                    raise RuntimeError("exchange returned zero eligible contracts")
                universe_cache.update(
                    {
                        "items": items,
                        "at": time.time(),
                        "error": None,
                        "successes": universe_cache["successes"] + 1,
                    }
                )
            except Exception as exc:
                # Preserve a previously successful list, but never disguise the
                # failed attempt. Health/UI show both stale coverage and error.
                universe_cache["error"] = f"{type(exc).__name__}: {exc}"
            return universe_cache

    def _apply_discovered(items: list[dict], *, initial: bool) -> tuple[int, int, str | None]:
        ranked = config_store.clean_symbols(
            [
                item.get("symbol")
                for item in items
                if isinstance(item, dict) and item.get("symbol")
            ]
        )
        wanted = ranked[:max_symbols]
        if not wanted:
            return 0, 0, "discovery produced no usable symbol names"

        wanted_set = set(wanted)
        drop = list(engine.symbols) if initial else [
            symbol for symbol in engine.symbols if symbol not in wanted_set
        ]
        removed = engine.remove_symbols(drop)
        if removed:
            feed.remove_symbols(removed)
        added = engine.add_symbols([symbol for symbol in wanted if symbol not in engine.symbols])
        if added:
            feed.add_symbols(added)
            if not initial:
                queue_warm(added)
        universe_cache["selected"] = len(engine.symbols)
        problem = _persist_symbols()
        return len(added), len(removed), problem

    async def _refresh_auto_universe(*, initial: bool) -> bool:
        data = await _universe(force=True)
        if data["error"]:
            retaining = (
                "last discovered"
                if data.get("mode") == "discovered" and data.get("items")
                else "configured fallback"
            )
            await publish(
                {
                    "type": "log",
                    "level": "warn",
                    "message": (
                        f"auto-universe fetch failed; retaining {len(engine.symbols)} {retaining} "
                        f"contracts: {data['error']}"
                    ),
                }
            )
            return False
        available = len(data["items"])
        if available < universe_min_expected:
            data["error"] = (
                f"suspicious partial universe: only {available} contracts "
                f"(minimum expected {universe_min_expected})"
            )
            await publish(
                {
                    "type": "log",
                    "level": "warn",
                    "message": (
                        f"{data['error']}; keeping {len(engine.symbols)} configured fallback contracts"
                    ),
                }
            )
            return False
        added, removed, problem = _apply_discovered(data["items"], initial=initial)
        data["mode"] = "discovered"
        data["selected"] = len(engine.symbols)
        if problem:
            await publish({"type": "log", "level": "warn", "message": problem})
        await publish(
            {
                "type": "log",
                "level": "info",
                "message": (
                    f"auto-universe discovered {available} enabled contracts; "
                    f"scanning {len(engine.symbols)} (added {added}, removed {removed})"
                ),
            }
        )
        await engine._emit_state(force=True)  # noqa: SLF001
        return True

    @app.get("/api/universe")
    async def universe(force: int = 0) -> dict[str, Any]:
        data = await _universe(force=bool(force))
        return {
            "exchange": engine.exchange,
            "error": data["error"],
            "count": len(data["items"]),
            "selected": len(engine.symbols),
            "mode": data["mode"],
            "attempts": data["attempts"],
            "fetched_at": data["at"],
            "last_attempt": data["last_attempt"],
            "diagnostics": getattr(feed, "universe_diagnostics", None),
            "items": data["items"][:4000],
        }

    @app.post("/api/universe/refresh")
    async def refresh_universe() -> dict[str, Any]:
        if not auto_universe:
            return {"ok": False, "error": "auto_universe is disabled"}
        ok = await _refresh_auto_universe(initial=False)
        return {
            "ok": ok,
            "error": universe_cache["error"],
            "available": len(universe_cache["items"]),
            "selected": len(engine.symbols),
            "mode": universe_cache["mode"],
        }

    @app.post("/api/universe/add")
    async def add_from_universe(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """``{"preset": "top50"|"all"}`` or ``{"symbols": [...]}`` from the browser."""
        preset = payload.get("preset")
        symbols = config_store.clean_symbols(payload.get("symbols") or [])
        warnings: list[str] = []
        if preset:
            data = await _universe()
            if data["error"]:
                return {"ok": False, "error": data["error"], "added": []}
            ranked = [item["symbol"] for item in data["items"]]
            if preset == "all":
                symbols = ranked
            else:
                try:
                    count = int(str(preset).replace("top", ""))
                except ValueError:
                    count = 50
                symbols = ranked[:count]

        known = set(engine.symbols)
        wanted = [s for s in symbols if s not in known]
        # only instruments the exchange actually lists
        data = await _universe()
        if data["items"]:
            listed = {item["symbol"] for item in data["items"]}
            unknown = [s for s in wanted if s not in listed]
            if unknown:
                warnings.append(f"not listed on {engine.exchange}: {', '.join(unknown[:5])}")
            wanted = [s for s in wanted if s in listed]

        room = max_symbols - len(engine.symbols)
        if len(wanted) > room:
            warnings.append(f"limit reached: {max_symbols} instruments (raise max_symbols in config.json)")
            wanted = wanted[:room]

        added = engine.add_symbols(wanted)
        if added:
            feed.add_symbols(added)
            queue_warm(added)
        problem = _persist_symbols()
        if problem:
            warnings.append(problem)
        await engine._emit_state(force=True)  # noqa: SLF001
        return {"ok": True, "added": added, "symbols": engine.symbols, "warnings": warnings}

    # ------------------------------------------------------------------- ws
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        await hub.register(ws)
        try:
            await ws.send_text(json.dumps({"type": "state", **engine.snapshot()}, default=str))
            while True:
                message = await ws.receive_text()
                if message == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            await hub.unregister(ws)

    return app
