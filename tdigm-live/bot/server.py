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
    engine.broadcast = hub.send_all
    engine.source = getattr(feed, "source", "live")
    engine.exchange = getattr(feed, "name", "binance")
    max_symbols = int(config.get("max_symbols", 500))
    workers = max(1, int(config.get("workers", 30)))
    db_path = (
        os.environ.get("TDIGM_DB")
        or config.get("database")
        or str(Path(__file__).resolve().parents[1] / "data" / "tdigm.sqlite3")
    )
    store = SignalStore(None if config.get("database") == "" else db_path)
    engine.attach_store(store)
    restored = engine.load_stored()
    universe_cache: dict[str, Any] = {"at": 0.0, "items": [], "error": None}
    tasks: list[asyncio.Task] = []
    warm_queue: asyncio.Queue[str] = asyncio.Queue()
    history_days = float(config.get("history_days", 0) or 0)
    rescan_state: dict[str, Any] = {"running": False, "result": None}
    warm_pending: set[str] = set()

    def queue_warm(symbols: list[str]) -> None:
        """Warm newly added instruments one at a time.

        Adding 300 symbols must not fire 1,500 REST calls at once - the exchange
        would rate-limit us and every row would look broken. They warm in the
        background and show 'warming' until they are ready.
        """
        for symbol in symbols:
            if symbol in warm_pending:
                continue
            warm_pending.add(symbol)
            warm_queue.put_nowait(symbol)

    async def warmer() -> None:
        """One of ``workers`` consumers of the warm-up queue.

        A single consumer meant every instrument the user added waited behind the
        one before it - 500 additions was a 500-step queue. Several consumers
        pull from the same queue, so an add is picked up within one turn and no
        instrument can starve the ones behind it: they all wait in the same FIFO.
        """
        while True:
            symbol = await warm_queue.get()
            try:
                await engine.warm_symbol(feed, symbol)
            except Exception:
                pass
            finally:
                warm_pending.discard(symbol)
                warm_queue.task_done()

    async def pump() -> None:
        """Read candles from the feed and hand them to the engine, forever."""
        try:
            async for candle in feed.candles():
                await engine.on_candle(candle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the server alive, surface the problem
            await hub.send_all({"type": "log", "level": "error", "message": f"feed stopped: {exc}"})
            raise

    async def setup_pusher() -> None:
        """State changes (a pending setup fills, wins, or dies) go out at once."""
        while True:
            await asyncio.sleep(0.5)
            while engine.setup_events:
                payload = engine.setup_events.pop(0)
                await hub.send_all({"type": "setup", "setup": payload})

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

    async def boot() -> None:
        """Warm in the background, then start reading candles.

        Warming used to run *before* uvicorn accepted a single connection, so a
        big universe meant the page looked dead (or "not running") for minutes.
        Now the server is up instantly, the page shows every cell as warming,
        and rows fill in as their history lands.
        """
        try:
            await engine.warmup(feed, concurrency=workers)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never take the page down over warm-up
            await hub.send_all(
                {"type": "log", "level": "error", "message": f"warm-up failed: {exc}"}
            )
        await pump()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks.append(asyncio.create_task(boot()))
        for _ in range(workers):
            tasks.append(asyncio.create_task(warmer()))
        tasks.append(asyncio.create_task(setup_pusher()))
        tasks.append(asyncio.create_task(retry_sweeper()))
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

    app = FastAPI(title="TDIGM liquidity scanner", lifespan=lifespan)
    # the table is ~90 kB of JSON and the page asks for it every few seconds
    # when it has to fall back to HTTP; over a proxy that is worth compressing
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # ------------------------------------------------------------------ page
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
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
            "market": str(config.get("market", "futures")),
            "setups": len(engine.setup_payloads),
            "stored": store.counts().get("total", 0),
            "store": store.enabled,
            "workers": workers,
            "rescanning": bool(rescan_state.get("running")),
            "setups_valid": len(engine.open_setups(valid_only=True, limit=10_000)),
            "candles_seen": engine.candles_seen,
            "signals": engine.kinds.get("signal", 0),
            "symbols": len(engine.symbols),
            "timeframes": engine.timeframes,
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
            "name": getattr(feed, "name", ""),
        }
        # the market the bot is configured for; `feed.market` may be "replay"
        body["market"] = str(config.get("market", "futures"))
        # never cache: a stale table looks exactly like a frozen bot
        return JSONResponse(
            body,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/api/setups")
    async def setups(valid: int = 0, limit: int = 300) -> dict[str, Any]:
        """Every setup the engine knows about, newest first, with its state."""
        rows = engine.open_setups(valid_only=bool(valid), limit=max(1, min(1000, limit)))
        return {
            "setups": rows,
            "counts": engine.setup_counts(),
            "total": len(engine.setup_payloads),
            "market": getattr(feed, "market", "futures"),
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
                  "valid", "mitigated", "entry", "stop", "target", "target_2r", "rr",
                  "score", "r_now", "zone_low", "zone_high", "swept_level", "bos_level",
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
    async def chart(symbol: str, timeframe: str, bars: int = 180) -> JSONResponse:
        """Candles + the markers a setup is made of, for one instrument."""
        return JSONResponse(
            engine.chart(symbol, timeframe, bars),
            headers={"Cache-Control": "no-store, max-age=0"},
        )

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
        """Which line is the centre band the price line must cross."""
        reference = engine.set_centre(str(payload.get("hvb_reference", engine.centre_reference)))
        if config_path:
            config["tdi"] = {**config.get("tdi", {}), "hvb_reference": reference}
            try:
                config_store.write(config_path, config)
            except Exception as exc:
                return {"ok": True, "hvb_reference": reference, "warning": f"could not write config: {exc}"}
        await engine._emit_state(force=True)  # noqa: SLF001
        return {
            "ok": True,
            "hvb_reference": reference,
            "label": "middle of the volatility bands" if reference == "mbl" else "50 midline",
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
        if force or (universe_cache["items"] and now - universe_cache["at"] < 600):
            return universe_cache
        try:
            items = await feed.universe()
            universe_cache.update({"items": items, "at": now, "error": None})
        except Exception as exc:
            universe_cache["error"] = f"{type(exc).__name__}: {exc}"
        return universe_cache

    @app.get("/api/universe")
    async def universe(force: int = 0) -> dict[str, Any]:
        data = await _universe(force=bool(force))
        return {
            "exchange": engine.exchange,
            "error": data["error"],
            "count": len(data["items"]),
            "fetched_at": data["at"],
            "items": data["items"][:4000],
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
