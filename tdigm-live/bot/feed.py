"""Market data: a live exchange websocket, or a replay of real recorded candles.

Both feeds produce the same thing - **closed 1-minute candles** - so the engine
above them cannot tell the difference:

    {"symbol": "BTCUSDT", "open_time": ms, "close_time": ms,
     "open": f, "high": f, "low": f, "close": f, "volume": f, "closed": True}

``LiveFeed``  : Binance combined kline websocket + REST warm-up history.
``ReplayFeed``: real Binance CSV files pushed at a chosen speed, for offline
                verification. It is labelled ``replay`` everywhere in the UI so
                recorded data can never be mistaken for a live market.
"""

from __future__ import annotations

import asyncio
import csv
import json
import random
import time
from pathlib import Path
from typing import AsyncIterator

import httpx
import websockets

BINANCE_REST = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443/stream?streams="
BINANCE_HOSTS = [BINANCE_REST, "https://data-api.binance.vision"]


class FeedError(RuntimeError):
    pass


def _kline_to_candle(symbol: str, k: dict) -> dict:
    return {
        "symbol": symbol.upper(),
        "open_time": int(k["t"]),
        "close_time": int(k["T"]),
        "open": float(k["o"]),
        "high": float(k["h"]),
        "low": float(k["l"]),
        "close": float(k["c"]),
        "volume": float(k["v"]),
        "trades": int(k.get("n", 0)),
        "closed": bool(k["x"]),
    }


class LiveFeed:
    """Binance spot klines. One combined websocket for every symbol."""

    source = "live"

    def __init__(self, symbols: list[str], timeframe: str = "1m") -> None:
        self.symbols = [s.upper() for s in symbols]
        self.timeframe = timeframe
        self.connected = False
        self.last_message = 0.0
        self.status = "starting"

    # ------------------------------------------------------------ history
    async def history(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        """Closed candles from REST, newest last. Used to warm the indicators."""
        last_error: Exception | None = None
        params = {"symbol": symbol.upper(), "interval": interval, "limit": min(limit, 1000)}
        for host in BINANCE_HOSTS:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(f"{host}/api/v3/klines", params=params)
                if resp.status_code != 200:
                    last_error = FeedError(f"{host} -> HTTP {resp.status_code}")
                    continue
                rows = resp.json()
                now_ms = int(time.time() * 1000)
                candles = [
                    {
                        "symbol": symbol.upper(),
                        "open_time": int(r[0]),
                        "close_time": int(r[6]),
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[5]),
                        "trades": int(r[8]),
                        "closed": True,
                    }
                    for r in rows
                    if int(r[6]) < now_ms  # drop the still-forming candle
                ]
                if candles:
                    return candles
                last_error = FeedError("exchange returned no closed candles")
            except Exception as exc:  # network, DNS, TLS ...
                last_error = exc
        raise FeedError(str(last_error) or "exchange unreachable")

    # -------------------------------------------------------------- stream
    async def candles(self) -> AsyncIterator[dict]:
        streams = "/".join(f"{s.lower()}@kline_{self.timeframe}" for s in self.symbols)
        url = BINANCE_WS + streams
        attempt = 0
        while True:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=2048,
                ) as ws:
                    self.connected = True
                    self.status = "connected"
                    attempt = 0
                    async for raw in ws:
                        message = json.loads(raw)
                        data = message.get("data") or {}
                        if data.get("e") != "kline":
                            continue
                        self.last_message = time.time()
                        candle = _kline_to_candle(data["s"], data["k"])
                        # Only closed candles drive the signals; the forming one is
                        # forwarded with closed=False for the live preview.
                        yield candle
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                attempt += 1
                self.status = f"reconnecting ({attempt}) - {type(exc).__name__}: {exc}"
                delay = min(30, 2 ** min(attempt, 5)) + random.uniform(0, 1)
                await asyncio.sleep(delay)


class ReplayFeed:
    """Real recorded candles, played back at ``speed`` bars per second."""

    source = "replay"

    def __init__(
        self,
        data_dir: str | Path,
        symbols: list[str] | None = None,
        speed: float = 20.0,
        loop: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.speed = max(0.1, float(speed))
        self.loop = loop
        self.connected = True
        self.status = f"replay {self.speed:g} bars/s"
        self.files = self._discover(symbols)
        if not self.files:
            raise FeedError(f"no CSV files found in {self.data_dir}")

    def _discover(self, symbols: list[str] | None) -> list[tuple[str, Path]]:
        found: list[tuple[str, Path]] = []
        for path in sorted(self.data_dir.rglob("*.csv")):
            symbol = path.name.split("-")[0].upper()
            if symbols and symbol not in {s.upper() for s in symbols}:
                continue
            found.append((symbol, path))
        return found

    async def history(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        # The replay warms up by simply streaming; the engine handles it.
        return []

    async def candles(self) -> AsyncIterator[dict]:
        while True:
            for symbol, path in self.files:
                with path.open(newline="") as handle:
                    for row in csv.reader(handle):
                        if len(row) < 6:
                            continue
                        raw_time = int(row[0])
                        open_time = raw_time // 1000 if raw_time > 10**14 else raw_time
                        if open_time < 10**12:  # seconds
                            open_time *= 1000
                        yield {
                            "symbol": symbol,
                            "open_time": open_time,
                            "close_time": open_time + 59_999,
                            "open": float(row[1]),
                            "high": float(row[2]),
                            "low": float(row[3]),
                            "close": float(row[4]),
                            "volume": float(row[5]),
                            "trades": 0,
                            "closed": True,
                        }
                        await asyncio.sleep(1.0 / self.speed)
            if not self.loop:
                return
