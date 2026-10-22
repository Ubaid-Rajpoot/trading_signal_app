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

from collections import deque

from .buckets import TIMEFRAMES, Rollup
from .map import DEFAULTS, KINDS, MarketMap

MAX_ZONES = 1200
MAX_HISTORY = 1500      # candles kept per instrument/timeframe for the chart
WARM_BARS = 1000        # candles requested from the exchange at start-up


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
        # Chart history, deliberately much longer than the detection lookback:
        # the map needs 240 bars to find a pattern, the trader needs a year of
        # candles to see where it sits.
        self.history: dict[tuple[str, str], deque] = {}
        self.partial: dict[tuple[str, str], dict] = {}   # the candle still forming
        self.last_open: dict[str, int] = {}              # last closed 1m open_time per symbol
        self.zones: dict[str, dict] = {}          # key -> zone dict (live state)
        self.events: list[dict] = []              # newest first: formed / mitigated
        self.kinds: dict[str, int] = {}
        self.resolved: dict[str, int] = {"mitigated": 0, "invalidated": 0}
        self.candles_seen = 0
        self.ticks_seen = 0
        self.loops_reset = 0
        self.seq = 0
        self.last_tick_at = time.time()
        self.started_at = time.time()
        # warm-up progress, so the page can say what it is waiting for
        self.warm_total = len(self.symbols) * len(self.timeframes)
        self.warm_done = 0
        self.warming = True
        self.warm_errors = 0
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
            self.history[key] = deque(maxlen=MAX_HISTORY)
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
            self.warm_total += len(added) * len(self.timeframes)
        return added

    def remove_symbols(self, symbols: list[str]) -> list[str]:
        drop = {str(s).upper() for s in symbols}
        removed = [s for s in self.symbols if s in drop]
        if removed:
            self.symbols = [s for s in self.symbols if s not in drop]
            self.maps = {k: v for k, v in self.maps.items() if k[0] not in drop}
            self.rollups = {k: v for k, v in self.rollups.items() if k[0] not in drop}
            self.history = {k: v for k, v in self.history.items() if k[0] not in drop}
            self.last_open = {k: v for k, v in self.last_open.items() if k not in drop}
            self._recount_warm()
            self.partial = {k: v for k, v in self.partial.items() if k[0] not in drop}
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
        self._recount_warm()
        return added

    def _recount_warm(self) -> None:
        """Progress must describe the instruments that are actually watched."""
        total = len(self.symbols) * len(self.timeframes)
        if total != self.warm_total:
            self.warm_total = total
            self.warm_done = min(self.warm_done, total)

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
        """Fill one instrument's chart history and detection map.

        ``WARM_BARS`` candles are asked for, which is the most a REST kline
        endpoint will give in one call: the page should open on a filled chart,
        not on a dozen candles.
        """
        symbol = symbol.upper()
        for tf in self.timeframes:
            key = (symbol, tf)
            map_ = self.maps.get(key)
            if map_ is None:
                continue
            if len(self.history.get(key, ())) >= 150:
                self.warm_done += 1   # already warm (a replay pre-roll, say)
                continue
            interval = feed.exchange_interval(tf) if hasattr(feed, "exchange_interval") else tf
            if interval is None:
                await self._warm_from_minutes(feed, symbol, tf)
                self.warm_done += 1
                continue
            try:
                candles = await asyncio.wait_for(feed.history(symbol, interval, WARM_BARS), timeout=20)
                self._warm_failures = 0
            except Exception as exc:  # a broken symbol must not kill the scanner
                self._warm_failures += 1
                self.warm_errors += 1
                self.warm_done += 1
                if self._warm_failures <= 3:
                    await self._log(f"warm-up {symbol} {tf}: {type(exc).__name__}: {exc}", "warn")
                continue
            for bar in candles:
                self._append(key, {**bar, "closed": True})
            self.warm_done += 1

    async def _warm_from_minutes(self, feed, symbol: str, tf: str) -> None:
        key = (symbol, tf)
        try:
            bars = await asyncio.wait_for(feed.history(symbol, "1m", WARM_BARS), timeout=20)
        except Exception:
            return
        rollup = Rollup(tf)
        for bar in bars:
            for done in rollup.add(bar):
                self._append(key, done)
        # keep the roll-up in step with the live stream
        self.rollups[key] = rollup

    async def warmup(self, feed, concurrency: int = 4) -> None:
        """Warm every instrument, a few at a time.

        Start-up used to wait for every symbol before the page was served, which
        on a dozen instruments meant a minute of staring at nothing. Now the map
        warms in the background while the page is already open and reporting
        progress (``warming`` / ``warm_done`` / ``warm_total``).
        """
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(symbol: str) -> None:
            async with sem:
                if self._warm_failures >= 8:
                    return
                try:
                    await self.warm_symbol(feed, symbol)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.warm_errors += 1

        try:
            await asyncio.gather(*(one(symbol) for symbol in list(self.symbols)))
        finally:
            self.warming = False
            if self._warm_failures >= 8:
                await self._log("warm-up stopped: the exchange is not answering - live candles will fill the map", "error")
        await self._emit_state(force=True)

    async def backfill(self, feed, symbol: str) -> None:
        """Top up a symbol that arrived through the live stream with no history."""
        await self.warm_symbol(feed, symbol)
        await self._emit_state(force=True)

    # -------------------------------------------------------------- candles
    def _append(self, key: tuple[str, str], bar: dict) -> dict[str, Any] | None:
        map_ = self.maps.get(key)
        if map_ is None:
            return None
        history = self.history.get(key)
        if history is None:
            history = self.history[key] = deque(maxlen=MAX_HISTORY)
        normalised = {
            "open_time": int(bar["open_time"]),
            "close_time": int(bar.get("close_time") or bar["open_time"]),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar.get("volume") or 0.0),
            "closed": True,
        }
        if history and normalised["open_time"] <= history[-1]["open_time"]:
            return None  # out of order: never let the chart go backwards
        history.append(normalised)
        self.partial.pop(key, None)
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

    def _reset_cell(self, key: tuple[str, str]) -> None:
        """A replay looped: start this cell again instead of stacking old bars."""
        map_ = self.maps.get(key)
        if map_ is None:
            return
        self.maps[key] = MarketMap(map_.symbol, map_.timeframe, self.settings)
        self.history[key] = deque(maxlen=MAX_HISTORY)
        self.partial.pop(key, None)
        self.last_open.pop(key[0], None)
        if key[1] != "1m":
            self.rollups[key] = Rollup(key[1])
        self.zones = {k: v for k, v in self.zones.items() if v.get("symbol") != key[0] or v.get("timeframe") != key[1]}
        self.events = [e for e in self.events if (e.get("zone") or {}).get("timeframe") != key[1]]

    def _apply_tick(self, symbol: str, candle: dict) -> None:
        """A candle that is still forming: update the charts, do not detect yet."""
        self.ticks_seen += 1
        self.last_tick_at = time.time()
        for tf in self.timeframes:
            key = (symbol, tf)
            if key not in self.maps:
                continue
            if tf == "1m":
                self.partial[key] = {
                    "open_time": int(candle["open_time"]),
                    "close_time": int(candle.get("close_time") or candle["open_time"]),
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "volume": float(candle.get("volume") or 0.0),
                    "closed": False,
                }
            else:
                preview = self._preview(key, candle)
                if preview is None:
                    continue
                history = self.history.get(key)
                if history and preview["open_time"] <= history[-1]["open_time"]:
                    continue
                self.partial[key] = preview

    def _preview(self, key: tuple[str, str], candle: dict) -> dict | None:
        """The higher-timeframe candle that is forming right now.

        It is assembled from the 1m candles already stored in this bucket plus
        the live tick - so a 15m candle shows the true open, the true high/low
        and the current close from the first minute, not just the last tick.
        """
        tf = key[1]
        step = TIMEFRAMES.get(tf)
        if step is None:
            return None
        start = (int(candle["open_time"]) // step) * step
        merged = {
            "open_time": start,
            "close_time": start + step - 1,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume") or 0.0),
            "timeframe": tf,
            "closed": False,
        }
        history = self.history.get(key)
        if history:
            for bar in reversed(history):
                if int(bar["open_time"]) < start:
                    break
                merged["high"] = max(merged["high"], float(bar["high"]))
                merged["low"] = min(merged["low"], float(bar["low"]))
                merged["open"] = float(bar["open"])
                merged["volume"] += float(bar.get("volume") or 0.0)
        return merged

    def ingest(self, candle: dict) -> None:
        """Take one CLOSED 1m candle and update every timeframe.

        Synchronous on purpose: it is shared by the live stream and by the
        replay pre-roll, which has to fill a thousand candles before the page
        opens without paying for a websocket round trip each time.
        """
        symbol = str(candle["symbol"]).upper()
        self.candles_seen += 1
        self.last_event_at = time.time()
        self.last_tick_at = time.time()
        self.partial.pop((symbol, "1m"), None)
        open_time = int(candle["open_time"])
        previous_open = self.last_open.get(symbol)
        if previous_open is not None and open_time < previous_open:
            # the feed went back in time: a replay looped, so start over cleanly
            self.loops_reset += 1
            for tf in self.timeframes:
                self._reset_cell((symbol, tf))
            self._pending_log = f"{symbol}: feed rewound, map restarted"
        self.last_open[symbol] = open_time
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

    async def on_candle(self, candle: dict) -> None:
        symbol = str(candle["symbol"]).upper()
        if not candle.get("closed", True):
            # live tick: the candle is growing. Charts update, the map waits for
            # the close because a wick that is not final cannot confirm anything.
            self._apply_tick(symbol, candle)
            await self._emit_state()
            return
        self.ingest(candle)
        note = getattr(self, "_pending_log", None)
        if note:
            self._pending_log = None
            await self._log(note)
        await self._emit_state()

    def preroll_feed(self, feed, bars_per_symbol: int) -> int:
        """Fill the map with the first N recorded candles before streaming starts."""
        loaded = 0
        first = getattr(feed, "first_bars", None)
        if first is None:
            return 0
        for symbol in list(self.symbols):
            for bar in first(symbol, bars_per_symbol):
                self.ingest(bar)
                loaded += 1
        return loaded

    # ---------------------------------------------------------------- charts
    def bars(self, symbol: str, timeframe: str, limit: int = 800) -> list[dict]:
        """Closed candles plus the one still forming - what the chart draws."""
        key = (symbol.upper(), timeframe)
        history = self.history.get(key)
        if history is None:
            return []
        limit = max(20, min(MAX_HISTORY, int(limit)))
        bars = list(history)[-limit:]
        live = self.partial.get(key)
        if live is not None:
            if bars and live["open_time"] <= bars[-1]["open_time"]:
                bars[-1] = live
            else:
                bars.append(live)
        return bars

    def price_of(self, symbol: str) -> float | None:
        """The freshest price we hold, preferring the live 1m bar."""
        symbol = symbol.upper()
        for tf in sorted(self.timeframes, key=lambda t: TIMEFRAMES.get(t, 0)):
            live = self.partial.get((symbol, tf))
            if live is not None:
                return float(live["close"])
            history = self.history.get((symbol, tf))
            if history:
                return float(history[-1]["close"])
        return None

    async def _emit_state(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_state_at < self.state_interval:
            return
        self._last_state_at = now
        self.seq += 1
        # the resolved zones are fetched on demand (/api/zones), not pushed
        await self._broadcast({"type": "state", **self.snapshot(cells=False, with_history=False)})

    def active_zones(self) -> list[dict]:
        return [z for z in self.zones.values() if z.get("status") == "active"]

    async def _broadcast(self, message: dict) -> None:
        if self.broadcast is not None:
            await self.broadcast(message)

    async def _log(self, message: str, level: str = "info") -> None:
        await self._broadcast({"type": "log", "level": level, "message": message})

    # -------------------------------------------------------------- snapshot
    @staticmethod
    def slim(zone: dict) -> dict:
        """Only the fields the page draws - keeps every push small."""
        return {
            key: zone.get(key)
            for key in (
                "key", "kind", "side", "symbol", "timeframe", "top", "bottom", "level",
                "time", "status", "strength", "evidence", "swept_level", "break_level",
                "fvg", "inducement", "session", "mitigated_at", "mitigated_price",
                "invalidated_at", "invalidated_price",
            )
        }

    @staticmethod
    def light(zone: dict) -> dict:
        """The few fields the live tape prints."""
        return {
            key: zone.get(key)
            for key in ("key", "kind", "side", "symbol", "timeframe", "level", "time", "status",
                        "mitigated_at", "invalidated_at")
        }

    def snapshot(self, cells: bool = True, with_history: bool = True) -> dict[str, Any]:
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
            "active": [self.slim(z) for z in active[:300]],
            "history": [self.slim(z) for z in history[:100]] if with_history else [],
            "events": [
                {**event, "zone": self.light(event["zone"]) if isinstance(event.get("zone"), dict) else None}
                for event in self.events[:30]
            ],
            "seq": self.seq,
            "warming": {
                "active": self.warming,
                "done": self.warm_done,
                "total": max(1, self.warm_total),
                "errors": self.warm_errors,
            },
            "server_time": int(time.time() * 1000),
            "candles_seen": self.candles_seen,
            "ticks_seen": self.ticks_seen,
            "loops_reset": self.loops_reset,
            "last_candle_age": round(time.time() - self.last_event_at, 1),
            "last_tick_age": round(time.time() - self.last_tick_at, 1),
            "uptime": round(time.time() - self.started_at, 1),
            "price": {symbol: price for symbol in self.symbols if (price := self.price_of(symbol)) is not None},
            "cells": [] if not cells else [
                {
                    "symbol": map_.symbol,
                    "timeframe": map_.timeframe,
                    "bars": len(self.history.get((map_.symbol, map_.timeframe), ())),
                    "price": self.price_of(map_.symbol),
                    "atr": map_.atr_now(),
                    "active": len(map_.active()),
                    "trend": map_.trend_label,
                }
                for map_ in self.maps.values()
            ],
        }
