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
from .feed import market_of, pretty_symbol
from .journal import journal
from .map import CONDITION_NAMES, DEFAULTS, KINDS, TREND_KINDS, MarketMap

ACTIVE_CAP = 200          # zones in one pushed frame (~1.8 KB each)

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
        self.markets: dict[str, str] = {s: market_of(s) for s in self.symbols}
        self.zones: dict[str, dict] = {}          # key -> zone dict (live state)
        # 1600 instruments x 5 timeframes can produce a lot of zones. The store
        # is capped (oldest *resolved* first) so a wide scan cannot grow without
        # bound; every live zone is always kept.
        self.max_zones = 20000
        self.max_active_zones = 300
        # Round 12: how many active zones one pushed frame carries. The page used
        # to receive 200, ranked by probability - so when more than 200 were live,
        # the ones below the cut *vanished from the screen* while still being
        # perfectly valid zones (neither mitigated nor invalidated). That is the
        # "signal disappeared" report. Signals now always come first, the cap is
        # explicit, and the frame says how many were left out.
        self.push_active = 200
        self.events: list[dict] = []              # newest first: formed / mitigated
        self.kinds: dict[str, int] = {}
        self.resolved: dict[str, int] = {"mitigated": 0, "invalidated": 0}
        # Round 9: every signal is written to a database the moment it exists, so
        # it can never disappear from the user's screen again. ``_journaled``
        # stores the last state written per key so a push only writes changes.
        self.journal_prob_min = 0.60
        self._journaled: dict[str, tuple] = {}
        self.journal_seen = 0
        self._journal_stats: dict[str, Any] = {}
        self._journal_stats_at = 0.0
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
        self.warm_problems: dict[str, str] = {}   # symbol -> why its history failed
        self.last_event_at = time.time()
        self._last_state_at = 0.0
        self._warm_failures = 0
        self.state_interval = self._interval_for(len(self.symbols))

        for symbol in self.symbols:
            self.add_cells(symbol)

    # ------------------------------------------------------------- plumbing
    @staticmethod
    def _interval_for(count: int) -> float:
        """How often a frame is pushed. A wide scan must not push 3 times a second.

        Round 12: 60 contracts used to push every 0.35 s - three frames a second of
        a few hundred kilobytes, and the browser rebuilt its whole list on each one.
        That is what made the app feel slow. The push is now paced by how much there
        is to say.
        """
        if count <= 20:
            return 0.35
        if count <= 60:
            return 0.7
        if count <= 200:
            return 1.2
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
            self.markets[symbol] = market_of(symbol)
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
            # Round 10: an instrument that leaves the scan takes its live plans
            # with it. The archive keeps every row, but it must not keep saying
            # "open" about a symbol nobody is watching any more - that is how a
            # signal looked like it vanished AND the open count grew at once.
            for zone in [z for z in self.zones.values() if z.get("symbol") in drop]:
                self._journal_drop(zone)
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
            interval = self._interval_for_timeframe(feed, symbol, tf)
            if interval is None:
                await self._warm_from_minutes(feed, symbol, tf)
                self.warm_done += 1
                continue
            candles = None
            problem: Exception | None = None
            # 1st: the full window. 2nd: the same interval over a much shorter span -
            # a long `start` is the usual reason an exchange answers "no kline" for a
            # contract that clearly has candles. 3rd: a retry after the rate limit.
            for attempt, bars_wanted in enumerate((WARM_BARS, max(120, WARM_BARS // 5), WARM_BARS)):
                try:
                    candles = await asyncio.wait_for(
                        feed.history(symbol, interval, bars_wanted), timeout=25)
                    self._warm_failures = 0
                    break
                except Exception as exc:  # a broken symbol must not kill the scanner
                    problem = exc
                    await asyncio.sleep(0.5 if attempt == 1 else 1.0)
            if not candles:
                # Round 13: one timeframe with no history used to leave the whole
                # instrument empty (the "warm-up TON_USDT 1h: FeedError: MEXC
                # futures returned no kline" report). The 1m series is the one the
                # exchange always has, so the higher timeframe is built from it.
                before = len(self.history.get(key, ()))
                await self._warm_from_minutes(feed, symbol, tf)
                gained = len(self.history.get(key, ())) - before
                self.warm_done += 1
                if gained >= 1:
                    self.warm_problems.pop(f"{symbol} {tf}", None)
                    continue
                self._warm_failures += 1
                self.warm_errors += 1
                reason = f"{type(problem).__name__}: {str(problem)[:120]}" if problem else "no candles"
                self.warm_problems[f"{symbol} {tf}"] = reason
                self.warm_problems[symbol] = f"{tf}: {reason}"
                if self._warm_failures <= 3:
                    await self._log(
                        f"warm-up {symbol} {tf}: {reason} - and rolling up from 1m gave only "
                        f"{gained} candles, so this timeframe stays empty for now", "warn")
                continue
            for bar in candles:
                self._append(key, {**bar, "closed": True})
            self.warm_done += 1

    @staticmethod
    def _interval_for_timeframe(feed, symbol: str, tf: str) -> str | None:
        """Ask the feed for its own name for this timeframe, per symbol.

        A feed that fronts two markets (spot + futures) can only answer when it
        knows which symbol, and a test double written before this existed only
        takes one argument - so both are handled, instead of crashing a warm-up.
        """
        if not hasattr(feed, "exchange_interval"):
            return tf
        try:
            return feed.exchange_interval(tf, symbol)
        except TypeError:
            return feed.exchange_interval(tf)

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
        """Warm every instrument, a few at a time - and never give up on the rest.

        Round 9 fixed a real reason futures looked empty: this loop used to return
        early for **every remaining symbol** once eight calls in a row had failed.
        Sixty contracts is three hundred REST calls, the contract API rate-limits,
        and the first burst of failures therefore switched the whole warm-up off -
        the scanner then waited for the live stream to fill 240 bars per
        timeframe, which at 15m is two and a half days. Now a failure costs one
        instrument, the pass backs off instead of stopping, and short histories are
        topped up by ``healer()`` later.
        """
        sem = asyncio.Semaphore(max(1, concurrency))
        backoff = {"sleep": 0.0}

        async def one(symbol: str) -> None:
            async with sem:
                if backoff["sleep"]:
                    await asyncio.sleep(backoff["sleep"])
                try:
                    await self.warm_symbol(feed, symbol)
                    self._warm_failures = 0
                    backoff["sleep"] = 0.0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.warm_errors += 1
                    self._warm_failures += 1
                    if self._warm_failures >= 4:
                        # the exchange is pushing back: slow down, do not stop
                        backoff["sleep"] = min(15.0, 0.5 * self._warm_failures)
                        self._warm_failures = 0

        try:
            await asyncio.gather(*(one(symbol) for symbol in list(self.symbols)))
        finally:
            self.warming = False
        short = self.thin_symbols()
        if short:
            why = "; ".join(f"{k}: {v}" for k, v in list(self.warm_problems.items())[:3])
            await self._log(
                f"warm-up: {len(short)} of {len(self.symbols)} instruments still short of history"
                + (f" ({why})" if why else "")
                + " - the healer re-requests them in the background", "warn")
        await self._emit_state(force=True)

    def thin_symbols(self, need: int = 200) -> list[str]:
        """Instruments with too little history for the 240-bar lookback.

        These are the ones that silently produce no signals. The diagnostics
        endpoint prints the same number, so "why is this contract empty" has an
        answer on screen instead of a guess.

        Round 13: the requirement is per timeframe. An hourly map can only ever be
        filled from hourly candles (or from a roll-up of one-minute ones, which
        gives ~16 hours per 1000 minutes), so demanding 200 hourly candles would
        keep a perfectly usable instrument on the healer's list for ten days.
        """
        out: list[str] = []
        for symbol in self.symbols:
            short = False
            for tf in self.timeframes:
                bars = len(self.history.get((symbol, tf), ()))
                wanted = need if tf in ("1m", "3m", "5m") else min(need, 60)
                minimum = int(self.maps[(symbol, tf)].cfg.get("structure_min_bars", 30)) if (symbol, tf) in self.maps else 30
                if bars < max(wanted, minimum):
                    short = True
                    break
            if short:
                out.append(symbol)
        return out

    async def backfill(self, feed, symbol: str) -> None:
        """Top up a symbol that arrived through the live stream with no history."""
        await self.warm_symbol(feed, symbol)
        await self._emit_state(force=True)

    # -------------------------------------------------------------- candles
    TF_PARENT = {"1m": "5m", "3m": "5m", "5m": "15m", "15m": "30m", "30m": "1h", "1h": "1h"}

    def _refresh_context(self, key: tuple[str, str]) -> None:
        """Give a map the structure of the timeframe above it (round 13)."""
        symbol, tf = key
        map_ = self.maps.get(key)
        if map_ is None:
            return
        parent_tf = self.TF_PARENT.get(tf, tf)
        if parent_tf == tf:
            map_.context = {}
            return
        parent = self.maps.get((symbol, parent_tf))
        if parent is None or parent is map_:
            map_.context = {}
            return
        state = parent.structure()
        map_.context = {**state, "timeframe": parent_tf}

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
        # Round 13: the timeframe above this one is the "bigger trend" the book asks
        # for (p.39, p.104). It is read before the bar is added, so a zone can only
        # ever be graded on structure that was already confirmed.
        self._refresh_context(key)
        events = map_.add(bar)
        if events.get("zones"):
            for zone in events["zones"]:
                zone["market"] = market_of(zone["symbol"])
                self.zones[zone["key"]] = zone
                self.kinds[zone["kind"]] = self.kinds.get(zone["kind"], 0) + 1
                self.events.insert(0, {"type": "formed", "zone": zone})
                self._journal_write(zone)
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
                if current is not None:
                    self._journal_close(item["key"], status, current)
                self.events.insert(0, {"type": status, "key": item["key"], "zone": current})
        del self.events[400:]
        self._prune_zones()
        if events.get("zones") or events.get("resolved"):
            self.last_event_at = time.time()
        return events

    # ------------------------------------------------------------- journal
    def _journal_write(self, zone: dict) -> None:
        """Store a zone the first time it becomes worth storing.

        The bar is ``journal_prob_min`` (default 60%, i.e. the C band and above)
        *or* anything the model called a signal - so the archive always contains
        every signal the page ever showed, plus the near misses, and never the
        noise.
        """
        store = journal()
        if not store.enabled:
            return
        if not (zone.get("signal") or (zone.get("prob") or 0) >= self.journal_prob_min):
            return
        if store.record(zone, int(zone.get("time") or time.time() * 1000)):
            self.journal_seen += 1
            self._journaled[zone.get("key")] = (zone.get("status"), zone.get("plan_state"))

    def _journal_close(self, key: str, status: str, zone: dict) -> None:
        store = journal()
        if not store.enabled:
            return
        result = {"plan_target": "target", "plan_stopped": "stopped",
                  "mitigated": "mitigated", "invalidated": "invalidated"}.get(status)
        # Round 13: entering the block fills the plan - that is the *start* of the
        # trade, not the end of the signal. The row stays open (so the live tab, the
        # "open now" count and a restart's re-adoption all still see it) until the
        # target or the stop decides it.
        filled = str(zone.get("plan_state") or "") == "filled"
        keep_open = filled and status in ("mitigated",)
        store.close_signal(
            key,
            status="active" if (status.startswith("plan_") or keep_open) else status,
            at=int(zone.get("plan_result_at") or zone.get("mitigated_at")
                   or zone.get("invalidated_at") or time.time() * 1000),
            result=result,
            result_r=zone.get("plan_result_r"),
        )
        self._journaled[key] = (zone.get("status"), status)

    def _journal_drop(self, zone: dict) -> None:
        """Stop following a row without losing it (instrument removed / replaced)."""
        store = journal()
        key = zone.get("key")
        if not store.enabled or not key:
            return
        if zone.get("status") != "active":
            self._journaled.pop(key, None)
            return
        store.close_signal(
            key,
            status="untracked",
            at=int(time.time() * 1000),
            result=zone.get("result"),
            result_r=zone.get("plan_result_r"),
            note="the instrument was removed from the scan while this plan was open",
        )
        self._journaled.pop(key, None)

    def journal_stats(self, max_age: float = 5.0) -> dict[str, Any]:
        """The archive's counters, cached - a push every 0.35 s must not count rows."""
        now = time.time()
        if self._journal_stats and now - self._journal_stats_at < max_age:
            return self._journal_stats
        try:
            self._journal_stats = journal().stats()
            self._journal_stats_at = now
        except Exception:
            pass
        return self._journal_stats

    def restore_open(self, snapshots: list[dict]) -> int:
        """Put the plans that were open when the server stopped back on the map.

        Round 13: "signals disappear when I refresh". They were stored - the
        database has every one - but a restart threw the in-memory map away and
        marked the open plans ``interrupted``, so the live tab came back empty and
        the trades were never followed again. Now the scanner adopts them: they are
        live immediately, and their targets and stops keep being checked against
        the candles that arrive.
        """
        restored = 0
        for data in snapshots:
            tf = str(data.get("timeframe") or "")
            symbol = str(data.get("symbol") or "")
            map_ = self.maps.get((symbol, tf))
            if map_ is None:
                continue
            zone = map_.adopt(data)
            if zone is None:
                continue
            item = zone.as_dict()
            item["market"] = market_of(symbol)
            item["restored"] = True
            self.zones[item["key"]] = item
            self._journaled[item["key"]] = (item.get("status"), item.get("plan_state"))
            restored += 1
        return restored

    def diagnostics(self) -> list[dict[str, Any]]:
        """One row per instrument: what the scanner actually has for it.

        "Futures gives no signals" is almost never the pattern engine - it is a
        contract whose history never arrived, or arrived too short to reach the
        lookback. This is the table that says which, for every contract, so the
        question stops being a guess.
        """
        rows: list[dict[str, Any]] = []
        for symbol in self.symbols:
            cells: dict[str, dict[str, Any]] = {}
            zones = signals = 0
            for tf in self.timeframes:
                key = (symbol, tf)
                map_ = self.maps.get(key)
                bars = len(self.history.get(key, ()))
                active = len(map_.active()) if map_ is not None else 0
                zones += active
                signals += sum(1 for z in (map_.active() if map_ is not None else []) if getattr(z, "signal", False))
                cells[tf] = {
                    "bars": bars,
                    "zones": active,
                    "atr": round(map_.atr_now(), 8) if (map_ is not None and map_.atr_now()) else None,
                    "ready": bool(bars >= int(self.settings.get("lookback", 240))),
                }
            rows.append({
                "symbol": symbol,
                "market": self.markets.get(symbol, market_of(symbol)),
                "price": self.price_of(symbol),
                "cells": cells,
                "zones": zones,
                "signals": signals,
                "problem": self.warm_problems.get(symbol),
            })
        rows.sort(key=lambda r: (-r["signals"], -r["zones"], r["symbol"]))
        return rows

    def sync_journal(self) -> int:
        """Write the changed state of every stored zone (cheap: only changes).

        A plan goes waiting -> filled -> target/stopped while the zone itself stays
        active. Without this the archive would keep saying "waiting" forever.
        """
        store = journal()
        if not store.enabled:
            return 0
        written = 0
        for zone in self.zones.values():
            key = zone.get("key")
            if not key:
                continue
            # Once a row exists it is kept up to date for its whole life. The old
            # gate skipped any zone whose probability had since fallen below the
            # floor, which froze those rows at "waiting" while the scanner went on
            # to fill them and hit their target - the record stopped matching the
            # trade, and looked like the details had been lost.
            if key not in self._journaled and not (
                zone.get("signal") or (zone.get("prob") or 0) >= self.journal_prob_min
            ):
                continue
            state = (zone.get("status"), zone.get("plan_state"))
            if self._journaled.get(key) == state:
                continue
            if store.record(zone):
                self._journaled[key] = state
                written += 1
        return written

    def _prune_zones(self) -> None:
        """Keep the store bounded: resolved zones go first, oldest first."""
        excess = len(self.zones) - max(1000, self.max_zones)
        if excess <= 0:
            return
        resolved = sorted(
            (z for z in self.zones.values() if z.get("status") != "active"),
            key=lambda z: -(z.get("mitigated_at") or z.get("invalidated_at") or 0),
        )
        for zone in resolved[-excess:]:
            self.zones.pop(zone.get("key"), None)

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
            self._pending_log = (
                f"{symbol}: the replay looped back to the start of the recording, so its map was "
                "rebuilt from the first candle - a replay-only note, not a live error")
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
        # a plan moves while its zone is still active, so the archive is brought
        # up to date here - once per push, and only for the rows that changed
        try:
            self.sync_journal()
        except Exception:
            pass
        # the resolved zones are fetched on demand (/api/zones), not pushed
        await self._broadcast({"type": "state", **self.snapshot(cells=False, with_history=False)})

    @staticmethod
    def is_live(zone: dict) -> bool:
        """Is this still something the trader is watching?

        Round 13 - the real "my signal disappeared" bug in the live app: a zone
        leaves ``status == "active"`` the moment price comes back into the block,
        which is *exactly* when its plan fills and the trade opens. So the trade
        was hidden at the moment it started, and the card vanished although
        nothing had been completed or invalidated. Liveness is the **plan**, not
        the block: a zone is live while its block is untouched, and stays live
        for as long as the trade it started is open (filled, not yet
        target/stopped).
        """
        if zone.get("status") == "active":
            return True
        return zone.get("plan_state") == "filled"

    def active_zones(self) -> list[dict]:
        return [z for z in self.zones.values() if self.is_live(z)]

    async def _broadcast(self, message: dict) -> None:
        if self.broadcast is not None:
            await self.broadcast(message)

    async def _log(self, message: str, level: str = "info") -> None:
        await self._broadcast({"type": "log", "level": level, "message": message})

    # ------------------------------------------------- live context / grading
    def _strong_levels(self) -> dict[tuple[str, str, str], list[dict]]:
        """Index of every strong / very strong level, per (symbol, side).

        Built on demand from the zones that already exist, so a grade never
        depends on data the map did not have when the zone formed.
        """
        index: dict[tuple[str, str, str], list[dict]] = {}
        for zone in self.zones.values():
            if zone.get("kind") not in TREND_KINDS:
                continue
            index.setdefault((zone["symbol"], zone["side"], zone["timeframe"]), []).append(zone)
        return index

    def _htf_confluence(self, zone: dict, index: dict[tuple[str, str, str], list[dict]]) -> list[str]:
        """A strong level on another timeframe sitting at this zone's level.

        The book says it plainly: "HTF REJECTION BLOCK IS A LTF ALGO CANDLE" -
        so the same price tagged as a strong level on a higher timeframe is the
        confluence that matters. Compared in percent, because every timeframe
        has its own ATR.
        """
        out: list[str] = []
        level = float(zone.get("level") or 0.0)
        if not level:
            return out
        for other_tf in self.timeframes:
            if other_tf == zone.get("timeframe"):
                continue
            for strong in index.get((zone["symbol"], zone["side"], other_tf), ()):
                if abs(float(strong.get("level") or 0.0) - level) / level > 0.0015:
                    continue
                label = "very strong" if str(strong.get("kind", "")).startswith("very_strong") else "strong"
                text = f"{other_tf} {label} {zone['side']} at the same price"
                if text not in out:
                    out.append(text)
        return out

    def enrich(self, zone: dict, index: dict | None = None) -> dict:
        """Add what is true *now*: the live regime and the HTF confluence.

        ``now_trend`` answers the only question that matters at the moment of a
        trade - "is the market trending the way this zone needs?" - and it is
        recomputed on every push because a range can turn into a trend while a
        block waits for price.
        """
        index = index if index is not None else self._strong_levels()
        map_ = self.maps.get((zone.get("symbol"), zone.get("timeframe")))
        if map_ is not None:
            regime = map_.regime()
            aligned = (regime["state"] == "trend_down" and zone.get("side") == "high") or (
                regime["state"] == "trend_up" and zone.get("side") == "low"
            )
            zone["now_trend"] = {
                **regime,
                "aligned": bool(aligned),
                "ready": bool(aligned and regime["state"] in ("trend_up", "trend_down")),
            }
        zone["htf"] = self._htf_confluence(zone, index)
        zone["market"] = market_of(zone.get("symbol", ""))
        zone["pretty"] = pretty_symbol(zone.get("symbol", ""))
        return zone

    # -------------------------------------------------------------- markets
    def market_counts(self, active: list[dict] | None = None) -> dict[str, Any]:
        """How many zones each market has - the page shows them side by side."""
        active = active if active is not None else self.active_zones()
        out: dict[str, Any] = {}
        for market in ("spot", "futures"):
            zones = [z for z in self.zones.values() if z.get("market") == market]
            live = [z for z in active if z.get("market") == market]
            out[market] = {
                "symbols": len([s for s in self.symbols if self.markets.get(s) == market]),
                "zones": len(zones),
                "active": len(live),
                "tradable": len([z for z in live if (z.get("plan") or {}).get("rr")]),
            }
        return out

    # -------------------------------------------------------------- snapshot
    @staticmethod
    def _regime(r: dict | None) -> dict | None:
        """A regime in the few fields the page prints (the rest is derivable)."""
        if not r:
            return None
        return {k: r.get(k) for k in ("state", "er", "adx", "aligned", "ready", "label") if k in r}

    @staticmethod
    def _plan(p: dict | None) -> dict | None:
        if not p:
            return None
        return {k: p.get(k) for k in ("side", "entry", "stop", "target", "target_source",
                                      "rr", "risk", "reward", "risk_pct", "stop_kind") if k in p}

    @staticmethod
    def slim(zone: dict, lean: bool = False) -> dict:
        """Only the fields the page draws - keeps every push small.

        ``lean`` drops the parts a wide scan cannot afford to push (the evidence
        list, the order-block candle, the confluence detail). The page fetches
        those from ``/api/zones`` the moment a card is opened, so nothing is
        lost - it just is not repeated in every frame.
        """
        out = {
            key: zone.get(key)
            for key in (
                "key", "kind", "side", "symbol", "timeframe", "top", "bottom", "level",
                "time", "status", "strength", "session", "mitigated_at", "mitigated_price",
                "invalidated_at", "invalidated_price",
                "block_src", "grade", "score", "block_atr", "disp_atr", "fvg_atr", "swept_major",
                # round 6: which market, and the trade plan that goes with it
                "market", "plan_state", "plan_filled_at", "plan_filled_price",
                "plan_result_at", "plan_result_r",
                # round 7: the measured probability and what it costs to trade
                "prob", "fee_r", "net_r", "tradable", "cond_met", "cond_total",
                # round 8: is it a signal, and which measured band does it sit in
                "signal", "measured",
            )
        }
        out["plan"] = Engine._plan(zone.get("plan"))
        out["cond_missing"] = [n for n in CONDITION_NAMES if not (zone.get("conds") or {}).get(n)]
        if not lean:
            out["evidence"] = zone.get("evidence")
            out["ob"] = zone.get("ob")
            out["fvg"] = zone.get("fvg")
            out["confluence"] = zone.get("confluence")
            out["htf"] = zone.get("htf")
            out["swept_level"] = zone.get("swept_level")
            out["break_level"] = zone.get("break_level")
            out["inducement"] = zone.get("inducement")
            out["trend"] = Engine._regime(zone.get("trend"))
            out["now_trend"] = Engine._regime(zone.get("now_trend"))
            out["entry_trend"] = Engine._regime(zone.get("entry_trend"))
        return out

    # The handful of fields a frame really needs: enough to draw a card, rank it,
    # filter it and print its plan - and nothing that is a list of prose. The
    # evidence, the order-block candle, the confluence lines and the HTF detail are
    # fetched for the one zone the user opens (`/api/zones/{key}`), which is the only
    # place they are ever read. Round 12: this took a 300-zone frame from ~380 KB to
    # ~110 KB, which is what the "the app got slow" report was about.
    ROW_FIELDS = (
        "key", "kind", "side", "symbol", "timeframe", "top", "bottom", "level", "time",
        "status", "strength", "session", "mitigated_at", "mitigated_price",
        "invalidated_at", "invalidated_price", "grade", "block_atr", "disp_atr", "market",
        "plan_state", "plan_result_at", "plan_result_r", "prob", "fee_r", "net_r",
        "tradable", "cond_met", "cond_total", "signal", "measured",
    )

    @staticmethod
    def row(zone: dict) -> dict:
        """A zone as the frame carries it: no prose, only what a card prints.

        Three things keep it small, because this object is sent for *every* live
        zone on every push: empty fields are left out entirely (a frame was 30 %
        nulls), the missing-condition list becomes a 10-bit mask the page already
        knows how to read, and the floats are cut to the precision a screen can
        show. Nothing is lost - the full zone is one request away.
        """
        out = {key: zone.get(key) for key in Engine.ROW_FIELDS if zone.get(key) is not None}
        plan = Engine._plan(zone.get("plan"))
        if plan:
            # target_source is a sentence; the card prints it, so it stays - but a
            # long name is cut, the full text is in the detailed fetch
            if isinstance(plan.get("target_source"), str):
                plan["target_source"] = plan["target_source"][:48]
            out["plan"] = plan
        gate = zone.get("gate") or {}
        if gate:
            # compact: the card prints the first two reasons, the full list is in
            # the detailed fetch. ~90 bytes a row, and it is why a pattern in a
            # range is visibly *not* a signal.
            out["gate"] = {
                "ok": bool(gate.get("ok")),
                "why": list(gate.get("why") or [])[:2],
                "structure": gate.get("structure"),
                "htf": gate.get("htf"),
                "htf_timeframe": gate.get("htf_timeframe"),
                "location": gate.get("location"),
            }
        conds = zone.get("conds") or {}
        mask = 0
        for i, name in enumerate(CONDITION_NAMES):
            if conds.get(name):
                mask |= 1 << i
        out["cond_mask"] = mask
        trend = Engine._regime(zone.get("now_trend") or zone.get("trend"))
        if trend:
            out["now_trend"] = trend
        for key in ("prob", "net_r", "fee_r", "block_atr", "disp_atr", "fvg_atr"):
            value = out.get(key)
            if isinstance(value, float):
                out[key] = round(value, 4 if key == "prob" else 3)
        measured = out.get("measured")
        if isinstance(measured, dict):
            out["measured"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in measured.items()}
        return out

    @staticmethod
    def light(zone: dict) -> dict:
        """The few fields the live tape prints."""
        return {
            key: zone.get(key)
            for key in ("key", "kind", "side", "symbol", "timeframe", "level", "time", "status",
                        "mitigated_at", "invalidated_at")
        }

    @staticmethod
    def rank_key(zone: dict) -> tuple:
        """Best first: measured chance, then expectancy, then the letter.

        The letter is derived from the measured chance, so ordering by the
        probability and *then* by the letter keeps a card whose stop is too tight
        to pay its own fees from jumping over a card that is simply more likely
        to reach its target. Trade-readiness is a filter - the page opens on it -
        not a promotion: a 51 % plan is not better than a 92 % plan only because
        its stop is wide enough to clear the fee.
        """
        grade = {"A+": 3, "A": 2, "B": 1, "C": 0}.get(zone.get("grade"), 0)
        net = zone.get("net_r")
        return (
            1 if Engine.is_live(zone) else 0,
            float(zone.get("prob") or 0.0),
            float(net if net is not None else -99.0),
            grade,
            int(zone.get("time") or 0),
        )

    def ranked(
        self, market: str = "", limit: int = 300, only_tradable: bool = False, status: str = "active",
        only_signal: bool = False,
    ) -> list[dict]:
        """Every zone, best first - the list the page's top section is built from."""
        items = list(self.zones.values())
        if status in ("active", "live"):
            items = [z for z in items if self.is_live(z)]
        elif status != "all":
            items = [z for z in items if z.get("status") == status]
        if market in ("spot", "futures"):
            items = [z for z in items if market_of(z.get("symbol", "")) == market]
        if only_tradable:
            items = [z for z in items if z.get("tradable")]
        if only_signal:
            items = [z for z in items if z.get("signal")]
        items.sort(key=self.rank_key, reverse=True)
        return items[: max(1, int(limit))]

    def snapshot(self, cells: bool = True, with_history: bool = True) -> dict[str, Any]:
        # A wide scan cannot push full detail for hundreds of zones every couple
        # of seconds, so past this many instruments the frame goes lean and the
        # page fetches the detail of the zone it opens.
        lean = len(self.symbols) > 120
        index = self._strong_levels()
        everything = self.ranked("", limit=len(self.zones) or 1)
        signal_total = sum(1 for z in everything if z.get("signal"))
        # Signals first, then everything else, both already best-first. A signal can
        # therefore only be left out if there are more signals than the frame holds.
        ordered = [z for z in everything if z.get("signal")]
        ordered += [z for z in everything if not z.get("signal")]
        cap = max(1, int(self.push_active))
        active = ordered[:cap]
        active_total = len(everything)
        # only what is actually sent is enriched: a wide scan must not pay for
        # work the page will never see
        for zone in active:
            self.enrich(zone, index)
        for zone in active:
            zone["market"] = market_of(zone.get("symbol", ""))
        history = [z for z in self.zones.values() if not self.is_live(z)]
        history.sort(key=lambda z: -int(z.get("mitigated_at") or z.get("invalidated_at") or 0))
        # A resolved zone is still shown (the page keeps the recent ones), so it
        # needs the same "as of now" fields as a live one - otherwise its card and
        # its journal row print a blank grade and a blank market. Only the handful
        # that is actually sent is enriched.
        for zone in history[:60]:
            self.enrich(zone, index)
        return {
            "source": self.source,
            "exchange": self.exchange,
            "symbols": self.symbols,
            "markets": {
                "spot": [s for s in self.symbols if self.markets.get(s) != "futures"],
                "futures": [s for s in self.symbols if self.markets.get(s) == "futures"],
            },
            "market_counts": self.market_counts(active),
            "timeframes": self.timeframes,
            "settings": self.settings,
            "kinds": [k for k in KINDS] + ["weak_high", "weak_low"],
            "counts": dict(self.kinds),
            "grade_counts": {
                g: sum(1 for z in self.zones.values() if z.get("status") == "active" and z.get("grade") == g)
                for g in ("A+", "A", "B", "C", "-")
            },
            "tradable_count": sum(1 for z in self.zones.values() if z.get("status") == "active" and z.get("tradable")),
            "signal_count": sum(1 for z in self.zones.values() if z.get("status") == "active" and z.get("signal")),
            "model": {
                "available": bool(getattr(self, "model_available", False)),
                "problem": getattr(self, "model_problem", None),
            },
            "resolved": dict(self.resolved),
            # round 9: the archive - signals are in a database, not only in a push
            "journal": {
                **(self.journal_stats() or {}),
                "seen": self.journal_seen,
            },
            # Round 12: what the frame holds, and what it had to leave out. The page
            # says so out loud instead of quietly dropping zones.
            "active_total": active_total,
            "active_shown": len(active),
            "signal_total": signal_total,
            "signals_shown": sum(1 for z in active if z.get("signal")),
            "push_limit": cap,
            "active": [self.row(z) for z in active],
            "history": [self.slim(z, lean) for z in history[:30]] if with_history else [],
            "events": [
                {**event, "zone": self.light(event["zone"]) if isinstance(event.get("zone"), dict) else None}
                for event in self.events[:12]
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
                    "regime": map_.regime(),
                }
                for map_ in self.maps.values()
            ],
        }
