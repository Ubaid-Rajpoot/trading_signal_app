"""The scanner: keeps every symbol/timeframe up to date and shouts when a
complete setup appears.

A complete setup is the drawing:

    1. a swing high (or low) is liquidity
    2. price pokes through it                    -> the liquidity is taken
    3. price then closes through the OPPOSITE swing, on the other side
                                                 -> the reversal is confirmed
    4. the TDIGM purple line crosses the centre band (HVB) -> momentum agrees

Step 2 and 3 are the structure, step 4 is the trader's TDIGM (50, 50, 1, 9).
Everything the trader switched on in ``filters`` must also agree, and every
rejection is counted so the page can explain the quiet.

Alerts only ever come from CLOSED candles. The candle that is still forming is
only used for the live preview, and previews are throttled because a full
universe of instruments would otherwise melt the process.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .buckets import TIMEFRAMES, Rollup, merge
from .filters import Filters
from .indicators import TdiPoint, TdiTracker, break_signal, centre_of, crossed_above, crossed_below, hvb_signal, line_speed, tdi
from .regime import RegimePoint, RegimeTracker
from .structure import Structure

MAX_CANDLES = 180


@dataclass
class Cell:
    """One instrument on one timeframe."""

    symbol: str
    timeframe: str
    settings: dict[str, Any]
    structure_settings: dict[str, Any]
    candles: deque = field(default_factory=lambda: deque(maxlen=MAX_CANDLES))
    tracker: TdiTracker = field(init=False)
    structure: Structure = field(init=False)
    regime: RegimeTracker = field(init=False)
    regime_point: RegimePoint = field(init=False)
    regime_limits: tuple[float, float] = (22.0, 0.30)
    rollup: Rollup | None = None
    forming: dict | None = None
    last_close_time: int = 0
    last_hvb: str | None = None
    last_hvb_time: int = 0
    last_break: str | None = None
    last_break_time: int = 0
    alerts: int = 0
    preview: dict | None = None
    preview_at: float = 0.0
    ema: float | None = None
    ema_bars: int = 0
    last_setup: dict | None = None

    def __post_init__(self) -> None:
        self.tracker = TdiTracker(
            rsi_period=int(self.settings.get("rsi_period", 50)),
            band_period=int(self.settings.get("band_period", 50)),
            fast_period=int(self.settings.get("fast_period", 1)),
            slow_period=int(self.settings.get("slow_period", 9)),
            band_mult=float(self.settings.get("band_mult", 1.6185)),
        )
        self.structure = Structure(self.structure_settings)
        self.regime = RegimeTracker()
        self.regime_point = RegimePoint(None, None, None, None, None, None, None)

    # ------------------------------------------------------------ indicators
    def update_ema(self, close: float, period: int) -> None:
        if self.ema is None:
            self.ema, self.ema_bars = close, 1
            return
        self.ema_bars += 1
        if self.ema_bars <= period:
            self.ema += (close - self.ema) / self.ema_bars
        else:
            k = 2.0 / (period + 1.0)
            self.ema += k * (close - self.ema)

    @property
    def trend(self) -> int | None:
        last = self.candles[-1]["close"] if self.candles else None
        if last is None or self.ema is None:
            return None
        return 1 if last >= self.ema else -1

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
            "trend": self.trend,
            "adx": _r(self.regime_point.adx),
            "er": round(self.regime_point.er, 3) if self.regime_point.er is not None else None,
            "regime": self.regime_point.state(*self.regime_limits),
            "regime_dir": self.regime_point.direction,
            "armed": self.structure.armed(last["close"] if last else None),
            "setups": self.structure.setups,
            "invalidated": self.structure.invalidated,
        }


class Engine:
    def __init__(
        self,
        *,
        symbols: list[str],
        timeframes: list[str],
        tdi_settings: dict[str, Any],
        break_lookback: int = 20,
        hvb_min_points: float = 1.0,
        filters: dict[str, Any] | None = None,
        structure: dict[str, Any] | None = None,
        broadcast: Callable[[dict], Awaitable[None]] | None = None,
        preview_max_symbols: int = 200,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.timeframes = [tf for tf in timeframes if tf in TIMEFRAMES]
        self.tdi_settings = tdi_settings
        self.centre_reference = str(tdi_settings.get("hvb_reference", "mbl"))
        self.break_lookback = int(break_lookback)
        self.hvb_min_points = float(hvb_min_points)
        self.hvb_min_speed = 0.0
        self.structure_settings = dict(structure or {})
        self.broadcast = broadcast
        self.filters = Filters(filters or {})
        self.preview_max_symbols = int(preview_max_symbols)
        self.exchange = "binance"

        self.cells: dict[tuple[str, str], Cell] = {}
        for symbol in self.symbols:
            self._add_cells(symbol)

        self.alerts: deque[dict] = deque(maxlen=400)
        self.signals: deque[dict] = deque(maxlen=100)  # setups survive the noise
        self.seen: set[tuple] = set()
        self.kinds: dict[str, int] = {}
        self.candles_seen = 0
        self.started_at = time.time()
        self.source = "live"
        self.min_bars = int(tdi_settings.get("rsi_period", 50)) + int(tdi_settings.get("band_period", 50)) + 10
        self.last_event_at = time.time()
        self._last_state_at = 0.0
        self.state_interval = self._interval_for(len(self.symbols))
        self.cooldowns: dict[tuple[str, str], float] = {}

    @staticmethod
    def _interval_for(count: int) -> float:
        """How often the full table may be pushed.

        Four times a second is right for a handful of instruments; for a
        thousand-instrument universe the same rate would be megabytes of JSON
        per second, so the table is coalesced harder while alerts stay instant.
        """
        if count <= 60:
            return 0.25
        if count <= 300:
            return 1.0
        return 2.0

    # ------------------------------------------------------------- helpers
    def _add_cells(self, symbol: str) -> None:
        for tf in self.timeframes:
            key = (symbol, tf)
            if key in self.cells:
                continue
            self.cells[key] = Cell(
                symbol=symbol,
                timeframe=tf,
                settings=self.tdi_settings,
                structure_settings=self.structure_settings,
                rollup=Rollup(tf) if tf != "1m" else None,
                regime_limits=(
                    float(self.filters.config.get("regime_adx_min") or 0),
                    float(self.filters.config.get("regime_er_min") or 0),
                ),
            )

    async def _emit(self, message: dict) -> None:
        if self.broadcast is not None:
            await self.broadcast(message)

    # ------------------------------------------------------------- symbols
    def add_symbols(self, symbols: list[str]) -> list[str]:
        added = []
        for raw in symbols:
            symbol = str(raw).strip().upper()
            if not symbol or symbol in self.symbols:
                continue
            self.symbols.append(symbol)
            self._add_cells(symbol)
            added.append(symbol)
        if added:
            self.state_interval = self._interval_for(len(self.symbols))
        return added

    def remove_symbols(self, symbols: list[str]) -> list[str]:
        drop = {str(s).upper() for s in symbols}
        removed = [s for s in self.symbols if s in drop]
        if removed:
            self.symbols = [s for s in self.symbols if s not in drop]
            self.cells = {k: v for k, v in self.cells.items() if k[0] not in drop}
            self.state_interval = self._interval_for(len(self.symbols))
        return removed

    def restrict_symbols(self, available: list[str]) -> None:
        """Replay feeds only carry some instruments - do not show phantom rows."""
        keep = {s.upper() for s in available}
        self.symbols = [s for s in self.symbols if s in keep]
        self.cells = {k: v for k, v in self.cells.items() if k[0] in keep}

    def set_filters(self, filters: dict[str, Any]) -> None:
        self.filters = Filters(filters or {})
        # the HVB gates live in the filter config: one source of truth
        self.hvb_min_points = float(self.filters.config.get("hvb_min_points", self.hvb_min_points) or 0)
        self.hvb_min_speed = float(self.filters.config.get("hvb_min_speed", self.hvb_min_speed) or 0)
        limits = (
            float(self.filters.config.get("regime_adx_min") or 0),
            float(self.filters.config.get("regime_er_min") or 0),
        )
        for cell in self.cells.values():
            cell.regime_limits = limits

    def set_centre(self, reference: str) -> str:
        """Which line is "the centre band" the price line must cross."""
        self.centre_reference = reference if reference in {"mbl", "fifty"} else "mbl"
        self.tdi_settings = {**self.tdi_settings, "hvb_reference": self.centre_reference}
        return self.centre_reference

    def set_structure(self, settings: dict[str, Any]) -> None:
        """Apply new structure settings to every cell that is already warm.

        Rebuilding replays each cell's own closed candles through the new
        detector, so the levels are the ones the new setting would have found -
        not an empty slate that needs an hour of warm-up.
        """
        self.structure_settings = dict(settings or {})
        for cell in self.cells.values():
            fresh = Structure(self.structure_settings)
            for bar in cell.candles:
                fresh.add(bar)
            cell.structure = fresh

    # -------------------------------------------------------------- warmup
    def seed(self, symbol: str, timeframe: str, candles: list[dict]) -> None:
        """Prime a cell with historical closed candles."""
        cell = self.cells.get((symbol.upper(), timeframe))
        if cell is None:
            return
        for candle in candles:
            if candle.get("closed", True):
                self._append(cell, {**candle, "closed": True})

    def _append(self, cell: Cell, bar: dict) -> TdiPoint | None:
        cell.candles.append(bar)
        cell.last_close_time = int(bar["close_time"])
        point = cell.tracker.add(float(bar["close"]))
        cell.update_ema(float(bar["close"]), int(self.filters.config.get("trend_ema", 50)))
        cell.regime_point = cell.regime.add(bar)
        cell.structure.add(bar)
        return point

    async def warm_symbol(self, feed, symbol: str) -> None:
        """Load enough history for one instrument (also used when adding one live)."""
        symbol = symbol.upper()
        for tf in self.timeframes:
            cell = self.cells.get((symbol, tf))
            if cell is None or len(cell.candles) >= 120:
                continue
            interval = feed.exchange_interval(tf) if hasattr(feed, "exchange_interval") else tf
            if interval is None:  # exchange has no such candle: build it from 1m
                await self._warm_from_minutes(feed, symbol, tf)
                continue
            try:
                candles = await feed.history(symbol, interval, 600)
            except Exception as exc:
                if await self._warm_from_minutes(feed, symbol, tf):
                    continue
                await self._emit(
                    {"type": "log", "level": "warn", "message": f"warm-up {symbol} {tf}: {exc}"}
                )
                continue
            if candles:
                self.seed(symbol, tf, candles)

        # the candle that is still forming has to start at the beginning of its
        # period, otherwise the first 15m/1h candle after a restart is a stub
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

    async def _warm_from_minutes(self, feed, symbol: str, tf: str) -> bool:
        """Fall back to rolling 1m history up when the exchange has no such candle."""
        cell = self.cells.get((symbol, tf))
        if cell is None:
            return False
        minutes = int(TIMEFRAMES[tf] / 60_000)
        need = min(1000, (self.min_bars + 5) * minutes)
        try:
            bars = await feed.history(symbol, "1m", need)
        except Exception:
            return False
        if not bars:
            return False
        rollup = Rollup(tf)
        for bar in bars:
            for done in rollup.add(bar):
                self._append(cell, done)
        return True

    async def warmup(self, feed) -> None:
        """History for every instrument. One bad symbol must not kill the bot."""
        for symbol in list(self.symbols):
            await self.warm_symbol(feed, symbol)
        await self._emit_state(force=True)

    def _prime_partial(self, cell: Cell, minutes: list[dict]) -> None:
        """Feed only the 1m bars of the period in progress into the rollup."""
        step = TIMEFRAMES[cell.timeframe]
        last_start = (int(minutes[-1]["open_time"]) // step) * step
        for bar in minutes:
            if int(bar["open_time"]) >= last_start:
                cell.rollup.add({**bar, "closed": True})  # type: ignore[union-attr]

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
        hvb_points = 0.0
        centre_cross = None
        hvb_speed = None
        if prev is not None:
            before = centre_of(prev, self.centre_reference)
            centre = centre_of(point, self.centre_reference)
            if before is not None and centre is not None:
                if crossed_above(prev.fast, before, point.fast, centre):
                    centre_cross = "up"
                elif crossed_below(prev.fast, before, point.fast, centre):
                    centre_cross = "down"
                if centre_cross:
                    hvb_points = (point.fast - centre) if centre_cross == "up" else (centre - point.fast)
                    hvb_speed = line_speed(cell.tracker.history, len(cell.tracker.history) - 1)
                    enough_points = self.hvb_min_points <= 0 or hvb_points >= self.hvb_min_points
                    enough_speed = self.hvb_min_speed <= 0 or (hvb_speed is not None and hvb_speed >= self.hvb_min_speed)
                    if enough_points and enough_speed:
                        hvb = centre_cross

        brk = break_signal(list(cell.candles), len(cell.candles) - 1, self.break_lookback)
        structure = cell.structure
        setup = structure.last_setup if structure.last_setup and structure.last_setup.index == structure.count - 1 else None

        if hvb:
            cell.last_hvb, cell.last_hvb_time = hvb, int(bar["close_time"])
        if brk:
            cell.last_break, cell.last_break_time = brk, int(bar["close_time"])

        base = {
            "symbol": cell.symbol,
            "timeframe": cell.timeframe,
            "time": int(bar["close_time"]),
            "price": bar["close"],
            "fast": _r(point.fast),
            "slow": _r(point.slow),
            "mbl": _r(point.mbl),
            "centre": _r(centre_of(point, self.centre_reference)),
            "centre_reference": self.centre_reference,
            "centre_label": "middle of bands" if self.centre_reference == "mbl" else "50 midline",
            "upper": _r(point.upper),
            "lower": _r(point.lower),
            "trend": cell.trend,
            "regime": self._regime_dict(cell),
            "armed": structure.armed(bar["close"]),
        }

        if setup is not None:
            candidate = {
                **base,
                "tdi_tail": self._tdi_tail(cell, point),
                "kind": "signal",
                "side": setup.side,
                "hvb": hvb,
                "centre_cross": centre_cross,
                "hvb_points": round(abs(hvb_points), 2),
                "hvb_speed": _r(hvb_speed),
                "break": brk,
                "structure": True,
                "rr": setup.rr,
                "bars_since_sweep": setup.index - setup.sweep.index,
                "setup": setup.as_dict(),
            }
            ok, reasons = self._apply_filters(cell, candidate, bar)
            if ok:
                candidate["filtered"] = []
                cell.last_setup = setup.as_dict()
                cell.alerts += 1
                self.cooldowns[(cell.symbol, cell.timeframe)] = time.time()
                await self._publish(cell, candidate, bar)
            else:
                await self._emit(
                    {
                        "type": "filtered",
                        "alert": {**candidate, "filtered": reasons},
                    }
                )
            # the setup is reported once; partials below stay quiet for this bar
            return

        if structure.sweep is not None and structure.sweep.index == structure.count - 1 and (
            structure.events.get("sweeps")
        ):
            await self._publish(
                cell,
                {
                    **base,
                    "kind": "sweep",
                    "side": "SHORT" if structure.sweep.side == "high" else "LONG",
                    "hvb": hvb,
                    "break": brk,
                    "filtered": [],
                },
                bar,
            )
            return

        if not (hvb or brk):
            return

        side = "LONG" if (hvb == "up" or brk == "high") else "SHORT"
        if side.lower() not in self.filters.sides:
            return
        await self._publish(
            cell,
            {
                **base,
                "kind": "hvb" if hvb else "break",
                "side": side,
                "hvb": hvb,
                "hvb_points": round(abs(hvb_points), 2),
                "break": brk,
                "filtered": [],
            },
            bar,
        )

    def _apply_filters(self, cell: Cell, candidate: dict, bar: dict) -> tuple[bool, list[str]]:
        cooldown = float(self.filters.config.get("cooldown_minutes") or 0)
        if cooldown > 0:
            last = self.cooldowns.get((cell.symbol, cell.timeframe))
            if last is not None and (time.time() - last) < cooldown * 60:
                self.filters.rejections["cooldown"] = self.filters.rejections.get("cooldown", 0) + 1
                return False, [f"cooldown ({cooldown:g} min) after the last signal"]
        minute = (int(bar["close_time"]) // 60_000) % 1440
        return self.filters.apply(
            candidate, trend=cell.trend, minute_of_day=minute, regime=candidate.get("regime")
        )

    async def _publish(self, cell: Cell, alert: dict, bar: dict) -> None:
        kind = alert["kind"]
        key = (cell.symbol, cell.timeframe, int(bar["close_time"]), kind)
        if key in self.seen:
            return
        self.seen.add(key)
        if len(self.seen) > 20_000:
            self.seen.clear()
        self.kinds[kind] = self.kinds.get(kind, 0) + 1
        self.alerts.appendleft(alert)
        if kind == "signal":
            self.signals.appendleft(alert)
        await self._emit({"type": "alert", "alert": alert})

    # ------------------------------------------------------------- preview
    def _preview(self, symbol: str, candle: dict) -> None:
        """Evaluate the still-forming candle so the UI shows it live."""
        if len(self.symbols) > self.preview_max_symbols:
            return  # a full universe is too big for per-tick recomputation
        now = time.monotonic()
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
            if len(cell.candles) < 120 or now - cell.preview_at < 1.5:
                continue
            cell.preview_at = now

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
            hvb = hvb_signal(points, index, self.hvb_min_points, self.centre_reference)
            cell.preview = {
                "hvb": hvb,
                "break": break_signal(tail, index, self.break_lookback),
                "price": preview["close"],
                "time": int(preview["open_time"]),
            }

    @staticmethod
    def _regime_dict(cell: Cell) -> dict[str, Any]:
        point = cell.regime_point
        adx_min, er_min = cell.regime_limits
        return {
            "state": point.state(adx_min, er_min),
            "adx": _r(point.adx),
            "er": round(point.er, 3) if point.er is not None else None,
            "direction": point.direction,
            "ema_fast": _r(point.ema_fast),
            "ema_slow": _r(point.ema_slow),
            "adx_min": adx_min,
            "er_min": er_min,
        }

    def _tdi_tail(self, cell: Cell, point: TdiPoint) -> list[list[float]]:
        """The last few candles of the indicator, so the page can draw the cross.

        [price line, centre band] per candle - enough for a 40-bar sparkline and
        enough for the trader to see that the cross really happens at the band.
        """
        tail = list(cell.tracker.history)[-40:]
        return [[round(p.fast, 2), round(centre_of(p, self.centre_reference) or 0.0, 2)] for p in tail if p.fast is not None]

    # ------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "exchange": self.exchange,
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "settings": self.tdi_settings,
            "centre_reference": self.centre_reference,
            "break_lookback": self.break_lookback,
            "hvb_min_points": self.hvb_min_points,
            "hvb_min_speed": self.hvb_min_speed,
            "structure_settings": self.structure_settings,
            "filters": self.filters.summary(),
            "kinds": dict(self.kinds),
            "cells": [cell.as_dict() for cell in self.cells.values()],
            "alerts": list(self.alerts)[:40],
            "signals": list(self.signals)[:60],
            "candles_seen": self.candles_seen,
            "last_candle_age": round(time.time() - self.last_event_at, 1),
            "uptime": round(time.time() - self.started_at, 1),
            "preview": len(self.symbols) <= self.preview_max_symbols,
        }


def _r(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None
