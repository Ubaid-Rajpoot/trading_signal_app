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

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .buckets import TIMEFRAMES, Rollup, merge
from .confluence import (
    Confluence,
    dealing_range,
    equal_extremes,
    failing_swings,
    liquidity_pools,
    pools_ahead,
    fair_value_gaps,
    position_in_range,
    zone_of,
)
from .filters import Filters
from .indicators import TdiPoint, TdiTracker, break_signal, centre_of, crossed_above, crossed_below, hvb_signal, line_speed, tdi
from .regime import RegimePoint, RegimeTracker
from .book.narrative import BookSettings
from .book.runner import BookRunner
from .setups import SetupRecord, order_block
from .store import SignalStore
from .structure import Structure
from .trend import TrendMap

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
    tmap: TrendMap = field(init=False)      # multi-degree structure (bot/trend.py)
    tmap_state: dict = field(default_factory=dict)
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
    pending: dict | None = None   # a confirmed setup waiting for its HVB

    def __post_init__(self) -> None:
        self.tracker = TdiTracker(
            rsi_period=int(self.settings.get("rsi_period", 50)),
            band_period=int(self.settings.get("band_period", 50)),
            fast_period=int(self.settings.get("fast_period", 1)),
            slow_period=int(self.settings.get("slow_period", 9)),
            band_mult=float(self.settings.get("band_mult", 1.6185)),
        )
        self.structure = Structure(self.structure_settings)
        self.tmap = TrendMap(self.trend_settings(self.structure_settings))
        self.regime = RegimeTracker()
        self.regime_point = RegimePoint(None, None, None, None, None, None, None)

    @staticmethod
    def trend_settings(structure_settings: dict[str, Any]) -> dict[str, Any]:
        """One place maps the structure settings onto the three degrees."""
        return {
            "minor_len": int(structure_settings.get("pivot_len", 5) or 5),
            "major_len": int(structure_settings.get("major_len", 20) or 20),
            "macro_len": int(structure_settings.get("macro_len", 80) or 80),
            "hh_tolerance_atr": float(structure_settings.get("hh_tolerance_atr", 0.10) or 0),
            "external_degree": str(structure_settings.get("external_degree", "major") or "major"),
        }

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

    def summary(self) -> dict[str, Any]:
        """A light row: what the table needs for thousands of cells.

        The full trend map (three degrees of pivots, ~1.5 kB) belongs on the
        setup card and in /api/chart, not on every row of a 3,500-cell table.
        """
        last = self.candles[-1] if self.candles else None
        point = self.tracker.last
        external = (self.tmap_state or {}).get("external") or {}
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
            "regime_state": (
                self.regime_point.state(*self.regime_limits) if self.regime_point else None
            ),
            "adx": _r(getattr(self.regime_point, "adx", None)),
            "er": _r(getattr(self.regime_point, "er", None)),
            "trend": self.trend,
            "dir": external.get("direction"),
            "clean": external.get("clean"),
            "bos": external.get("bos_direction"),
            "armed": self.structure.armed(last["close"] if last else None) is not None,
        }

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
            "trend_context": self.tmap_state or None,
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
        strategy: str = "book",
        book: dict[str, Any] | None = None,
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
        # every setup that was ever published, plus its live state
        self.registry: dict[str, SetupRecord] = {}
        self.by_cell: dict[tuple[str, str], list[SetupRecord]] = {}
        self.setup_payloads: dict[str, dict] = {}
        self.setup_events: list[dict] = []   # drained by the server and pushed
        self._quiet = 0            # >0 while replaying history: collect, do not shout
        self._collected: list[dict] = []
        self.backfilled = 0
        self.warm_failures: dict[str, int] = {}
        self.store: SignalStore | None = None
        self.market = "futures"
        # ---- which decision engine runs (round 21: the book's, by default)
        self.strategy = "book" if str(strategy).lower() in {"book", "narrative", "v6"} else "sweep"
        self.book_config = dict(book or {})
        self.book: BookRunner | None = None
        self._seeding = False
        if self.strategy == "book":
            self._build_book()

    # ------------------------------------------------------------------ book
    def _build_book(self) -> None:
        """(Re)build the narrative engine around the current universe."""
        settings = BookSettings.from_config(self.book_config,
                                            entry_tf=self.timeframes[0])
        self.book = BookRunner(self.symbols, self.timeframes, settings)
        # how much 1m history the narrative replays at boot: the liquidity map
        # needs days, not the 45 minutes the old sweep cells needed
        self.book_warm_bars = max(500, int(self.book_config.get("warm_bars", 4000)))

    def set_strategy(self, strategy: str, book: dict[str, Any] | None = None) -> str:
        self.strategy = "book" if str(strategy).lower() in {"book", "narrative", "v6"} else "sweep"
        if book is not None:
            self.book_config = dict(book)
        if self.strategy == "book" and self.book is None:
            self._build_book()
        return self.strategy

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
    def add_symbols(self, symbols: list[str]) -> list[str]:  # noqa: D401
        added = []
        for raw in symbols:
            symbol = str(raw).strip().upper()
            if not symbol or symbol in self.symbols:
                continue
            self.symbols.append(symbol)
            self._add_cells(symbol)
            if self.book is not None:
                self.book.add_symbol(symbol)
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
            if self.book is not None:
                for symbol in removed:
                    self.book.states.pop(symbol, None)
            self.state_interval = self._interval_for(len(self.symbols))
        return removed

    def restrict_symbols(self, available: list[str]) -> None:
        """Replay feeds only carry some instruments - do not show phantom rows."""
        keep = {s.upper() for s in available}
        self.symbols = [s for s in self.symbols if s in keep]
        self.cells = {k: v for k, v in self.cells.items() if k[0] in keep}
        if self.book is not None:
            self.book = BookRunner(self.symbols, self.timeframes,
                                   BookSettings.from_config(self.book_config,
                                                            entry_tf=self.timeframes[0]))

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
            trend = TrendMap(Cell.trend_settings(self.structure_settings))
            for bar in cell.candles:
                fresh.add(bar)
                trend.add(bar)
            cell.structure = fresh
            cell.tmap = trend
            cell.tmap_state = trend.state()

    # --------------------------------------------------------------- archive
    def attach_store(self, store: SignalStore | None) -> None:
        """Give the engine a database: every setup is written as it is found."""
        self.store = store

    def load_stored(self, limit: int = 400) -> int:
        """Put the archive on the board before a single candle arrives.

        A restart used to mean an empty screen until the exchange answered - the
        setups found a minute earlier were still in the database but nothing read
        them back. These rows are marked ``stored`` so the page can say "from the
        archive"; a live replay of the same history replaces them in place.
        """
        if self.store is None or not self.store.enabled:
            return 0
        rows = self.store.list(limit=limit)
        for row in rows:
            key = row.get("key")
            if not key or key in self.setup_payloads:
                continue
            self.setup_payloads[key] = row
        return len(rows)

    def _remember(self, payload: dict) -> None:
        """Write-through to the database. Never allowed to break the scanner."""
        if self.store is None:
            return
        try:
            self.store.record(payload, self.market)
        except Exception:  # a disk problem is logged by the store, not raised here
            pass

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
        cell.tmap_state = cell.tmap.add(bar, atr=cell.regime_point.atr)
        return point

    async def warm_symbol(self, feed, symbol: str, attempts: int = 3, force: bool = False) -> int:
        """Load enough history for one instrument, then read the setups it holds.

        Returns the number of cells that still came back empty. Two things matter
        here and both were missing before:

        * a **retry** - one 429 from the exchange used to leave that instrument
          empty for the rest of the session, silently, which is how 500 symbols
          can produce no signals at all;
        * a **replay over the history** afterwards, so a setup that confirmed
          before the app started still exists. Otherwise the only setups the app
          can ever show you are the ones that happen while it is open.
        """
        symbol = symbol.upper()
        empty = 0
        book_mode = self.strategy == "book" and self.book is not None
        for tf in self.timeframes:
            cell = self.cells.get((symbol, tf))
            if cell is None or (len(cell.candles) >= 120 and not force):
                continue
            candles: list[dict] = []
            for attempt in range(attempts):
                interval = feed.exchange_interval(tf) if hasattr(feed, "exchange_interval") else tf
                if interval is None:  # exchange has no such candle: build it from 1m
                    if await self._warm_from_minutes(feed, symbol, tf):
                        candles = []
                        break
                else:
                    try:
                        candles = await feed.history(symbol, interval, 600)
                        if candles:
                            break
                    except Exception as exc:
                        if attempt == attempts - 1:
                            if await self._warm_from_minutes(feed, symbol, tf):
                                candles = []
                                break
                            self.warm_failures[f"{symbol}:{tf}"] = attempt + 1
                            await self._emit(
                                {"type": "log", "level": "warn", "message": f"warm-up {symbol} {tf}: {exc}"}
                            )
                        else:
                            await asyncio.sleep(0.6 * (attempt + 1))
            if candles:
                await self._replay(symbol, tf, candles, evaluate=not book_mode)
                self.warm_failures.pop(f"{symbol}:{tf}", None)
            current = self.cells.get((symbol, tf), cell)
            if len(current.candles) < 120:
                empty += 1
                # silence was the old bug: an instrument with no history looked
                # exactly like an instrument with nothing to report
                self.warm_failures.setdefault(f"{symbol}:{tf}", attempts)
                if current is not None and not current.candles:
                    await self._emit(
                        {
                            "type": "log",
                            "level": "warn",
                            "message": f"no {tf} history for {symbol} - it will be retried",
                        }
                    )
        if book_mode:
            await self._warm_book(feed, symbol, force=force)
        return empty

    async def _warm_book(self, feed, symbol: str, force: bool = False) -> int:
        """Warm the book engine from history, so the page is not empty at boot.

        The narrative needs what a single candle cannot give it: the previous
        day's and week's highs and lows, the session ranges, a higher timeframe
        with real swings, and enough 1m bars for a trap -> displacement ->
        mitigation sequence to have happened already. So we fetch a deep 1m
        window and replay it through **the same** ``BookRunner.on_candle`` the
        live feed uses. A plan found here is stored and shown exactly like a live
        one, marked ``seeded``.
        """
        state = self.book.states.get(symbol) if self.book else None
        if state is None and self.book is not None:
            state = self.book.add_symbol(symbol)
        if state is None:
            return 0
        first = state.narratives.get(self.timeframes[0])
        if first is not None and first.bars and not force:
            return 0

        async def fetch(interval: str, limit: int) -> list[dict]:
            if not hasattr(feed, "history"):
                return []
            try:
                name = feed.exchange_interval(interval) if hasattr(feed, "exchange_interval") else interval
                if name is None:
                    return []
                return await feed.history(symbol, name, limit) or []
            except Exception as exc:
                await self._emit({"type": "log", "level": "warn",
                                  "message": f"warm-up {symbol} {interval}: {exc}"})
                return []

        # deeper history = more of the page filled at boot, but every extra 1,000
        # bars is one more REST page per instrument
        warm_bars = self.book_warm_bars
        if len(self.symbols) > 250:
            warm_bars = min(warm_bars, 1500)
        elif len(self.symbols) > 60:
            warm_bars = min(warm_bars, 4000)
        fine = await fetch("1m", warm_bars)
        if not fine:
            self.warm_failures.setdefault(f"{symbol}:1m", 1)
            return 1

        published = 0
        self._seeding = True
        try:
            for candle in fine:
                for plan in self.book.on_candle({**candle, "symbol": symbol}):
                    if await self._publish_book(plan) is not None:
                        published += 1
        finally:
            self._seeding = False
        self.warm_failures.pop(f"{symbol}:1m", None)
        self.backfilled += published
        await self._emit({"type": "log", "level": "info",
                          "message": f"warm-up {symbol}: {len(fine)} 1m bars replayed, "
                                     f"{published} plan(s) already on the books"})
        return 0

    async def _replay(self, symbol: str, timeframe: str, candles: list[dict],
                      evaluate: bool = True) -> list[dict]:
        """Feed closed history through the live pipeline once, and keep the result.

        This is the same code path as live - a fresh cell fed the same closed
        candles through ``_append``/``_evaluate`` - so a setup found here is not
        a second, approximate definition of the strategy.

        Two details matter:

        * the whole fetched history is replayed (600 bars), not just the 180 the
          cell keeps, so a setup that confirmed 8 hours ago is still found;
        * the lifecycle runs over that history too, so an old setup is reported
          as expired/stopped instead of pretending to still be pending.
        """
        cell = self.cells.get((symbol.upper(), timeframe))
        if cell is None or not candles:
            return []
        fresh = Cell(
            symbol=cell.symbol,
            timeframe=cell.timeframe,
            settings=cell.settings,
            structure_settings=cell.structure_settings,
            rollup=cell.rollup if timeframe != "1m" else None,
            regime_limits=cell.regime_limits,
        )
        self.by_cell[(cell.symbol, cell.timeframe)] = []
        self._quiet += 1
        self._collected = []
        try:
            for bar in candles:
                point = self._append(fresh, bar)
                self._track_setups(fresh, bar)
                if point is None:
                    continue
                if not evaluate:
                    continue
                structure = fresh.structure
                setup = structure.last_setup
                if setup is None or setup.index != structure.count - 1:
                    continue
                await self._evaluate(fresh, bar, point)
        finally:
            self._quiet -= 1
            collected, self._collected = self._collected, []
        fresh.alerts = cell.alerts
        self.cells[(symbol.upper(), timeframe)] = fresh
        if collected:
            self.backfilled += len(collected)
        return collected

    def warming(self) -> int:
        """Cells that still have no usable history - shown while the bot boots."""
        return sum(
            1
            for cell in self.cells.values()
            if cell.tracker.last is None or len(cell.candles) < 120
        )

    async def warmup(self, feed, concurrency: int = 30) -> None:
        """History for every instrument, in parallel, one bad symbol at a time.

        Serial warm-up is what made a 120-instrument universe look dead for
        minutes: 5 timeframes x 120 symbols of REST calls, one after another.
        Here a small pool fetches them at once and the table is pushed after
        each instrument, so rows appear as they land instead of at the end.
        """
        symbols = list(self.symbols)
        if not symbols:
            return
        limit = max(1, min(int(concurrency), len(symbols)))
        gate = asyncio.Semaphore(limit)

        async def one(symbol: str) -> None:
            async with gate:
                try:
                    await self.warm_symbol(feed, symbol)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one bad symbol must not stop the rest
                    await self._emit(
                        {"type": "log", "level": "warn", "message": f"warm-up {symbol}: {exc}"}
                    )
                await self._emit_state()

        await asyncio.gather(*(one(symbol) for symbol in symbols))
        await self._emit_state(force=True)

    def forget_setups(self) -> None:
        """Drop every remembered setup (used before a full re-scan)."""
        self.registry.clear()
        self.setup_payloads.clear()
        self.setup_events.clear()
        self.by_cell.clear()

    async def rescan(
        self, feed, only_missing: bool = False, reset: bool = False, concurrency: int = 30
    ) -> dict[str, Any]:
        """Re-read history and re-run the scan.

        This is what the rescans button does, and what a filter change needs: the
        setups you are looking at must be the ones the *current* rules find.
        """
        if reset:
            self.forget_setups()
        found = 0
        warmed = 0
        gate = asyncio.Semaphore(max(1, int(concurrency)))

        async def one(symbol: str) -> None:
            nonlocal found, warmed
            async with gate:
                cells = [self.cells.get((symbol, tf)) for tf in self.timeframes]
                missing = any(c is None or len(c.candles) < 120 for c in cells)
                if missing or not only_missing:
                    # re-fetch the full window: a cell only *keeps* 180 bars, so
                    # re-scanning the buffer would look at half the history the
                    # first scan saw and quietly lose setups that are still alive
                    await self.warm_symbol(feed, symbol, force=True)
                    warmed += 1

        await asyncio.gather(*(one(symbol) for symbol in self.symbols))
        await self._emit_state(force=True)
        return {"symbols": len(self.symbols), "setups": found, "warmed": warmed}

    async def retry_failed(self, feed) -> int:
        """Try again for any instrument whose history never arrived."""
        todo: list[str] = []
        for symbol in self.symbols:
            cells = [self.cells.get((symbol, tf)) for tf in self.timeframes]
            if any(cell is not None and len(cell.candles) < 120 for cell in cells):
                todo.append(symbol)
        if not todo:
            return 0
        gate = asyncio.Semaphore(30)

        async def one(symbol: str) -> None:
            async with gate:
                try:
                    await self.warm_symbol(feed, symbol)
                except Exception:
                    pass

        await asyncio.gather(*(one(symbol) for symbol in todo))
        return len(todo)

    async def backfill(self, symbol: str) -> list[dict]:
        """Re-derive every timeframe of one instrument from the candles it has."""
        found: list[dict] = []
        for tf in self.timeframes:
            cell = self.cells.get((symbol.upper(), tf))
            if cell is None or len(cell.candles) < self.min_bars:
                continue
            if len(cell.candles) < 120:
                continue
            found.extend(await self._replay(symbol, tf, list(cell.candles)))
        return found

    async def _warm_from_minutes(self, feed, symbol: str, tf: str) -> bool:
        """Fall back to rolling 1m history up when the exchange has no such candle."""
        cell = self.cells.get((symbol, tf))
        if cell is None:
            return False
        minutes = int(TIMEFRAMES[tf] / 60_000)
        # enough 1m bars to fill this timeframe's window - a 1h cell needs
        # 120 hours of minutes, not 1,000 bars (that was 16 candles)
        need = min(60_000, (self.min_bars + 5) * minutes)
        try:
            bars = await feed.history(symbol, "1m", need)
        except Exception:
            return False
        if not bars:
            return False
        rollup = Rollup(tf)
        built: list[dict] = []
        for bar in bars:
            for done in rollup.add(bar):
                built.append(done)
        if not built:
            return False
        await self._replay(symbol, tf, built)
        return True

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

        book = self.book if self.strategy == "book" else None
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
                # first: did this candle change the life of an existing setup?
                self._track_setups(cell, bar)
                if book is not None:
                    # the decision belongs to the narrative engine; the cell is
                    # kept for the chart only (candles, markers, TDI pane)
                    continue
                if point is not None:
                    await self._evaluate(cell, bar, point)

        if book is not None:
            for plan in book.on_candle(candle):
                await self._publish_book(plan)

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

        context = cell.tmap_state or cell.tmap.state()
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
            "context": context,
            "volume_ratio": self._volume_ratio(cell),
        }

        # a setup confirmed earlier and is still waiting for its HVB: does this
        # candle print it? (the TDI cross often trails the structure break by a
        # bar or two, which is why "same candle only" was so sparse)
        if cell.pending is not None:
            await self._confirm_pending(cell, bar, hvb, hvb_points, hvb_speed, centre_cross, base, point)

        if setup is not None:
            book = self._confluence(cell, setup, bar)
            depth = cell.tmap.depth(setup.side, setup.break_kind, setup.index)
            candidate = {
                **base,
                "depth": depth,
                "book": book.as_dict(),
                "break_kind": book.break_kind,
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
                "external_trend": book.external_trend,
                "bars_since_sweep": setup.index - setup.sweep.index,
                "setup": setup.as_dict(),
            }
            candidate["score"], candidate["score_parts"] = self.filters.score_of(candidate)
            window = int(self.filters.config.get("hvb_window") or 0)
            if (
                window > 0
                and not hvb
                and self.filters.config.get("hvb", True)
                and str(self.filters.config.get("confirm_mode") or "all").lower() == "all"
            ):
                # hold it: the setup is real, the HVB has not printed yet
                cell.pending = {
                    "candidate": candidate,
                    "setup": setup.as_dict(),
                    "index": setup.index,
                    "count": cell.structure.count - 1,
                    "side": setup.side,
                }
                # say it out loud: the setup is real, the HVB has not printed yet
                await self._emit(
                    {
                        "type": "filtered",
                        "alert": {
                            **candidate,
                            "filtered": [f"HVB not on the break candle yet - watching {window} more bar(s)"],
                        },
                    }
                )
                return
            candidate["hvb_lag"] = 0 if hvb else None
            candidate["grade"] = grade_of(candidate)
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

    def _markers(self, cell: Cell, candles: list[dict], since: int) -> dict[str, Any]:
        """Where the structure broke and where the TDIGM HVB printed, bar by bar.

        The same functions the live engine uses (``break_signal`` for the break,
        the TDI centre cross for the HVB), applied to the same closed candles -
        so the chart cannot show a break the engine would not have seen. Without
        this the page could only draw markers for a setup that still exists, and
        the trader's actual complaint was "it does not show me where the break
        and the HVB happened".
        """
        if len(candles) < 5:
            return {"bos": [], "hvb": []}
        settings = cell.settings
        points = tdi(
            [float(bar["close"]) for bar in candles],
            int(settings.get("rsi_period", 50)),
            int(settings.get("band_period", 50)),
            int(settings.get("fast_period", 1)),
            int(settings.get("slow_period", 9)),
            float(settings.get("band_mult", 1.6185)),
        )
        bos: list[dict] = []
        hvb: list[dict] = []
        for i in range(1, len(candles)):
            bar = candles[i]
            if int(bar["open_time"]) < since:
                continue
            brk = break_signal(candles, i, self.break_lookback)
            if brk:
                index = i - self.break_lookback if i >= self.break_lookback else 0
                level = max(float(c["high"]) for c in candles[max(0, i - self.break_lookback):i]) if brk == "high" else min(float(c["low"]) for c in candles[max(0, i - self.break_lookback):i])
                bos.append({"t": int(bar["open_time"]), "dir": 1 if brk == "high" else -1, "level": level})
            prev, point = points[i - 1], points[i]
            if prev is None or point is None:
                continue
            before = centre_of(prev, self.centre_reference)
            centre = centre_of(point, self.centre_reference)
            if before is None or centre is None:
                continue
            if crossed_above(prev.fast, before, point.fast, centre):
                hvb.append({"t": int(bar["open_time"]), "side": "up"})
            elif crossed_below(prev.fast, before, point.fast, centre):
                hvb.append({"t": int(bar["open_time"]), "side": "down"})
        return {"bos": bos[-40:], "hvb": hvb[-40:]}

    async def _confirm_pending(
        self,
        cell: Cell,
        bar: dict,
        hvb: str | None,
        hvb_points: float,
        hvb_speed: float | None,
        centre_cross: str | None,
        base: dict,
        point: TdiPoint,
    ) -> None:
        """Publish a held setup the moment its HVB prints, or let it go.

        ``hvb_window`` bars is the whole tolerance: the structure break and the
        TDIGM cross are two different measurements and they do not always land on
        the same candle. The setup keeps the entry/stop it was confirmed with;
        only the HVB fields come from this candle, and ``hvb_lag`` says how late
        it was, so the record stays exact (and the tier is B, not A).
        """
        held = cell.pending
        if held is None:
            return
        window = int(self.filters.config.get("hvb_window") or 0)
        lag = cell.structure.count - 1 - int(held["count"])
        want = "down" if held["side"] == "SHORT" else "up"
        if lag > window:
            cell.pending = None
            self.filters.rejections["no HVB inside the window"] = (
                self.filters.rejections.get("no HVB inside the window", 0) + 1
            )
            return
        if not hvb or hvb != want:
            return
        cell.pending = None
        candidate = dict(held["candidate"])
        candidate.update(
            {
                "time": int(bar["close_time"]),
                "price": bar["close"],
                "hvb": hvb,
                "centre_cross": centre_cross,
                "hvb_points": round(abs(hvb_points), 2),
                "hvb_speed": _r(hvb_speed),
                "hvb_lag": lag,
                "regime": base.get("regime"),
                "armed": base.get("armed"),
                "context": base.get("context"),
                "volume_ratio": base.get("volume_ratio"),
            }
        )
        candidate["score"], candidate["score_parts"] = self.filters.score_of(candidate)
        candidate["grade"] = grade_of(candidate)
        ok, reasons = self._apply_filters(cell, candidate, bar)
        if not ok:
            await self._emit({"type": "filtered", "alert": {**candidate, "filtered": reasons}})
            return
        candidate["filtered"] = []
        cell.alerts += 1
        self.cooldowns[(cell.symbol, cell.timeframe)] = time.time()
        await self._publish(cell, candidate, bar)

    def _volume_ratio(self, cell: Cell) -> float | None:
        """This candle's volume against the average of the bars before it.

        The "high volume bar" reading: 1.0 is an average candle, 2.0 is twice the
        normal participation. Only closed candles are in the deque.
        """
        period = int(self.filters.config.get("volume_period") or 20)
        if period < 2 or len(cell.candles) < period + 1:
            return None
        window = list(cell.candles)[-(period + 1):-1]
        volumes = [float(bar.get("volume") or 0.0) for bar in window]
        average = sum(volumes) / len(volumes)
        if average <= 0:
            return None
        return round(float(cell.candles[-1].get("volume") or 0.0) / average, 3)

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
        if kind == "signal":
            alert = self._register(alert, bar, cell)
            self.signals.appendleft(alert)
        self.alerts.appendleft(alert)
        if self._quiet:
            # replaying history: this already happened, so collect it and let the
            # caller decide. Shouting about yesterday's candles is noise.
            self._collected.append(alert)
            return
        await self._emit({"type": "alert", "alert": alert})

    # ---------------------------------------------------------- book engine
    async def _publish_book(self, plan: dict) -> dict | None:
        """A narrative plan becomes a tracked setup with a life of its own."""
        key = f"{plan['symbol']}|{plan['timeframe']}|{plan['created_ms']}"
        if key in self.registry:
            return self.setup_payloads.get(key)
        record = self._register_book(plan, key)
        payload = self.setup_payloads[key]
        self.signals.appendleft(payload)
        self.alerts.appendleft(payload)
        self.kinds["signal"] = self.kinds.get("signal", 0) + 1
        self.cooldowns[(plan["symbol"], plan["timeframe"])] = time.time()
        if self._quiet:
            self._collected.append(payload)
        else:
            await self._emit({"type": "alert", "alert": payload})
            self.setup_events.append(payload)
        return payload

    def _register_book(self, plan: dict, key: str) -> SetupRecord:
        gap = plan.get("ac") or {}
        zone = {"high": float(gap.get("top") or plan["entry"]),
                "low": float(gap.get("bottom") or plan["entry"]),
                "mid": (float(gap.get("top") or plan["entry"]) + float(gap.get("bottom") or plan["entry"])) / 2,
                "kind": "fvg", "imbalance": gap.get("kind")}
        trap = plan.get("trap") or {}
        record = SetupRecord(
            key=key,
            symbol=plan["symbol"],
            timeframe=plan["timeframe"],
            side=plan["side"],
            created_ms=int(plan["created_ms"]),
            entry=float(plan["entry"]),
            stop=float(plan["stop"]),
            target=float(plan["target"]) if plan.get("target") else None,
            rr=plan.get("rr"),
            zone=zone,
            swept_level=float(trap.get("level") or 0.0),
            swept_side=str(trap.get("side") or ""),
            sweep_ms=int(trap.get("time") or 0),
            bos_level=float((plan.get("shift") or {}).get("level") or 0.0),
            bos_ms=int((plan.get("shift") or {}).get("time") or 0),
            break_kind=str((plan.get("shift") or {}).get("kind") or "MS"),
            volume_ratio=(plan.get("ac") or {}).get("volume_ratio"),
            score=plan.get("score"),
            grade=plan.get("grade"),
            reasons=list(plan.get("confluences") or []),
            # the trade is on the moment the zone is mitigated - that IS the entry
            state="active",
            touched_ms=int(plan["created_ms"]),
            max_bars=int(self.book_config.get("setup_max_bars", 240)),
            breakeven_at_r=float(self.book_config.get("breakeven_at_r", 1.0) or 0.0),
        )
        self.registry[key] = record
        self.by_cell.setdefault((record.symbol, record.timeframe), []).append(record)
        payload = {**plan, **record.as_dict()}
        payload["key"] = key
        payload["kind"] = "signal"
        payload["strategy"] = "book"
        if getattr(self, "_seeding", False):
            # found while replaying history at start-up, not by the live feed
            payload["seeded"] = True
        payload["time"] = int(plan["created_ms"])
        payload["price"] = float(plan["entry"])
        payload["zone"] = record.zone
        self.setup_payloads[key] = payload
        self._remember(payload)
        return record

    # -------------------------------------------------------------- setups
    def _register(self, alert: dict, bar: dict, cell: Cell | None = None) -> dict:
        """Turn a signal alert into a tracked setup with a zone and a state."""
        setup = alert.get("setup") or {}
        side = alert.get("side") or setup.get("side") or "SHORT"
        key = f"{alert['symbol']}|{alert['timeframe']}|{alert['time']}"
        if key in self.registry:
            return self.setup_payloads[key]
        if key in self.setup_payloads and self.setup_payloads[key].get("stored"):
            # a row loaded from the archive: replace it with the live record
            self.setup_payloads.pop(key, None)
        # the cell being evaluated - during a history replay that is the *fresh*
        # cell, not the one in self.cells yet
        source = cell or self.cells.get((alert["symbol"], alert["timeframe"]))
        candles = list(source.candles) if source is not None else []
        record = SetupRecord(
            key=key,
            symbol=alert["symbol"],
            timeframe=alert["timeframe"],
            side=side,
            created_ms=int(alert["time"]),
            entry=float(setup.get("entry") or alert.get("price") or 0.0),
            stop=float(setup.get("stop") or 0.0),
            target=None if setup.get("target") is None else float(setup["target"]),
            rr=setup.get("rr"),
            zone=order_block(candles, side),
            swept_level=float(setup.get("swept_level") or 0.0),
            swept_side=str(setup.get("swept_side") or ""),
            sweep_ms=int(setup.get("sweep_time") or 0),
            bos_level=float(setup.get("broke_level") or 0.0),
            bos_ms=int(setup.get("broke_time") or 0),
            break_kind=str(setup.get("break_kind") or "SHIFT"),
            hvb_ms=int(alert["time"]) if alert.get("hvb") else None,
            hvb_side=alert.get("hvb"),
            volume_ratio=alert.get("volume_ratio"),
            score=alert.get("score"),
            score_parts=alert.get("score_parts") or {},
            depth=alert.get("depth"),
            trend_dir=((alert.get("context") or {}).get("external") or {}).get("direction"),
            trend_clean=((alert.get("context") or {}).get("external") or {}).get("clean"),
            grade=alert.get("grade"),
            reasons=_evidence(alert),
            max_bars=int(self.filters.config.get("setup_max_bars") or 240),
        )
        self.registry[key] = record
        self.by_cell.setdefault((record.symbol, record.timeframe), []).append(record)
        # the alert as the engine built it, plus the living state of the setup:
        # one object, so the feed, the card and the API cannot disagree
        payload = {**alert, **{k: v for k, v in record.as_dict().items() if k not in {"key"}}}
        payload["kind"] = "signal"
        payload["key"] = key
        payload["setup"] = setup
        self.setup_payloads[key] = payload
        self._remember(payload)
        return payload

    def _track_setups(self, cell: Cell, bar: dict) -> None:
        """Every closed bar advances the life of every open setup on that cell."""
        records = self.by_cell.get((cell.symbol, cell.timeframe))
        if not records:
            return
        for record in records:
            if record.state in ("win", "stopped", "expired", "invalidated"):
                continue
            if not record.advance(bar):
                continue
            payload = self.setup_payloads.get(record.key)
            if payload is None:
                continue
            payload.update(record.as_dict())
            payload["valid"] = record.valid
            payload["mitigated"] = record.mitigated
            self._remember(payload)
            if self._quiet:
                continue
            self.setup_events.append(payload)  # the server pushes these out

    def open_setups(self, *, valid_only: bool = False, limit: int = 300) -> list[dict]:
        rows = list(self.setup_payloads.values())
        if valid_only:
            rows = [row for row in rows if row.get("valid")]
        rows.sort(key=lambda row: -row["created_ms"])
        return rows[:limit]

    def setup_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.setup_payloads.values():
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        return counts

    def chart(self, symbol: str, timeframe: str, bars: int = 180) -> dict:
        """Candles plus every marker the setup is made of, for one cell."""
        cell = self.cells.get((symbol.upper(), timeframe))
        if cell is None:
            return {"symbol": symbol, "timeframe": timeframe, "candles": [], "setups": []}
        candles = list(cell.candles)[-max(20, min(400, bars)):]
        first = int(candles[0]["open_time"]) if candles else 0
        rows = []
        for bar in candles:
            rows.append(
                {
                    "t": int(bar["open_time"]),
                    "o": bar["open"],
                    "h": bar["high"],
                    "l": bar["low"],
                    "c": bar["close"],
                    "v": bar.get("volume"),
                }
            )
        setups = [
            row
            for row in self.setup_payloads.values()
            if row["symbol"] == symbol.upper() and row["timeframe"] == timeframe
            and row["created_ms"] >= first
        ]
        setups.sort(key=lambda row: row["created_ms"])
        markers = self._markers(cell, candles, first)
        return {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "candles": rows,
            "markers": markers,
            "setups": setups,
            "pools": self._chart_pools(symbol.upper()),
            "book": self._book_state(symbol.upper(), timeframe),
            "structure": cell.structure.as_dict(),
            "trend": cell.tmap_state or {},
            "warm": len(cell.candles) >= 120,
            "bars": len(cell.candles),
        }

    # ----------------------------------------------------------- book drawing
    def _chart_pools(self, symbol: str) -> list[dict]:
        """The liquidity map, so the chart can draw the levels the trade aimed at."""
        if self.book is None:
            return []
        state = self.book.states.get(symbol)
        if state is None:
            return []
        return [pool.as_dict() for pool in state.liquidity.pools[-24:]]

    def _book_state(self, symbol: str, timeframe: str) -> dict:
        """What the narrative is waiting for right now (trap / armed / nothing)."""
        if self.book is None:
            return {}
        state = self.book.states.get(symbol)
        if state is None:
            return {}
        narrative = state.narratives.get(timeframe)
        if narrative is None:
            return {}
        context = state.contexts.get(timeframe) or {}
        return {**narrative.state(),
                "bias": context.get("bias"),
                "premium": context.get("premium"),
                "htf": context.get("htf")}

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

    def _confluence(self, cell: Cell, setup, bar: dict) -> Confluence:
        """p.42 / p.53 / p.87 applied to THIS setup (only runs when one appears)."""
        candles = list(cell.candles)
        index = len(candles) - 1
        # structure indices are absolute (they count every bar ever seen) while
        # the candle list is a rolling window - map one onto the other
        offset = cell.structure.count - len(candles)

        def local(absolute: int) -> int:
            return max(0, min(index, absolute - offset))
        side = setup.side
        sweep = setup.sweep
        atr = cell.regime_point.atr
        point = Confluence(side=side)

        point.break_kind = setup.break_kind
        point.external_trend = cell.structure.external_trend
        point.swept_label = "strong high" if sweep.side == "high" else "strong low"
        point.broke_label = "weak low" if setup.broke.side == "low" else "weak high"

        bounds = cell.structure.range()
        if bounds:
            point.range_high, point.range_low = bounds["high"], bounds["low"]
            point.range_high_time, point.range_low_time = bounds["high_time"], bounds["low_time"]
            bounds_pair = dealing_range(bounds["high"], bounds["low"])
            point.position = position_in_range(float(bar["close"]), bounds_pair)
            point.zone = zone_of(point.position)
            point.sweep_position = position_in_range(float(sweep.level), bounds_pair)
            point.sweep_zone = zone_of(point.sweep_position)

        # p.87: the displacement that broke structure should leave an imbalance
        wanted = "bearish" if side == "SHORT" else "bullish"
        gaps = [
            gap
            for gap in fair_value_gaps(candles, max(2, local(sweep.index)), index)
            if gap.side == wanted
        ]
        point.fvgs_in_leg = len(gaps)
        if gaps:
            point.fvg_filled = max(gap.filled_fraction(candles, index) for gap in gaps)

        pools = liquidity_pools(candles, lookback=30, tolerance=0.25, atr=atr)
        point.equal_highs = max((count for _, count in pools["highs"]), default=0)
        point.equal_lows = max((count for _, count in pools["lows"]), default=0)
        point.pools_ahead = pools_ahead(pools, side, float(bar["close"]))

        if atr and atr > 0:
            point.structure_atr = abs(sweep.level - setup.broke.price) / atr

        swing = failing_swings(candles, cell.structure.pivot_len)
        point.failure_swing = swing["kind"] if swing else None
        return point

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
            "cells": [cell.summary() for cell in self.cells.values()],
            "alerts": list(self.alerts)[:40],
            "signals": self.open_setups(limit=120),
            "setup_counts": self.setup_counts(),
            "setups_valid": sum(1 for row in self.setup_payloads.values() if row.get("valid")),
            "backfilled": self.backfilled,
            "strategy": self.strategy,
            "book": ({"traps": self.book.traps, "displacements": self.book.armed,
                      "plans": len(self.book.plans), "drops": self.book.drops()}
                     if self.book is not None else {}),
            "archive": (self.store.counts() if self.store else {}),
            "failed": len(self.warm_failures),
            "warming": self.warming(),
            "candles_seen": self.candles_seen,
            "last_candle_age": round(time.time() - self.last_event_at, 1),
            "uptime": round(time.time() - self.started_at, 1),
            "preview": len(self.symbols) <= self.preview_max_symbols,
        }


def _r(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def grade_of(alert: dict) -> str:
    """A / B, from what the setup actually has - never from a wish.

    A  every strict condition held: the HVB printed on the break candle itself,
       the external sequence is clean, and the last break of structure agrees.
    B  everything held except one of those: the HVB came late (inside
       ``hvb_window``), or the external sequence is mixed. Still a setup, one
       rung down, and the card says which rung and why.
    """
    lag = alert.get("hvb_lag")
    external = (alert.get("context") or {}).get("external") or {}
    clean = external.get("clean", True)
    bos = external.get("bos_direction")
    side = str(alert.get("side") or "")
    agrees = bos is None or (bos > 0) == (side == "LONG")
    if (lag in (0, None)) and clean and agrees:
        return "A"
    return "B"


def _evidence(alert: dict) -> list[str]:
    """The one-line reason a setup is a setup - what the card prints.

    Written once, here, so the alert, the setup record and the docs cannot drift
    apart: every entry is a fact the engine computed, never a slogan.
    """
    out: list[str] = []
    book = alert.get("book") or {}
    ctx = alert.get("context") or {}
    external = ctx.get("external") or {}
    setup = alert.get("setup") or {}
    swept = setup.get("swept_side")
    if swept:
        out.append(f"swept the {'high' if swept == 'high' else 'low'} at {_fmt(setup.get('swept_level'))}")
    if setup.get("broke_level") is not None:
        out.append(f"broke structure at {_fmt(setup.get('broke_level'))} ({setup.get('break_kind', 'SHIFT')})")
    if alert.get("hvb"):
        out.append(f"HVB {alert['hvb']} on the break candle ({_fmt(alert.get('hvb_points'))} pts)")
    if external.get("direction"):
        degree = "HH+HL" if external["direction"] > 0 else "LH+LL"
        out.append(
            "external trend " + ("bullish" if external["direction"] > 0 else "bearish")
            + (f" (clean {degree})" if external.get("clean") else " (mixed swings)")
        )
    depth = (alert.get("depth") or {}).get("value")
    if depth:
        out.append(f"depth {depth}")
    if alert.get("volume_ratio") is not None:
        out.append(f"volume {alert['volume_ratio']:.2f}x")
    if book.get("zone"):
        out.append(f"entry in {book['zone']}")
    return out


def _fmt(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "–"
    if abs(number) >= 1000:
        return f"{number:,.0f}"
    if abs(number) >= 1:
        return f"{number:.2f}"
    return f"{number:.6f}".rstrip("0").rstrip(".")
