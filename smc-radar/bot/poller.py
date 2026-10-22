"""A REST poller that guarantees the scan keeps moving.

The websocket is the fastest way to get candles, but it is not the only honest
one: the exchange's own kline endpoint returns the very same candles, including
the one that is still forming. On a machine where the socket connects but
delivers nothing, "connected: true, candles_seen: 0" is what the page shows -
and that is exactly what this module exists to prevent.

Every ``interval`` seconds it asks for the last few 1m candles of each symbol:

* the newest, still-open candle is pushed as a **tick** (``closed=False``) so
  the chart animates and the live price moves;
* the first time a candle is seen **closed**, it is pushed with
  ``closed=True`` - that is what the detection map runs on, so a 5m/15m/1h
  candle is still assembled from real 1m candles by the engine.

Duplicates are impossible: the engine ignores a candle whose ``open_time`` it
has already processed. Nothing is invented - every number here comes from the
exchange's REST response.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

MAX_CONCURRENCY = 6


class RestPoller:
    def __init__(
        self,
        feed,
        engine,
        *,
        interval: float = 6.0,
        concurrency: int = MAX_CONCURRENCY,
        quiet_after: float = 45.0,
        force: bool = False,
        idle_interval: float = 30.0,
    ) -> None:
        self.feed = feed
        self.engine = engine
        self.interval = max(2.0, float(interval))
        self.concurrency = max(1, int(concurrency))
        # when the websocket is demonstrably delivering, the poller only tops up
        # the closed candles; when it is silent it is the primary source
        self.quiet_after = float(quiet_after)
        self.force = bool(force)
        # when the websocket is delivering, the poller only has to make sure no
        # closed candle was missed - twice a minute is plenty for that
        self.idle_interval = max(float(interval), float(idle_interval))
        self.last_closed: dict[str, int] = {}
        self.requests = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_run = 0.0
        self.closed_pushed = 0
        self.ticks_pushed = 0
        self.primary = False
        self._sem = asyncio.Semaphore(self.concurrency)

    # ------------------------------------------------------------------ state
    @property
    def silent(self) -> bool:
        """Is the websocket failing to deliver candles?"""
        frames = getattr(self.feed, "parsed", 0)
        if frames:
            return False
        last = getattr(self.feed, "last_message", 0) or 0
        return (time.time() - last) > self.quiet_after if last else True

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "interval": self.interval,
            "primary": self.primary,
            "requests": self.requests,
            "errors": self.errors,
            "last_error": self.last_error,
            "closed_pushed": self.closed_pushed,
            "ticks_pushed": self.ticks_pushed,
            "age": round(time.time() - self.last_run, 1) if self.last_run else None,
        }

    # ------------------------------------------------------------------- loop
    async def run(self) -> None:
        while True:
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let the page freeze over one bad poll
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(self.interval if (self.force or self.silent) else self.idle_interval)

    async def once(self) -> int:
        symbols = list(self.engine.symbols)
        if not symbols:
            return 0
        self.primary = self.force or self.silent
        self.last_run = time.time()
        # when the socket is healthy, refresh closed candles only (cheap);
        # when it is silent, this is the live feed and ticks matter
        include_open = self.primary
        await asyncio.gather(*(self._symbol(s, include_open) for s in symbols))
        return len(symbols)

    async def _symbol(self, symbol: str, include_open: bool) -> None:
        async with self._sem:
            try:
                self.requests += 1
                candles = await asyncio.wait_for(
                    self.feed.history(symbol, "1m", 3, include_open=include_open),
                    timeout=12,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                self.last_error = f"{symbol}: {type(exc).__name__}: {exc}"
                return
            if not candles:
                return
            for candle in candles:
                closed = bool(candle.get("closed"))
                open_time = int(candle["open_time"])
                if closed:
                    if self.last_closed.get(symbol, 0) >= open_time:
                        continue  # already handed to the engine
                    self.last_closed[symbol] = open_time
                    self.closed_pushed += 1
                    await self.engine.on_candle({**candle, "symbol": symbol, "closed": True})
                elif include_open:
                    # the candle that is still forming: a real tick from the
                    # exchange, not a simulation
                    self.ticks_pushed += 1
                    await self.engine.on_candle({**candle, "symbol": symbol, "closed": False})
