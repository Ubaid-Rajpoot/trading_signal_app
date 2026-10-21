"""HTTP + WebSocket server: the page, the state, and the live push."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

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
        payload = json.dumps(message, default=str)
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.unregister(ws)


def create_app(config: dict[str, Any], feed, engine: Engine) -> FastAPI:
    hub = Hub()
    engine.broadcast = hub.send_all
    engine.source = getattr(feed, "source", "live")

    app = FastAPI(title="TDIGM break + HVB bot")
    tasks: list[asyncio.Task] = []

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

    @app.on_event("startup")
    async def start() -> None:
        await engine.warmup(feed)
        tasks.append(asyncio.create_task(pump()))

    @app.on_event("shutdown")
    async def stop() -> None:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        tasks.clear()

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "source": engine.source,
            "feed": getattr(feed, "status", "unknown"),
            "connected": getattr(feed, "connected", None),
            "clients": len(hub.clients),
            "candles_seen": engine.candles_seen,
            "symbols": engine.symbols,
            "timeframes": engine.timeframes,
        }

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(engine.snapshot())

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        await hub.register(ws)
        try:
            await ws.send_text(json.dumps({"type": "state", **engine.snapshot()}, default=str))
            while True:
                # The client only ever listens; this keeps the socket open and
                # notices when the browser goes away.
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
