"""HTTP + WebSocket server: the page, the map, the live push, the controls.

Anything the page changes (instruments, detection settings) is applied to the
running engine immediately and written back to ``config.json`` so it survives a
restart. The exchange universe (every MEXC symbol and its turnover) is fetched
**server side** and cached - the browser never calls an exchange directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from . import config_store
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
    engine.exchange = getattr(feed, "name", "mexc")
    max_symbols = int(config.get("max_symbols", 400))
    universe_cache: dict[str, Any] = {"at": 0.0, "items": [], "error": None}
    tasks: list[asyncio.Task] = []
    warm_queue: asyncio.Queue[str] = asyncio.Queue()
    warm_pending: set[str] = set()

    def queue_warm(symbols: list[str]) -> None:
        """Warm newly added instruments one at a time.

        Adding 300 symbols must not fire 1,500 REST calls at once - the exchange
        would rate-limit us and every row would look broken. They warm in the
        background and show "warming" until they are ready.
        """
        for symbol in symbols:
            if symbol in warm_pending:
                continue
            warm_pending.add(symbol)
            warm_queue.put_nowait(symbol)

    async def warmer() -> None:
        while True:
            symbol = await warm_queue.get()
            try:
                await engine.warm_symbol(feed, symbol)
                await engine._emit_state(force=True)  # noqa: SLF001
            except Exception:
                pass
            finally:
                warm_pending.discard(symbol)
                warm_queue.task_done()
            await asyncio.sleep(0.05)

    async def pump() -> None:
        try:
            async for candle in feed.candles():
                await engine.on_candle(candle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # keep the server alive, surface the problem
            await hub.send_all({"type": "log", "level": "error", "message": f"feed stopped: {exc}"})
            raise

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await engine.warmup(feed)
        tasks.append(asyncio.create_task(pump()))
        tasks.append(asyncio.create_task(warmer()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            tasks.clear()

    app = FastAPI(title="SMC radar - strong highs, algo candles, rejection blocks", lifespan=lifespan)

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
            "clients": len(hub.clients),
            "candles_seen": engine.candles_seen,
            "symbols": len(engine.symbols),
            "timeframes": engine.timeframes,
            "zones": len(engine.zones),
        }

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(engine.snapshot())

    @app.get("/api/zones")
    async def zones(status: str = "active") -> dict[str, Any]:
        if status == "all":
            items = list(engine.zones.values())
        else:
            items = [z for z in engine.zones.values() if z.get("status") == status]
        items.sort(key=lambda z: -int(z.get("time") or 0))
        return {"count": len(items), "items": items[:1000]}

    @app.get("/api/candles")
    async def candles(symbol: str, timeframe: str = "5m", limit: int = 300) -> dict[str, Any]:
        map_ = engine.maps.get((symbol.upper(), timeframe))
        if map_ is None:
            return {"symbol": symbol.upper(), "timeframe": timeframe, "bars": []}
        bars = list(map_.bars)[-max(20, min(600, int(limit))) :]
        return {
            "symbol": map_.symbol,
            "timeframe": map_.timeframe,
            "price": map_.last_price,
            "atr": map_.atr_now(),
            "bars": bars,
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {
            "symbols": engine.symbols,
            "timeframes": engine.timeframes,
            "exchange": engine.exchange,
            "settings": engine.settings,
            "max_symbols": max_symbols,
            "source": engine.source,
            "config_path": str(config_path) if config_path else None,
        }

    # --------------------------------------------------------------- symbols
    def _persist(updates: dict[str, Any]) -> str | None:
        if getattr(feed, "source", "live") == "replay":
            # A replay is a demonstration: it must never overwrite the real
            # instrument list with the handful of symbols it has files for.
            return "replay mode: this change applies to the running session only, config.json is untouched"
        if not config_path:
            return "no config path - changes are in memory only"
        try:
            config.update(updates)
            config_store.write(config_path, config)
            return None
        except Exception as exc:  # never lose the running app over a write
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
            gone = engine.remove_symbols([s for s in engine.symbols if s not in set(wanted)])
            if gone:
                feed.remove_symbols(gone)
            add = [s for s in wanted if s not in engine.symbols]
            remove = []

        if remove:
            gone = engine.remove_symbols(remove)
            if gone:
                feed.remove_symbols(gone)
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
                {
                    "type": "log",
                    "message": f"added {len(added)} instrument(s): {', '.join(added[:8])}"
                    + (" ..." if len(added) > 8 else ""),
                }
            )

        problem = _persist({"symbols": list(engine.symbols)})
        if problem:
            warnings.append(problem)
        await engine._emit_state(force=True)  # noqa: SLF001 - same module boundary
        return {"ok": True, "symbols": engine.symbols, "added": added, "removed": remove, "warnings": warnings}

    # -------------------------------------------------------------- settings
    @app.post("/api/settings")
    async def set_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        settings = config_store.validate_settings({**engine.settings, **payload})
        engine.set_settings(settings)
        await engine._emit_state(force=True)  # noqa: SLF001
        problem = _persist({"settings": engine.settings})
        return {"ok": True, "settings": engine.settings, "warning": problem}

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
        data = await _universe()
        if preset:
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
        problem = _persist({"symbols": list(engine.symbols)})
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
