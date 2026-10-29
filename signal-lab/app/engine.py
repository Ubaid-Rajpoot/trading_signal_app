"""The signal engine.

Two modes, one pipeline:

  replay  walks the local 1-minute archive, bar by bar, at a clock you control.
          Everything the page shows is produced *in the order the bars arrived* -
          no look-ahead, no re-scoring of old bars with new information.
  live    polls MEXC futures for every configured symbol **and every configured
          timeframe at the same time**. MEXC only serves 1m/5m/15m/30m/1h/4h, so
          3m, 10m, 45m, 2h and 3h are aggregated from the nearest native interval.

A signal is created when the model's probability of the target being touched
before the stop crosses that timeframe's cut. It is **filled at the next bar's
open**, then resolved by the first barrier touched, with a bar that touches both
counted as a loss. One entry per stream per `horizon` bars, which is exactly how
the accuracy was measured in `research/`.

Scanning many instruments must never hang: the universe is capped, fetches go
through a bounded thread pool, a failing symbol is recorded and skipped, and the
first poll walks back `scan_bars` closed bars so the page has signals at start-up
instead of after an hour of waiting.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np

import joblib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import sys

for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from lab import data as labdata  # noqa: E402
from lab import features as labfeatures  # noqa: E402
from lab.outcomes import atr_series, first_touch  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
START_MS = 1704067200000     # 2024-01-01 - the years the model never trained on
WARMUP = 700                 # bars of history before the first recorded signal
ALL_TFS = ("1m", "3m", "5m", "10m", "15m", "30m", "45m", "1h", "2h", "3h", "4h")
# MEXC serves these; the rest are built by aggregation (see app/feed.py)
NATIVE_FOR = {"1m": "1m", "3m": "1m", "5m": "5m", "10m": "5m", "15m": "15m",
              "30m": "15m", "45m": "15m", "1h": "1h", "2h": "1h", "3h": "1h", "4h": "4h"}
# enough history for the 59 features (700 bars of window) plus the start-up scan
NATIVE_BARS = {"1m": 1200, "5m": 1200, "15m": 1200, "30m": 1200, "1h": 900, "4h": 800}
DEFAULT_GEOM = {"target": 8.0, "stop": 2.0, "horizon": 240}
COLUMNS = ["key", "symbol", "tf", "side", "band", "prob", "signal_ms", "entry_ms", "entry",
           "stop", "target", "atr", "status", "bars_held", "pnl_r", "exit_ms",
           "fee_r", "break_even", "origin", "rr"]


@dataclass
class Signal:
    key: str
    symbol: str
    tf: str
    side: str
    band: str
    prob: float
    signal_ms: int
    entry_ms: int | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    atr: float | None = None
    status: str = "pending"
    fee_r: float | None = None
    break_even: float | None = None
    bars_held: int = 0
    pnl_r: float | None = None
    exit_ms: int | None = None
    origin: str = "live"
    _fill_i: int | None = field(default=None, repr=False)

    def row(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        d["rr"] = (self.target - self.entry) / (self.entry - self.stop) if self.entry and self.stop else None
        return d


class Stream:
    """One (symbol, timeframe) feed with its own position in time."""

    def __init__(self, symbol: str, tf: str, s):
        self.symbol, self.tf, self.s = symbol, tf, s
        self.i = -1
        self.pending: Signal | None = None
        self.open: list[Signal] = []
        # the research measures one trade per `horizon` bars, not one per open
        # position: a trade that resolves in 3 bars must not let the next one in
        # immediately, or the sample is 4x larger than the one that was measured
        self.next_ok_bar = 0
        self.last_t: int = 0


class Engine:
    def __init__(self, mode: str = "replay", speed: float = 25.0, cut: float | None = None,
                 symbols: list[str] | None = None, tfs: list[str] | None = None,
                 horizon: int | None = None, start_ms: int = START_MS, db: str | None = None,
                 config=None, assume_live: bool = False):
        self.config = config
        self.mode = mode
        self.speed = speed
        self.cut = cut
        self.lock = threading.RLock()
        db_rel = getattr(config, "db", None) if config is not None else None
        self.db_path = db or (os.path.join(ROOT, db_rel) if db_rel else os.path.join(HERE, "data", "signals.db"))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS signals(
            key TEXT PRIMARY KEY, symbol TEXT, tf TEXT, side TEXT, band TEXT, prob REAL,
            signal_ms INTEGER, entry_ms INTEGER, entry REAL, stop REAL, target REAL, atr REAL,
            status TEXT, bars_held INTEGER, pnl_r REAL, exit_ms INTEGER, mode TEXT)""")
        for col, typ in (("fee_r", "REAL"), ("break_even", "REAL"), ("origin", "TEXT"),
                         ("rr", "REAL")):
            try:
                self._db.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        self._db.commit()

        pack = joblib.load(os.path.join(HERE, "model.pkl"))
        # A replay of 2024+ must not be scored by a model that was trained on 2024+.
        # Replay uses the validation model (trained up to 2023-12-31); live uses the
        # production model, because there is no "after" for live to cheat with.
        if self.mode == "replay" and "models_oos" in pack:
            self.models = pack["models_oos"]
            self.model_label = "validation model, trained until 2023-12-31"
        else:
            self.models = pack["models"]
            self.model_label = "production model, trained through 2026-10"
        self.pack = pack
        self.features = pack["features"]
        self.model_features = sorted(self.features)
        self.fee_rt = float(pack.get("fee_rt", 0.0005))
        self.geoms = self._load_geoms(pack)
        wanted = [t for t in (tfs or list(ALL_TFS)) if t in ALL_TFS]
        # a timeframe with no model cannot be scored: say so instead of guessing
        self.missing_tfs = [t for t in wanted if t not in self.models]
        self.tfs = [t for t in wanted if t in self.models] or ["1h"]
        default_tf = self.tfs[0] if self.tfs else "1h"
        self.geometry = self.geom(default_tf)
        self.horizon = int(horizon or self.geometry["horizon"])
        with open(os.path.join(HERE, "data", "bands.json")) as fh:
            self.bands_meta = json.load(fh)
        ordered = sorted(self.bands_meta["bands"], key=lambda b: -b["cut"])
        self.band_cuts = [(b["cut"], "ABCDEFGH"[min(i, 7)]) for i, b in enumerate(ordered) if b["cut"] > 0]
        # per-timeframe bands: the same probability means different things on a
        # 1-minute bar and a 4-hour bar, so each clock has its own measured table
        self.band_tables: dict[str, list[tuple[float, str]]] = {}
        tf_bands_path = os.path.join(HERE, "data", "bands_tf.json")
        if os.path.exists(tf_bands_path):
            with open(tf_bands_path) as fh:
                for tf, table in (json.load(fh).get("bands") or {}).items():
                    rows = sorted(table, key=lambda b: -b["cut"])
                    self.band_tables[tf] = [(float(b["cut"]), "ABCDEFGH"[min(i, 7)])
                                            for i, b in enumerate(rows) if b["cut"] > 0]

        self.symbols = symbols or ["BTCUSDT", "ETHUSDT"]
        self.streams: dict[tuple[str, str], Stream] = {}
        self.universe: list[str] | None = None
        self.failed: dict[str, str] = {}
        # replay starts at the first recorded bar; live starts now (otherwise the
        # page would show 2024-01-01 until the first poll succeeds)
        self.clock_ms = int(time.time() * 1000) if mode == "live" else start_ms
        self.steps = 0
        self.finished = False
        self.adopted = 0
        self.error: str | None = None
        self.started = time.time()
        self.last_poll = 0.0
        self.backfilling = False
        self.client = None
        self.assume_live = assume_live      # tests: never touch the network
        if mode == "replay":
            # a simulation is not a record: replay rows are rebuilt every run so a
            # restart cannot double-count the same trades
            self._db.execute("DELETE FROM signals WHERE mode = 'replay'")
            self._db.commit()
        self._load_signals()

    # -------------------------------------------------------------- geometry
    def _load_geoms(self, pack: dict) -> dict:
        """Which stop, target and cut applies on each timeframe.

        The sweep in `research/08_timeframes.py` decides this per timeframe: a
        2 ATR stop is ~0.03 R of fee on 1h but ~0.5 R on 1m, so one geometry for
        all clocks is not a choice, it is a bug. `tools/train_scanner.py` writes
        the winners into `app/data/geoms.json`; anything missing falls back to the
        pack's single 1h geometry and is flagged as such in /api/state.
        """
        base = {"target": pack.get("target", 8.0), "stop": pack.get("stop", 2.0),
                "horizon": pack.get("horizon", 240)}
        geoms = {tf: dict(base) for tf in ALL_TFS}
        src = pack.get("geoms")
        if not src and os.path.exists(os.path.join(HERE, "data", "geoms.json")):
            with open(os.path.join(HERE, "data", "geoms.json")) as fh:
                src = json.load(fh).get("geoms")
        for tf, g in (src or {}).items():
            if tf in geoms:
                geoms[tf].update({k: g[k] for k in ("target", "stop", "horizon") if k in g})
                for extra in ("cut", "n", "win", "fee_r", "ev_post", "per_day", "viable",
                              "basis", "source"):
                    if extra in g:
                        geoms[tf][extra] = g[extra]
        for tf in ALL_TFS:
            g = geoms[tf]
            g["rr"] = float(g["target"]) / float(g["stop"])
            g.setdefault("cut", float(self.bands_meta_recommended()))
        return geoms

    def bands_meta_recommended(self) -> float:
        try:
            with open(os.path.join(HERE, "data", "bands.json")) as fh:
                return float(json.load(fh).get("recommended_cut", 0.30))
        except Exception:
            return 0.30

    def geom(self, tf: str) -> dict:
        g = dict(self.geoms.get(tf) or DEFAULT_GEOM)
        g.setdefault("target", 8.0)
        g.setdefault("stop", 2.0)
        g.setdefault("horizon", 240)
        g["rr"] = float(g["target"]) / float(g["stop"])
        g["label"] = f"1 : {g['rr']:.0f}"
        return g

    def cut_for(self, tf: str) -> float:
        """The probability a setup must reach to be recorded on this timeframe."""
        if self.cut is not None:
            return float(self.cut)
        return float(self.geoms.get(tf, {}).get("cut", 0.30))

    def record_cut(self) -> float:
        """One number for the header and the /api/cut control (the default clock's)."""
        tf = self.tfs[0] if self.tfs else "1h"
        return self.cut_for(tf)

    def horizons(self) -> dict[str, int]:
        return {tf: int(self.geom(tf)["horizon"]) for tf in self.tfs}

    # ---------------------------------------------------------------- storage
    def _load_signals(self) -> None:
        cols = ", ".join(COLUMNS)
        cur = self._db.execute(f"SELECT {cols} FROM signals WHERE mode = ?", (self.mode,))
        self.memory: list[dict] = [dict(zip(COLUMNS, r)) for r in cur.fetchall()]
        self.memory.sort(key=lambda s: -s["signal_ms"])

    def _persist(self, sig: Signal) -> None:
        self._persist_row(sig.row())

    def _persist_row(self, row: dict) -> None:
        self.memory = [m for m in self.memory if m["key"] != row["key"]]
        self.memory.append(row)
        self._db.execute(
            f"INSERT OR REPLACE INTO signals({', '.join(COLUMNS)}, mode)"
            f" VALUES({', '.join('?' * len(COLUMNS))}, ?)",
            tuple(row.get(c) for c in COLUMNS) + (self.mode,))
        self._db.commit()

    # ------------------------------------------------------------------ setup
    def prepare(self) -> None:
        if self.mode == "replay":
            for sym in self.symbols:
                for tf in self.tfs:
                    try:
                        s = labdata.load(sym, tf, DATA_DIR)
                    except FileNotFoundError:
                        continue
                    st = Stream(sym, tf, s)
                    start = int(np.searchsorted(s.t, START_MS // 60000))
                    st.i = max(start, WARMUP) - 1  # last closed bar before the clock starts
                    self.streams[(sym, tf)] = st
            self._rebuild_memory_from_db()
        else:
            self._prepare_live()

    def _rebuild_memory_from_db(self) -> None:
        self.memory = [m for m in self.memory if m["status"] in ("won", "lost", "expired")]

    # ------------------------------------------------------------------- step
    def step(self) -> bool:
        """Advance the earliest stream by one bar. Returns False when everything is done."""
        if self.mode != "replay":
            return False
        with self.lock:
            live = [(st.s.t[st.i + 1], key) for key, st in self.streams.items() if st.i + 1 < len(st.s)]
            if not live:
                self.finished = True
                return False
            t_next, key = min(live)
            st = self.streams[key]
            st.i += 1
            st.last_t = int(st.s.t[st.i])
            self.clock_ms = int(st.s.t[st.i]) * 60000
            self._on_bar(st, st.i)
            self.steps += 1
            return True

    def rewind(self) -> None:
        """Start the replay again from the beginning (a simulation is not a record)."""
        with self.lock:
            self._db.execute("DELETE FROM signals WHERE mode = 'replay'")
            self._db.commit()
            self.memory = []
            self.steps = 0
            self.finished = False
            self.streams = {}
            self.prepare()

    def _bands_for(self, p: float, tf: str | None = None) -> tuple[float, str] | None:
        cuts = self.band_tables.get(tf or "", self.band_cuts)
        for cut, letter in cuts:
            if p >= cut:
                return cut, letter
        return None

    def _on_bar(self, st: Stream, i: int, feats: dict | None = None, j: int | None = None,
                atr_now: float | None = None) -> None:
        """Score one bar, with features that may have been computed in bulk.

        `feats`/`j` let a caller that is walking many bars at once compute the 59
        features once over a window and read row `j` for each bar. That is what
        makes a 40-bar start-up scan of 1 300 streams take seconds instead of
        minutes; recomputing a 700-bar window per bar would be 40x the work.
        """
        s = st.s
        geom = self.geom(st.tf)
        horizon = int(geom["horizon"])
        h, l, o, c = float(s.h[i]), float(s.l[i]), float(s.o[i]), float(s.c[i])

        # 1. fill anything queued at this bar's open
        if st.pending is not None:
            sig = st.pending
            sig.entry, sig.entry_ms, sig.status, sig._fill_i = o, int(s.t[i]) * 60000, "open", i
            # barriers belong to the price the trade is actually filled at
            sig.stop = o - geom["stop"] * sig.atr
            sig.target = o + geom["target"] * sig.atr
            # the round trip costs `fee_rt` of the *price*; in R that is
            # fee_rt * price / (stop in price)
            risk_r = geom["stop"] * sig.atr
            sig.fee_r = self.fee_rt * o / risk_r if risk_r > 0 else None
            if sig.fee_r is not None:
                sig.break_even = (1 + sig.fee_r) / (1 + geom["rr"])
            st.pending = None
            st.open.append(sig)
            self._persist(sig)

        # 2. resolve open positions against this bar (both barriers = loss)
        still = []
        for sig in st.open:
            sig.bars_held += 1
            hit_t = h >= sig.target
            hit_s = l <= sig.stop
            if hit_s:
                sig.status = "lost"
                sig.pnl_r = -1.0
                sig.exit_ms = int(s.t[i]) * 60000
                self._persist(sig)
            elif hit_t:
                sig.status = "won"
                sig.pnl_r = geom["rr"]
                sig.exit_ms = int(s.t[i]) * 60000
                self._persist(sig)
            elif sig.bars_held >= horizon:
                sig.status = "expired"
                sig.pnl_r = 0.0
                sig.exit_ms = int(s.t[i]) * 60000
                self._persist(sig)
            else:
                still.append(sig)
        st.open = still

        # 3. look at the closed bar: is there a setup?
        if i < 60 or i < st.next_ok_bar or st.pending:
            return
        if feats is None:
            hist = max(0, i - WARMUP + 1)
            sl = s.slice(hist, i + 1)
            feats = labfeatures.features(sl)
            j = -1
            atr_now = float(atr_series(sl.h, sl.l, sl.c)[-1])
        try:
            row = [float(feats[n][j]) for n in self.model_features]
        except (KeyError, IndexError):
            return
        if not all(np.isfinite(row)):
            return
        x = np.array([row], dtype=np.float32)
        model = self.models.get(st.tf) or self.models[self.tfs[0]]
        p = float(model.predict_proba(x)[0, 1])
        band = self._bands_for(p, st.tf)
        if band is None or p < self.cut_for(st.tf):
            return
        atr = float(atr_now)
        if not np.isfinite(atr) or atr <= 0:
            return
        key = f"{st.symbol}:{st.tf}:{int(s.t[i])}"
        sig = Signal(key=key, symbol=st.symbol, tf=st.tf, side="long", band=band[1], prob=p,
                     signal_ms=int(s.t[i]) * 60000, atr=atr,
                     origin="scan" if self.backfilling else "live")
        # a preview only - the traded levels are set on fill, off the entry price
        sig.stop = c - geom["stop"] * atr
        sig.target = c + geom["target"] * atr
        st.pending = sig
        st.next_ok_bar = i + horizon   # the lock-out the research applied

    # ------------------------------------------------------------- live mode
    def client_or_create(self):  # pragma: no cover - needs the exchange
        if self.client is None:
            import httpx
            workers = int(getattr(self.config, "workers", 12) or 12)
            self.client = httpx.Client(
                timeout=float(getattr(self.config, "timeout_seconds", 20.0) or 20.0),
                limits=httpx.Limits(max_connections=workers * 2,
                                    max_keepalive_connections=workers))
        return self.client

    def resolve_universe(self) -> list[str]:  # pragma: no cover - needs the exchange
        """Explicit list from config.json, or the N most liquid contracts."""
        import feed
        cfg = self.config
        explicit = [mexc_symbol(s) for s in (cfg.symbols_final if cfg else self.symbols)]
        if explicit:
            return explicit
        top = int(getattr(cfg, "auto_top", 120) or 120) if cfg else len(self.symbols)
        try:
            return feed.symbols(self.client_or_create(), top=top) or explicit
        except Exception as exc:
            self.error = f"universe: {exc}"
            return explicit

    def _prepare_live(self) -> None:  # pragma: no cover - needs the exchange
        if self.assume_live:
            return
        self.poll_live(first=True)

    def tick_live(self) -> None:  # pragma: no cover - needs the exchange
        self.poll_live()

    def poll_live(self, first: bool = False) -> dict:  # pragma: no cover - needs the exchange
        """Fetch every symbol in parallel, then walk the bars closed since last time."""
        import feed

        if self.assume_live:
            return {"symbols": 0, "ok": 0, "failed": 0, "bars": 0}
        cfg = self.config
        workers = int(getattr(cfg, "workers", 12) or 12)
        scan = int(getattr(cfg, "scan_bars", 40) or 40)
        if self.universe is None:
            self.universe = self.resolve_universe()
        native = list(dict.fromkeys(NATIVE_FOR[tf] for tf in self.tfs))
        bars = max([NATIVE_BARS[n] for n in native] + [WARMUP + scan + 50])

        t0 = time.time()
        results: dict[str, dict] = {}
        failed: dict[str, str] = {}
        client = self.client_or_create()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self._fetch_symbol, client, sym, native, bars): sym
                    for sym in self.universe}
            try:
                for fut in as_completed(futs, timeout=max(60.0, len(self.universe) / workers * 30)):
                    sym = futs[fut]
                    try:
                        results[sym] = fut.result()
                    except Exception as exc:
                        failed[sym] = str(exc)[:120]
            except Exception as exc:            # a slow exchange must not hang the app
                self.error = f"poll timeout: {exc}"
        fresh = 0
        for sym, by_native in results.items():
            for tf in self.tfs:
                rows = by_native.get(NATIVE_FOR[tf]) or []
                if len(rows) < 100:
                    continue
                s = build_series(sym, NATIVE_FOR[tf], tf, rows)
                if s is None or len(s) < 100:
                    continue
                try:
                    fresh += self._advance(sym, tf, s, scan if first else 0)
                except Exception as exc:
                    failed[sym] = f"{tf}: {exc}"[:120]
        self.failed = failed
        self.last_poll = time.time()
        if self.streams:
            self.clock_ms = max(int(st.last_t) * 60000 for st in self.streams.values() if st.last_t)
        if failed and len(failed) == len(self.universe):
            self.error = f"every symbol failed (e.g. {next(iter(failed))}: {next(iter(failed.values()))})"
        elif failed:
            self.error = f"{len(failed)} of {len(self.universe)} symbols failed (e.g. {next(iter(failed))})"
        elif not results:
            self.error = "no symbol returned candles"
        else:
            self.error = None
        try:
            self._adopt_live()
        except Exception as exc:
            self.error = f"adopt: {exc}"
        return {"symbols": len(self.universe), "ok": len(results), "failed": len(failed),
                "bars": fresh, "seconds": round(time.time() - t0, 1)}

    def _fetch_symbol(self, client, sym: str, native: list[str], bars: int) -> dict:  # pragma: no cover
        import feed
        out = {}
        for nat in native:
            # native timeframes go through `feed.klines` (already the tested path);
            # the aggregated ones need the raw interval, so they ask for it directly
            rows = feed.klines(client, sym, nat, bars=bars)
            if rows:
                out[nat] = rows
        if not out:
            raise RuntimeError("no candles")
        return out

    def _advance(self, sym: str, tf: str, s, backfill: int) -> int:
        """Score every bar of `s` that this stream has not seen yet."""
        with self.lock:
            key = (sym, tf)
            st = self.streams.get(key)
            newest = len(s) - 2                       # the last bar is still forming
            if newest < 0:
                return 0
            if st is None:
                st = Stream(sym, tf, s)
                self.streams[key] = st
                # on the first poll, walk back over the last `backfill` closed bars
                # so the page has signals immediately instead of after an hour
                start = max(60, newest - backfill) if backfill else newest
                self.backfilling = start < newest
                n = self._walk(st, s, start, newest)
                self.backfilling = False
                return n
            if newest <= st.i:
                return 0
            st.s = s
            start = int(np.searchsorted(s.t, st.last_t, side="right"))
            start = min(max(start, st.i + 1), newest)
            return self._walk(st, s, start, newest)

    def _walk(self, st: Stream, s, start: int, newest: int) -> int:
        """Score bars `start..newest` in order, computing the features once."""
        if newest < start:
            return 0
        lo = max(0, start - WARMUP + 1)
        sl = s.slice(lo, newest + 1)
        feats = labfeatures.features(sl)
        atr = atr_series(sl.h, sl.l, sl.c)
        n = 0
        for i in range(start, newest + 1):
            st.i, st.last_t = i, int(s.t[i])
            self._on_bar(st, i, feats, i - lo, float(atr[i - lo]))
            n += 1
        return n

    def _adopt_live(self) -> None:  # pragma: no cover - needs the exchange
        """Re-open the trades that were still running when the app was stopped.

        A restart must not lose a live trade: the row is in SQLite, so it is looked
        up, its barriers are replayed over the candles that have arrived since, and
        whatever is still open is put back under supervision.
        """
        open_rows = [m for m in self.memory if m["status"] in ("open", "pending")]
        for row in open_rows:
            st = self.streams.get((row["symbol"], row["tf"]))
            if st is None:
                continue
            geom = self.geom(row["tf"])
            s = st.s
            if row["entry_ms"] is None:
                self._persist_row({**row, "status": "canceled", "pnl_r": 0.0})
                continue
            j = int(np.searchsorted(s.t, row["entry_ms"] // 60000))
            if j >= len(s) or row["target"] is None or row["stop"] is None:
                continue
            out = first_touch(s.h, s.l, np.array([j]), np.array([row["target"]]),
                              np.array([row["stop"]]), max(1, int(geom["horizon"])))
            if out[0] != 0:
                self._persist_row({**row, "status": "won" if out[0] == 1 else "lost",
                                   "pnl_r": geom["rr"] if out[0] == 1 else -1.0,
                                   "bars_held": int(len(s) - 1 - j),
                                   "exit_ms": int(s.t[len(s) - 2]) * 60000})
                continue
            if any(x.key == row["key"] for x in st.open):
                continue
            sig = Signal(key=row["key"], symbol=row["symbol"], tf=row["tf"], side=row["side"],
                         band=row["band"], prob=row["prob"], signal_ms=row["signal_ms"],
                         entry_ms=row["entry_ms"], entry=row["entry"], stop=row["stop"],
                         target=row["target"], atr=row["atr"], status="open",
                         fee_r=row.get("fee_r"), break_even=row.get("break_even"),
                         bars_held=int(len(s) - 1 - j), origin=row.get("origin", "live"))
            st.open.append(sig)
            self.adopted += 1

    # ------------------------------------------------------------------- view
    def size(self, row: dict) -> dict | None:
        """Position size for this trade at the configured risk, for the page.

        A queued signal has no entry yet - it will be filled at the next bar's
        open. Its stop and target previews are already fixed in ATR terms, so the
        price implied by those two levels is used and the card says `estimated`.
        """
        cfg = self.config
        if cfg is None or not row.get("stop") or not row.get("target"):
            return None
        geom = self.geom(row.get("tf", ""))
        rr = float(row.get("rr") or geom["rr"] or 0)
        entry = row.get("entry")
        estimated = False
        if not entry:
            # stop = c - s*A and target = c + t*A, so c = (rr*stop + target)/(rr+1)
            entry = (rr * float(row["stop"]) + float(row["target"])) / (rr + 1)
            estimated = True
        risk_unit = abs(float(entry) - float(row["stop"]))
        if risk_unit <= 0:
            return None
        budget = float(cfg.equity_usd) * float(cfg.risk_per_trade_pct) / 100.0
        qty = budget / risk_unit
        return {"qty": qty, "notional": qty * float(entry), "risk_usd": budget,
                "rr": rr, "reward_usd": budget * rr, "estimated_entry": estimated}

    def candidates(self, limit: int = 40) -> list[dict]:
        """What the user can act on right now: queued entries first, then open trades."""
        with self.lock:
            pend = [st.pending.row() for st in self.streams.values() if st.pending]
            open_ = [st.open[0].row() for st in self.streams.values() if st.open]
            pend.sort(key=lambda r: -r["prob"])
            open_.sort(key=lambda r: -r["prob"])
            out = pend + open_
            for r in out:
                r["rr"] = r.get("rr") or self.geom(r.get("tf", ""))["rr"]
                r["size"] = self.size(r)
            return out[:limit]

    def snapshot(self, limit: int = 300) -> dict:
        with self.lock:
            rows = sorted(self.memory, key=lambda r: -r["signal_ms"])[:limit]
            pend = [st.pending.row() for st in self.streams.values() if st.pending]
            open_ = [st.open[0].row() for st in self.streams.values() if st.open]
            for r in open_ + pend:
                r["rr"] = r.get("rr") or self.geom(r.get("tf", ""))["rr"]
                r["size"] = self.size(r)
            stats = self._stats()
            cfg = self.config.as_dict() if self.config is not None else None
            return {
                "mode": self.mode, "clock_ms": self.clock_ms, "steps": self.steps,
                "speed": self.speed, "finished": self.finished, "error": self.error,
                "symbols": self.symbols, "tfs": self.tfs,
                "missing_tfs": self.missing_tfs, "cut": self.record_cut(),
                "cut_override": self.cut,
                "cuts": {tf: self.cut_for(tf) for tf in self.tfs},
                "adopted": self.adopted,
                "model": self.model_label, "fee_bp": self.fee_rt * 1e4,
                "geometry": self.geometry,
                "geoms": {tf: self.geom(tf) for tf in self.tfs},
                "geoms_all": {tf: {**self.geom(tf),
                                   "scanned": tf in self.tfs,
                                   "trained": bool(tf in self.models)}
                              for tf in ALL_TFS},
                "horizon": self.horizon, "horizons": self.horizons(),
                "config": cfg, "last_poll": self.last_poll,
                "universe": len(self.universe) if self.universe else len(self.symbols),
                "failed": self.failed,
                "backfilling": self.backfilling,
                "streams": [{"symbol": st.symbol, "tf": st.tf,
                             "time_ms": int(st.s.t[st.i]) * 60000 if st.i >= 0 else None,
                             "bar": st.i, "bars": len(st.s)} for st in self.streams.values()],
                "signals": rows, "open": open_, "pending": pend,
                "candidates": self.candidates(int(getattr(self.config, "max_candidates", 40) or 40))
                if self.config is not None else pend + open_,
                "stats": stats,
            }

    def _stats(self) -> dict:
        out = {"all": _agg(self.memory, self.geometry["rr"])}
        by_band: dict[str, list[dict]] = {}
        by_tf: dict[str, list[dict]] = {}
        for r in self.memory:
            by_band.setdefault(r["band"], []).append(r)
            by_tf.setdefault(r["tf"], []).append(r)
        out["bands"] = {b: _agg(v, self.geometry["rr"]) for b, v in sorted(by_band.items())}
        out["by_tf"] = {tf: _agg(v, self.geom(tf)["rr"]) for tf, v in sorted(by_tf.items())}
        return out


def _agg(rows: list[dict], rr: float = 0.0) -> dict:
    won = sum(1 for r in rows if r["status"] == "won")
    lost = sum(1 for r in rows if r["status"] == "lost")
    exp = sum(1 for r in rows if r["status"] == "expired")
    res = won + lost
    fees = [r["fee_r"] for r in rows if r.get("fee_r") is not None]
    bes = [r["break_even"] for r in rows if r.get("break_even") is not None]
    net = float(sum((r["pnl_r"] or 0) - (r.get("fee_r") or 0) * (1 if r["status"] in ("won", "lost") else 0)
                    for r in rows))
    return {"n": len(rows), "won": won, "lost": lost, "expired": exp,
            "accuracy": (won / res) if res else None,
            "break_even": (sum(bes) / len(bes)) if bes else None,
            "fee_r": (sum(fees) / len(fees)) if fees else None,
            "expectancy": (net / res) if res else None,
            "net_r": net, "rr": rr,
            "open": sum(1 for r in rows if r["status"] in ("open", "pending"))}


def mexc_symbol(sym: str) -> str:
    """`BTCUSDT` -> `BTC_USDT`, the name MEXC futures uses. Anything else is left alone."""
    if "_" in sym or not sym.endswith("USDT") or len(sym) <= 4:
        return sym
    return sym[:-4] + "_USDT"


def build_series(symbol: str, native_tf: str, tf: str, rows: list[list[float]]):
    """Exchange candles -> the timeframe we want (aggregated when MEXC lacks it)."""
    if not rows or len(rows) < 2:
        return None
    a = np.array(rows, dtype=np.float64)
    order = np.argsort(a[:, 0])
    a = a[order]
    s = _series_from_rows(symbol, native_tf, a)
    if native_tf == tf:
        s.tf = tf
        s.bars_in = np.ones(len(a), dtype=np.int32)
        return s
    arr = np.stack([a[:, 1], a[:, 3], a[:, 4], a[:, 2], a[:, 5],
                    a[:, 5] * a[:, 2], np.ones(len(a)), a[:, 5] * 0.5], axis=1)
    return labdata.resample(symbol, tf, s.t, arr)


def _series_from_rows(symbol: str, tf: str, a: np.ndarray):
    t = (a[:, 0] / 60).astype(np.int64)
    # MEXC order: time, open, close, high, low, vol
    s = labdata.Series(symbol=symbol, tf=tf, t=t, o=a[:, 1], h=a[:, 3], l=a[:, 4],
                       c=a[:, 2], v=a[:, 5], qv=a[:, 5] * a[:, 2], n=np.ones(len(a)),
                       tb=a[:, 5] * 0.5)
    return s
