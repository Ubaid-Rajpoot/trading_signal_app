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

from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import config_store
from .engine import Engine
from .feed import is_futures, market_of, to_futures
from .poller import RestPoller

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


class Hub:
    """Keeps the connected browsers and pushes every update to all of them."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.sse: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(ws)

    async def send_all(self, message: dict) -> None:
        for queue in list(self.sse):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # the slowest client misses a frame rather than slowing the scanner
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
    poller = RestPoller(
        feed,
        engine,
        interval=float(config.get("rest_poll_seconds", 6)),
        quiet_after=float(config.get("rest_poll_quiet_after", 45)),
        force=bool(config.get("rest_poll_always", False)),
    )
    app_state: dict[str, Any] = {"warmed": False, "warm_task": None, "poller": poller, "started_at": time.time()}
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

    async def heartbeat() -> None:
        """Push the state even when nothing changed, so no page ever goes stale.

        It also watches the exchange: a socket can stay open while delivering
        nothing at all, which looks exactly like "the data stopped and I had to
        restart the server". After two silent minutes the feed is rebuilt.
        """
        while True:
            await asyncio.sleep(5)
            try:
                if engine.source != "replay" and getattr(feed, "connected", False):
                    last = getattr(feed, "last_message", 0) or 0
                    if last and time.time() - last > 120:
                        feed.force_reconnect()
                        await hub.send_all({"type": "log", "level": "warn",
                                            "message": "the exchange stream went quiet - reconnecting"})
                await engine._emit_state(force=True)  # noqa: SLF001 - one scanner, one clock
            except Exception:
                pass

    async def warm_background() -> None:
        try:
            await engine.warmup(feed)
        finally:
            app_state["warmed"] = True

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # The page must open at once. Warming a dozen instruments over REST takes
        # a while, so it runs next to the server, never in front of it.
        tasks.append(asyncio.create_task(warm_background()))
        tasks.append(asyncio.create_task(pump()))
        tasks.append(asyncio.create_task(warmer()))
        tasks.append(asyncio.create_task(heartbeat()))
        if engine.source != "replay" or bool(config.get("rest_poll_in_replay", False)):
            tasks.append(asyncio.create_task(poller.run()))
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
        last_message = getattr(feed, "last_message", 0) or 0
        return {
            "ok": True,
            "source": engine.source,
            "exchange": engine.exchange,
            "feed": getattr(feed, "status", "unknown"),
            "connected": getattr(feed, "connected", None),
            "clients": len(hub.clients),
            "candles_seen": engine.candles_seen,
            "ticks_seen": engine.ticks_seen,
            "seq": engine.seq,
            "last_tick_age": round(time.time() - engine.last_tick_at, 1),
            "zones": len(engine.zones),
            "symbols": len(engine.symbols),
            "timeframes": engine.timeframes,
            "uptime": round(time.time() - app_state["started_at"], 1),
            "warming": {
                "active": engine.warming,
                "done": engine.warm_done,
                "total": max(1, engine.warm_total),
                "errors": engine.warm_errors,
            },
            # why is data (not) arriving? These fields answer it.
            "stream": {
                "messages": getattr(feed, "messages", 0),
                "parsed": getattr(feed, "parsed", 0),
                "ignored": getattr(feed, "ignored", 0),
                "frames": getattr(feed, "frames", 0),
                "subscribed": len(getattr(feed, "subscribed", []) or []),
                "seconds_since_message": round(time.time() - last_message, 1) if last_message else None,
                "samples": list(getattr(feed, "samples", []) or [])[-2:],
            },
            "rest_poll": poller.stats(),
            "delivering": engine.candles_seen > 0 or engine.ticks_seen > 0,
        }

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        """Server-sent events: the same push as the websocket, over plain HTTP.

        Some proxies (and some corporate networks) drop websockets silently. The
        page listens to whichever transport answers, so the map keeps updating
        without anyone restarting the server.
        """
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
        hub.sse.add(queue)

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(message, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"   # keeps the connection alive through proxies
            finally:
                hub.sse.discard(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no", "connection": "keep-alive"},
        )

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(engine.snapshot())

    @app.get("/api/zones")
    async def zones(status: str = "active", since: int = 0, limit: int = 1000, market: str = "all") -> dict[str, Any]:
        """``active``, ``mitigated``, ``invalidated``, ``resolved`` or ``all``.

        ``since`` (a candle timestamp) keeps the payload to zones that formed or
        were resolved after that moment - which is what the "recently mitigated"
        view asks for.
        """
        if status == "all":
            items = list(engine.zones.values())
        elif status == "resolved":
            items = [z for z in engine.zones.values() if z.get("status") != "active"]
        else:
            items = [z for z in engine.zones.values() if z.get("status") == status]
        if market in ("spot", "futures"):
            items = [z for z in items if (z.get("market") or market_of(z.get("symbol", ""))) == market]
        if since:
            items = [
                z
                for z in items
                if int(z.get("time") or 0) >= since
                or int(z.get("mitigated_at") or 0) >= since
                or int(z.get("invalidated_at") or 0) >= since
            ]
        index = engine._strong_levels()
        for zone in items:
            engine.enrich(zone, index)
        items.sort(key=lambda z: -int(z.get("mitigated_at") or z.get("invalidated_at") or z.get("time") or 0))
        limit = max(1, min(4000, int(limit)))
        return {"count": len(items), "items": items[:limit]}

    @app.get("/api/candles")
    async def candles(symbol: str, timeframe: str = "5m", limit: int = 800, since: int = 0) -> dict[str, Any]:
        """Candles for the chart. ``since`` makes the refresh incremental.

        The page keeps its own copy; while it stays open it only asks for the
        candles it has not seen, so a chart can follow the market all day
        without re-downloading a thousand bars every couple of seconds.
        """
        symbol = symbol.upper()
        map_ = engine.maps.get((symbol, timeframe))
        if map_ is None:
            return {"symbol": symbol, "timeframe": timeframe, "bars": [], "live": False}
        bars = engine.bars(symbol, timeframe, limit if not since else 400)
        if since:
            history = list(engine.history.get((symbol, timeframe), ()))
            fresh = [b for b in history if int(b["open_time"]) >= int(since)]
            live = engine.partial.get((symbol, timeframe))
            if live is not None:
                fresh.append(live)
            return {
                "symbol": symbol,
                "timeframe": timeframe,
                "price": engine.price_of(symbol),
                "atr": map_.atr_now(),
                "live": bool(fresh) and not fresh[-1].get("closed", True),
                "bar_count": len(history),
                "server_time": int(time.time() * 1000),
                "incremental": True,
                "bars": fresh[-limit:],
            }
        return {
            "symbol": map_.symbol,
            "timeframe": map_.timeframe,
            "price": engine.price_of(symbol) or map_.last_price,
            "atr": map_.atr_now(),
            "live": bool(bars) and not bars[-1].get("closed", True),
            "bar_count": len(engine.history.get((symbol, timeframe), ())),
            "server_time": int(time.time() * 1000),
            "bars": bars,
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {
            "symbols": engine.symbols,
            "spot_symbols": [s for s in engine.symbols if not is_futures(s)],
            "futures_symbols": [s for s in engine.symbols if is_futures(s)],
            "timeframes": engine.timeframes,
            "exchange": engine.exchange,
            "settings": engine.settings,
            "max_symbols": max_symbols,
            "source": engine.source,
            "config_path": str(config_path) if config_path else None,
        }

    # --------------------------------------------------------------- symbols
    def _market_lists() -> dict[str, Any]:
        """Spot and futures are stored in their own lists in config.json."""
        return {
            "symbols": [s for s in engine.symbols if not is_futures(s)],
            "futures_symbols": [s for s in engine.symbols if is_futures(s)],
        }

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

        problem = _persist(_market_lists())
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
    async def universe(force: int = 0, market: str = "all") -> dict[str, Any]:
        data = await _universe(force=bool(force))
        items = data["items"]
        if market in ("spot", "futures"):
            items = [i for i in items if (i.get("market") or market_of(i["symbol"])) == market]
        return {
            "exchange": engine.exchange,
            "error": data["error"],
            "count": len(items),
            "futures": len([i for i in data["items"] if (i.get("market") or market_of(i["symbol"])) == "futures"]),
            "spot": len([i for i in data["items"] if (i.get("market") or market_of(i["symbol"])) == "spot"]),
            "fetched_at": data["at"],
            "items": items[:4000],
        }

    @app.post("/api/universe/add")
    async def add_from_universe(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """``{"preset": "top50"|"all"}`` or ``{"symbols": [...]}`` from the browser."""
        preset = payload.get("preset")
        market = str(payload.get("market") or "spot").lower()
        symbols = config_store.clean_symbols(payload.get("symbols") or [])
        if market == "futures":
            symbols = [to_futures(s) for s in symbols]
        warnings: list[str] = []
        data = await _universe()
        if preset:
            if data["error"]:
                return {"ok": False, "error": data["error"], "added": []}
            ranked = [
                item["symbol"]
                for item in data["items"]
                if (item.get("market") or market_of(item["symbol"])) == market
            ]
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
        problem = _persist(_market_lists())
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
