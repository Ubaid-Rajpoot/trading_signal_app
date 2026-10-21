"""The scanner: keeps every symbol/timeframe up to date and shouts when both
things happen on the same closed candle:

    1. a high/low break  - the close takes out the previous ``break_lookback`` highs/lows
    2. an HVB            - the TDIGM purple line crosses the centre band

That is the whole product. Everything else here exists to make it trustworthy:
closed candles only for alerts, visible warm-up, deduped alerts, and a live
preview of the candle that is still forming.

Indicators are kept incrementally (``TdiTracker``), so the cost per candle is a
few hundred operations no matter how many bars are in memory - a bot that lags
behind the market is worse than no bot.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .buckets import TIMEFRAMES, Rollup, merge
from .indicators import TdiPoint, TdiTracker, break_signal, crossed_above, crossed_below, tdi

MAX_CANDLES = 300


@dataclass
class Cell:
    """One instrument on one timeframe."""

    symbol: str
    timeframe: str
    settings: dict[str, Any]
    candles: deque = field(default_factory=lambda: deque(maxlen=MAX_CANDLES))
    tracker: TdiTracker = field(init=False)
    rollup: Rollup | None = None
    forming: dict | None = None
    last_close_time: int = 0
    last_hvb: str | None = None
    last_hvb_time: int = 0
    last_break: str | None = None
    last_break_time: int = 0
    alerts: int = 0
    preview: dict | None = None

    def __post_init__(self) -> None:
        self.tracker = TdiTracker(
            rsi_period=int(self.settings.get("rsi_period", 50)),
            band_period=int(self.settings.get("band_period", 50)),
            fast_period=int(self.settings.get("fast_period", 1)),
            slow_period=int(self.settings.get("slow_period", 9)),
            band_mult=float(self.settings.get("band_mult", 1.6185)),
        )

    def as_dict(self) -> dict[str, Any]:
        last = self.candles[-1] if self.candles else None
        point = self.tracker.last
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": len(self.candles),
            "warm": point is not None and len(self.candles) >= 120,
            "price": last["close"] if last else None,
            "last_close_time": self.last_close_time,
            "hvb": self.last_hvb,
            "hvb_time": self.last_hvb_time,
            "break": self.last_break,
            "break_time": self.last_break_time,
            "alerts": self.alerts,
            "preview": self.preview,
            "fast": round(point.fast, 2) if point and point.fast is not None else None,
            "mbl": round(point.mbl, 2) if point and point.mbl is not None else None,
        }


class Engine:
    def __init__(
        self,
        *,
        symbols: list[str],
        timeframes: list[str],
        tdi_settings: dict[str, Any],
        break_lookback: int = 20,
        hvb_min_points: float = 0.0,
        broadcast: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.timeframes = [tf for tf in timeframes if tf in TIMEFRAMES]
        self.tdi_settings = tdi_settings
        self.break_lookback = int(break_lookback)
        self.hvb_min_points = float(hvb_min_points)
        self.broadcast = broadcast

        self.cells: dict[tuple[str, str], Cell] = {}
        for symbol in self.symbols:
            for tf in self.timeframes:
                self.cells[(symbol, tf)] = Cell(
                    symbol=symbol,
                    timeframe=tf,
                    settings=tdi_settings,
                    rollup=Rollup(tf) if tf != "1m" else None,
                )

        self.alerts: deque[dict] = deque(maxlen=300)
        self.seen: set[tuple] = set()
        self.candles_seen = 0
        self.started_at = time.time()
        self.source = "live"
        self.min_bars = int(tdi_settings.get("rsi_period", 50)) + int(tdi_settings.get("band_period", 50)) + 10
        self.last_event_at = time.time()
        self._last_state_at = 0.0
        self.state_interval = 0.25  # at most 4 full snapshots per second

    # ------------------------------------------------------------- helpers
    async def _emit(self, message: dict) -> None:
        if self.broadcast is not None:
            await self.broadcast(message)

    def restrict_symbols(self, available: list[str]) -> None:
        """Replay feeds only carry some instruments - do not show phantom rows."""
        keep = {s.upper() for s in available}
        self.symbols = [s for s in self.symbols if s in keep]
        self.cells = {k: v for k, v in self.cells.items() if k[0] in keep}

    # -------------------------------------------------------------- warmup
    def seed(self, symbol: str, timeframe: str, candles: list[dict]) -> None:
        """Prime a cell with historical closed candles (live mode)."""
        cell = self.cells.get((symbol.upper(), timeframe))
        if cell is None:
            return
        for candle in candles:
            if candle.get("closed", True):
                self._append(cell, {**candle, "closed": True})

    def _append(self, cell: Cell, bar: dict) -> TdiPoint | None:
        cell.candles.append(bar)
        cell.last_close_time = int(bar["close_time"])
        return cell.tracker.add(float(bar["close"]))

    async def warmup(self, feed) -> None:
        """Ask the feed for history per timeframe; replay feeds warm up by playing."""
        for symbol in self.symbols:
            for tf in self.timeframes:
                try:
                    candles = await feed.history(symbol, tf, 500)
                except Exception as exc:  # one bad symbol must not kill the bot
                    await self._emit(
                        {"type": "log", "level": "warn", "message": f"warm-up {symbol} {tf}: {exc}"}
                    )
                    continue
                if candles:
                    self.seed(symbol, tf, candles)

            # The timeframe candles that are still forming must start at the
            # beginning of their period, not at the moment the bot booted -
            # otherwise the first 15m/1h candle it closes is a partial one.
            try:
                minutes = await feed.history(symbol, "1m", 200)
            except Exception:
                minutes = []
            if minutes:
                for tf in self.timeframes:
                    cell = self.cells.get((symbol, tf))
                    if cell is None or cell.rollup is None:
                        continue
                    self._prime_partial(cell, minutes)

        await self._emit_state(force=True)

    def _prime_partial(self, cell: Cell, minutes: list[dict]) -> None:
        """Feed only the 1m bars of the period in progress into the rollup."""
        step = TIMEFRAMES[cell.timeframe]
        last_start = (int(minutes[-1]["open_time"]) // step) * step
        for bar in minutes:
            if int(bar["open_time"]) >= last_start:
                cell.rollup.add({**bar, "closed": True})

    # ------------------------------------------------------------- candles
    async def on_candle(self, candle: dict) -> None:
        symbol = candle["symbol"].upper()

        # The forming candle is only used for the live preview - never for alerts.
        if not candle.get("closed", True):
            self._preview(symbol, candle)
            return

        self.candles_seen += 1
        self.last_event_at = time.time()

        for tf in self.timeframes:
            cell = self.cells.get((symbol, tf))
            if cell is None:
                continue
            if tf == "1m":
                completed = [dict(candle, timeframe="1m", closed=True)]
            else:
                completed = cell.rollup.add(candle) if cell.rollup else []
            for bar in completed:
                point = self._append(cell, bar)
                if point is not None:
                    await self._evaluate(cell, bar, point)

        await self._emit_state()

    async def _emit_state(self, force: bool = False) -> None:
        """Alerts go out instantly; the full table is coalesced.

        A fast feed (or a replay) can produce hundreds of candles per second -
        sending the whole table for each one would melt both ends of the socket.
        """
        now = time.monotonic()
        if not force and now - self._last_state_at < self.state_interval:
            return
        self._last_state_at = now
        await self._emit({"type": "state", **self.snapshot()})

    # ------------------------------------------------------------ evaluate
    async def _evaluate(self, cell: Cell, bar: dict, point: TdiPoint) -> None:
        if len(cell.candles) < self.min_bars:
            return
        prev = cell.tracker.prev
        hvb = None
        if prev is not None:
            if crossed_above(prev.fast, prev.mbl, point.fast, point.mbl) and (
                self.hvb_min_points <= 0 or (point.fast - point.mbl) >= self.hvb_min_points
            ):
                hvb = "up"
            elif crossed_below(prev.fast, prev.mbl, point.fast, point.mbl) and (
                self.hvb_min_points <= 0 or (point.mbl - point.fast) >= self.hvb_min_points
            ):
                hvb = "down"

        brk = break_signal(list(cell.candles), len(cell.candles) - 1, self.break_lookback)

        if hvb:
            cell.last_hvb, cell.last_hvb_time = hvb, int(bar["close_time"])
        if brk:
            cell.last_break, cell.last_break_time = brk, int(bar["close_time"])
        if not (hvb or brk):
            return

        aligned = (hvb == "up" and brk == "high") or (hvb == "down" and brk == "low")
        kind = "signal" if aligned else ("hvb" if hvb else "break")
        key = (cell.symbol, cell.timeframe, int(bar["close_time"]), kind)
        if key in self.seen:
            return
        self.seen.add(key)

        alert = {
            "symbol": cell.symbol,
            "timeframe": cell.timeframe,
            "time": int(bar["close_time"]),
            "price": bar["close"],
            "hvb": hvb,
            "break": brk,
            "kind": kind,
            "side": "LONG" if (hvb == "up" or brk == "high") else "SHORT",
            "fast": round(point.fast, 2) if point.fast is not None else None,
            "slow": round(point.slow, 2) if point.slow is not None else None,
            "mbl": round(point.mbl, 2) if point.mbl is not None else None,
            "upper": round(point.upper, 2) if point.upper is not None else None,
            "lower": round(point.lower, 2) if point.lower is not None else None,
        }
        if kind == "signal":
            cell.alerts += 1
        self.alerts.appendleft(alert)
        await self._emit({"type": "alert", "alert": alert})

    # ------------------------------------------------------------- preview
    def _preview(self, symbol: str, candle: dict) -> None:
        """Evaluate the still-forming candle so the UI shows it live."""
        for tf in self.timeframes:
            cell = self.cells.get((symbol, tf))
            if cell is None or not cell.candles:
                continue
            start = (int(candle["open_time"]) // TIMEFRAMES[tf]) * TIMEFRAMES[tf]
            preview = dict(candle)
            preview["open_time"] = start
            if cell.forming and cell.forming.get("open_time") == start:
                preview = merge(cell.forming, candle)
                preview["open_time"] = start
            cell.forming = preview
            if len(cell.candles) < 120:
                continue

            # The batch indicator over a short tail is cheap and cannot drift.
            tail = list(cell.candles)[-160:] + [preview]
            points = tdi(
                [c["close"] for c in tail],
                int(self.tdi_settings.get("rsi_period", 50)),
                int(self.tdi_settings.get("band_period", 50)),
                int(self.tdi_settings.get("fast_period", 1)),
                int(self.tdi_settings.get("slow_period", 9)),
                float(self.tdi_settings.get("band_mult", 1.6185)),
            )
            index = len(tail) - 1
            prev, now = points[index - 1], points[index]
            hvb = None
            if crossed_above(prev.fast, prev.mbl, now.fast, now.mbl):
                hvb = "up"
            elif crossed_below(prev.fast, prev.mbl, now.fast, now.mbl):
                hvb = "down"
            cell.preview = {
                "hvb": hvb,
                "break": break_signal(tail, index, self.break_lookback),
                "price": preview["close"],
                "time": int(preview["open_time"]),
            }

    # ------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "settings": self.tdi_settings,
            "break_lookback": self.break_lookback,
            "hvb_min_points": self.hvb_min_points,
            "cells": [cell.as_dict() for cell in self.cells.values()],
            "alerts": list(self.alerts)[:80],
            "candles_seen": self.candles_seen,
            "last_candle_age": round(time.time() - self.last_event_at, 1),
            "uptime": round(time.time() - self.started_at, 1),
        }
