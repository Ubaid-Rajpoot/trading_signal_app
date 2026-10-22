"""The scanner: every instrument x every timeframe, kept on one map.

The engine does three things and nothing else:

1. turns the exchange's 1-minute candles into every timeframe (``buckets.py``),
2. feeds each closed candle into that instrument's :class:`MarketMap`,
3. keeps a live list of every zone - **active until price mitigates it** - and
   pushes the changes to the page.

A zone never disappears while it is active: it is either shown, or it is marked
``mitigated`` / ``invalidated`` with the candle and the price that did it, so the
page can always answer "when did this form, and what happened to it".
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from .buckets import TIMEFRAMES, Rollup
from .map import DEFAULTS, KINDS, MarketMap

MAX_ZONES = 1200


class Engine:
    def __init__(
        self,
        *,
        symbols: list[str],
        timeframes: list[str],
        settings: dict[str, Any] | None = None,
        broadcast: Callable[[dict], Awaitable[None]] | None = None,
        preview_max_symbols: int = 200,
    ) -> None:
        self.symbols = [str(s).upper() for s in symbols]
        self.timeframes = [tf for tf in timeframes if tf in TIMEFRAMES]
        self.settings = {**DEFAULTS, **(settings or {})}
        self.broadcast = broadcast
        self.preview_max_symbols = int(preview_max_symbols)
        self.source = "live"
        self.exchange = "mexc"

        self.maps: dict[tuple[str, str], MarketMap] = {}
        self.rollups: dict[tuple[str, str], Rollup] = {}
        self.zones: dict[str, dict] = {}          # key -> zone dict (live state)
        self.events: list[dict] = []              # newest first: formed / mitigated
        self.kinds: dict[str, int] = {}
        self.resolved: dict[str, int] = {"mitigated": 0, "invalidated": 0}
        self.candles_seen = 0
        self.started_at = time.time()
        self.last_event_at = time.time()
        self._last_state_at = 0.0
        self._warm_failures = 0
        self.state_interval = self._interval_for(len(self.symbols))

        for symbol in self.symbols:
            self.add_cells(symbol)

    # ------------------------------------------------------------- plumbing
    @staticmethod
    def _interval_for(count: int) -> float:
        if count <= 60:
            return 0.35
        if count <= 300:
            return 1.0
        return 2.0

    def add_cells(self, symbol: str) -> None:
        for tf in self.timeframes:
            key = (symbol, tf)
            if key in self.maps:
                continue
            self.maps[key] = MarketMap(symbol, tf, self.settings)
            if tf != "1m":
                self.rollups[key] = Rollup(tf)

    def add_symbols(self, symbols: list[str]) -> list[str]:
        added: list[str] = []
        for raw in symbols:
            symbol = str(raw).strip().upper()
            if not symbol or symbol in self.symbols:
                continue
            self.symbols.append(symbol)
            self.add_cells(symbol)
            added.append(symbol)
        if added:
            self.state_interval = self._interval_for(len(self.symbols))
        return added

    def remove_symbols(self, symbols: list[str]) -> list[str]:
        drop = {str(s).upper() for s in symbols}
        removed = [s for s in self.symbols if s in drop]
        if removed:
            self.symbols = [s for s in self.symbols if s not in drop]
            self.maps = {k: v for k, v in self.maps.items() if k[0] not in drop}
            self.rollups = {k: v for k, v in self.rollups.items() if k[0] not in drop}
            self.zones = {k: v for k, v in self.zones.items() if v.get("symbol") not in drop}
            self.state_interval = self._interval_for(len(self.symbols))
        return removed

    def set_symbols(self, symbols: list[str]) -> list[str]:
        """Replace the instrument list (replay: only what the files speak for)."""
        wanted: list[str] = []
        for raw in symbols:
            symbol = str(raw).upper().strip()
            if symbol and symbol not in wanted:
                wanted.append(symbol)
        for symbol in [s for s in self.symbols if s not in wanted]:
            self.remove_symbols([symbol])
        added = self.add_symbols(wanted)
        self.state_interval = self._interval_for(len(self.symbols))
        return added

    def restrict_symbols(self, available: list[str]) -> None:
        keep = {str(s).upper() for s in available}
        drop = [s for s in self.symbols if s not in keep]
        if drop:
            self.remove_symbols(drop)

    def set_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Apply detection settings to every instrument, keeping what is known.

        Rebuilding a map means re-reading the candles it still holds, so the
        zones on screen are the ones the new setting would have found - a map
        that silently forgets the last hour would be worse than useless.
        """
        merged = {**self.settings, **(settings or {})}
        if merged == self.settings:
            return self.settings
        self.settings = merged
        for key, old in list(self.maps.items()):
            fresh = MarketMap(old.symbol, old.timeframe, merged)
            for bar in old.bars:
                fresh.add(bar)
            self.maps[key] = fresh
        # zones that are still live keep their status: they were found by a
        # different setting, so they are relabelled from the rebuilt map
        live: dict[str, dict] = {}
        for map_ in self.maps.values():
            for zone in map_.zones:
                live.setdefault(zone.key, zone.as_dict())
        # keep previously resolved history, drop the ones the new setting lost
        for key, zone in self.zones.items():
            if zone.get("status") != "active" and key not in live:
                live[key] = zone
        self.zones = live
        return self.settings

    # -------------------------------------------------------------- warm-up
    async def warm_symbol(self, feed, symbol: str) -> None:
        symbol = symbol.upper()
        for tf in self.timeframes:
            key = (symbol, tf)
            map_ = self.maps.get(key)
            if map_ is None or len(map_.bars) >= 150:
                continue
            interval = feed.exchange_interval(tf) if hasattr(feed, "exchange_interval") else tf
            if interval is None:
                await self._warm_from_minutes(feed, symbol, tf)
                continue
            try:
                candles = await asyncio.wait_for(feed.history(symbol, interval, 400), timeout=15)
                self._warm_failures = 0
            except Exception as exc:  # a broken symbol must not kill the scanner
                self._warm_failures += 1
                if self._warm_failures <= 3:
                    await self._log(f"warm-up {symbol} {tf}: {type(exc).__name__}: {exc}", "warn")
                continue
            for bar in candles:
                self._append(key, {**bar, "closed": True})

    async def _warm_from_minutes(self, feed, symbol: str, tf: str) -> None:
        key = (symbol, tf)
        minutes = int(TIMEFRAMES[tf] / 60_000)
        try:
            bars = await feed.history(symbol, "1m", min(1000, 400 * minutes + 200))
        except Exception:
            return
        rollup = Rollup(tf)
        for bar in bars:
            for done in rollup.add(bar):
                self._append(key, done)

    async def warmup(self, feed) -> None:
        for symbol in list(self.symbols):
            if self._warm_failures >= 6:
                # the exchange is not answering: serving the page now is more
                # useful than waiting out a timeout for every instrument
                await self._log("warm-up stopped: the exchange is not answering - live candles will fill the map", "error")
                break
            await self.warm_symbol(feed, symbol)
        await self._emit_state(force=True)

    # -------------------------------------------------------------- candles
    def _append(self, key: tuple[str, str], bar: dict) -> dict[str, Any] | None:
        map_ = self.maps.get(key)
        if map_ is None:
            return None
        events = map_.add(bar)
        if events.get("zones"):
            for zone in events["zones"]:
                self.zones[zone["key"]] = zone
                self.kinds[zone["kind"]] = self.kinds.get(zone["kind"], 0) + 1
                self.events.insert(0, {"type": "formed", "zone": zone})
        if events.get("resolved"):
            for item in events["resolved"]:
                status = item["status"]
                current = self.zones.get(item["key"])
                if current is not None:
                    # copy the resolution from the map's own zone object
                    for zone in map_.zones:
                        if zone.key == item["key"]:
                            current.update(zone.as_dict())
                            break
                if status in self.resolved:
                    self.resolved[status] += 1
                self.events.insert(0, {"type": status, "key": item["key"], "zone": current})
        del self.events[400:]
        if events.get("zones") or events.get("resolved"):
            self.last_event_at = time.time()
        return events

    async def on_candle(self, candle: dict) -> None:
        symbol = str(candle["symbol"]).upper()
        if not candle.get("closed", True):
            return
        self.candles_seen += 1
        self.last_event_at = time.time()
        for tf in self.timeframes:
            key = (symbol, tf)
            if key not in self.maps:
                continue
            if tf == "1m":
                completed = [dict(candle, timeframe="1m", closed=True)]
            else:
                rollup = self.rollups.get(key)
                completed = rollup.add(candle) if rollup else []
            for bar in completed:
                self._append(key, bar)
        await self._emit_state()

    async def _emit_state(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_state_at < self.state_interval:
            return
        self._last_state_at = now
        await self._broadcast({"type": "state", **self.snapshot()})

    def active_zones(self) -> list[dict]:
        return [z for z in self.zones.values() if z.get("status") == "active"]

    async def _broadcast(self, message: dict) -> None:
        if self.broadcast is not None:
            await self.broadcast(message)

    async def _log(self, message: str, level: str = "info") -> None:
        await self._broadcast({"type": "log", "level": level, "message": message})

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> dict[str, Any]:
        active = [z for z in self.zones.values() if z.get("status") == "active"]
        active.sort(key=lambda z: -int(z.get("time") or 0))
        history = [z for z in self.zones.values() if z.get("status") != "active"]
        history.sort(key=lambda z: -int(z.get("mitigated_at") or z.get("invalidated_at") or 0))
        return {
            "source": self.source,
            "exchange": self.exchange,
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "settings": self.settings,
            "kinds": [k for k in KINDS] + ["weak_high", "weak_low"],
            "counts": dict(self.kinds),
            "resolved": dict(self.resolved),
            "active": active[:400],
            "history": history[:200],
            "events": self.events[:60],
            "candles_seen": self.candles_seen,
            "last_candle_age": round(time.time() - self.last_event_at, 1),
            "uptime": round(time.time() - self.started_at, 1),
            "price": {map_.symbol: map_.last_price for map_ in self.maps.values() if map_.timeframe == "1m"},
            "cells": [
                {
                    "symbol": map_.symbol,
                    "timeframe": map_.timeframe,
                    "bars": len(map_.bars),
                    "price": map_.last_price,
                    "atr": map_.atr_now(),
                    "active": len(map_.active()),
                    "trend": map_.trend_label,
                }
                for map_ in self.maps.values()
            ],
        }
