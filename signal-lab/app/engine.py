"""The signal engine.

Two modes, one pipeline:

  replay  walks the local 1-minute archive, bar by bar, at a clock you control.
          Everything the page shows is produced *in the order the bars arrived* -
          no look-ahead, no re-scoring of old bars with new information.
  live    polls MEXC futures for the same symbols and timeframes.

A signal is created when the model's probability of the target being touched
before the stop crosses the recording cut. It is **filled at the next bar's
open**, then resolved by the first barrier touched, with a bar that touches both
counted as a loss.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
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
from lab.outcomes import atr_series  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
START_MS = 1704067200000     # 2024-01-01 - the years the model never trained on
WARMUP = 700                 # bars of history before the first recorded signal
TF_TF_MIN = {"1h": 60, "4h": 240}


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


class Engine:
    def __init__(self, mode: str = "replay", speed: float = 25.0, cut: float | None = None,
                 symbols: list[str] | None = None, tfs: list[str] | None = None,
                 horizon: int | None = None, start_ms: int = START_MS, db: str | None = None):
        self.mode = mode
        self.speed = speed
        self.cut = cut
        self.horizon = horizon  # replaced by the model's own horizon below
        self.lock = threading.RLock()
        self.db_path = db or os.path.join(HERE, "data", "signals.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS signals(
            key TEXT PRIMARY KEY, symbol TEXT, tf TEXT, side TEXT, band TEXT, prob REAL,
            signal_ms INTEGER, entry_ms INTEGER, entry REAL, stop REAL, target REAL, atr REAL,
            status TEXT, bars_held INTEGER, pnl_r REAL, exit_ms INTEGER, mode TEXT)""")
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
        self.features = pack["features"]
        self.geometry = {"target": pack["target"], "stop": pack["stop"],
                         "rr": pack["target"] / pack["stop"],
                         "label": f"1 : {pack['target'] / pack['stop']:.0f}"}
        self.horizon = int(pack.get("horizon", 240))
        self.fee_rt = float(pack.get("fee_rt", 0.0005))
        self.model_features = sorted(self.features)
        with open(os.path.join(HERE, "data", "bands.json")) as fh:
            self.bands_meta = json.load(fh)
        ordered = sorted(self.bands_meta["bands"], key=lambda b: -b["cut"])
        self.band_cuts = [(b["cut"], "ABCDEFGH"[min(i, 7)]) for i, b in enumerate(ordered) if b["cut"] > 0]
        # a 1:4 model lives around p = 0.25-0.55, so the recording cut must come from
        # the measured bands, never from a number remembered from another geometry
        if self.cut is None:
            self.cut = float(self.bands_meta.get(
                "recommended_cut", min(c for c, _ in self.band_cuts)))

        self.symbols = symbols or ["BTCUSDT", "ETHUSDT"]
        self.tfs = tfs or ["1h"]
        self.streams: dict[tuple[str, str], Stream] = {}
        self.clock_ms = start_ms
        self.steps = 0
        self.finished = False
        self.error: str | None = None
        self.started = time.time()
        if mode == "replay":
            # a simulation is not a record: replay rows are rebuilt every run so a
            # restart cannot double-count the same trades
            self._db.execute("DELETE FROM signals WHERE mode = 'replay'")
            self._db.commit()
        self._load_signals()

    # ---------------------------------------------------------------- storage
    def _load_signals(self) -> None:
        cur = self._db.execute("SELECT key, symbol, tf, side, band, prob, signal_ms, entry_ms, entry,"
                               " stop, target, atr, status, bars_held, pnl_r, exit_ms FROM signals"
                               " WHERE mode = ?", (self.mode,))
        self.memory: list[dict] = []
        for r in cur.fetchall():
            self.memory.append(dict(zip(
                ["key", "symbol", "tf", "side", "band", "prob", "signal_ms", "entry_ms", "entry",
                 "stop", "target", "atr", "status", "bars_held", "pnl_r", "exit_ms"], r)))
        self.memory.sort(key=lambda s: -s["signal_ms"])

    def _persist(self, sig: Signal) -> None:
        self.memory = [m for m in self.memory if m["key"] != sig.key]
        self.memory.append(sig.row())
        self._db.execute("INSERT OR REPLACE INTO signals(key, symbol, tf, side, band, prob, signal_ms,"
                         " entry_ms, entry, stop, target, atr, status, bars_held, pnl_r, exit_ms, mode)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (sig.key, sig.symbol, sig.tf, sig.side, sig.band, sig.prob, sig.signal_ms,
                          sig.entry_ms, sig.entry, sig.stop, sig.target, sig.atr, sig.status,
                          sig.bars_held, sig.pnl_r, sig.exit_ms, self.mode))
        self._db.commit()

    # ------------------------------------------------------------------ setup
    def prepare(self) -> None:
        if self.mode == "replay":
            for sym in self.symbols:
                for tf in self.tfs:
                    if tf not in TF_TF_MIN:
                        continue
                    s = labdata.load(sym, tf, DATA_DIR)
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
            self.prepare()

    def _bands_for(self, p: float) -> tuple[float, str] | None:
        for cut, letter in self.band_cuts:
            if p >= cut:
                return cut, letter
        return None

    def _on_bar(self, st: Stream, i: int) -> None:
        s = st.s
        h, l, o, c = float(s.h[i]), float(s.l[i]), float(s.o[i]), float(s.c[i])

        # 1. fill anything queued at this bar's open
        if st.pending is not None:
            sig = st.pending
            sig.entry, sig.entry_ms, sig.status, sig._fill_i = o, int(s.t[i]) * 60000, "open", i
            # barriers belong to the price the trade is actually filled at
            sig.stop = o - self.geometry["stop"] * sig.atr
            sig.target = o + self.geometry["target"] * sig.atr
            # what the round trip costs, in R, and the accuracy this trade needs
            # the round trip costs `fee_rt` of the *price*; in R that is
            # fee_rt * price / (stop in price)
            risk_r = self.geometry["stop"] * sig.atr
            sig.fee_r = self.fee_rt * o / risk_r if risk_r > 0 else None
            if sig.fee_r is not None:
                sig.break_even = (1 + sig.fee_r) / (1 + self.geometry["rr"])
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
                sig.pnl_r = self.geometry["rr"]
                sig.exit_ms = int(s.t[i]) * 60000
                self._persist(sig)
            elif sig.bars_held >= self.horizon:
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
        hist = max(0, i - WARMUP + 1)
        sl = s.slice(hist, i + 1)
        f = labfeatures.features(sl)
        if not all(np.isfinite(f[n][-1]) for n in self.model_features):
            return
        x = np.array([[f[n][-1] for n in self.model_features]], dtype=np.float32)
        model = self.models.get(st.tf) or self.models[self.tfs[0]]
        p = float(model.predict_proba(x)[0, 1])
        band = self._bands_for(p)
        if band is None or p < self.cut:
            return
        atr = float(atr_series(sl.h, sl.l, sl.c)[-1])
        if not np.isfinite(atr) or atr <= 0:
            return
        key = f"{st.symbol}:{st.tf}:{int(s.t[i])}"
        sig = Signal(key=key, symbol=st.symbol, tf=st.tf, side="long", band=band[1], prob=p,
                     signal_ms=int(s.t[i]) * 60000, atr=atr)
        # a preview only - the traded levels are set on fill, off the entry price
        sig.stop = c - self.geometry["stop"] * atr
        sig.target = c + self.geometry["target"] * atr
        st.pending = sig
        st.next_ok_bar = i + self.horizon   # the lock-out the research applied

    # ------------------------------------------------------------- live mode
    def _prepare_live(self) -> None:  # pragma: no cover - needs the exchange
        import httpx
        import feed

        self.client = httpx.Client()
        for sym in self.symbols:
            for tf in self.tfs:
                try:
                    rows = feed.klines(self.client, sym, tf, bars=WARMUP)
                except Exception as exc:  # a bad symbol must not kill the app
                    self.error = f"{sym} {tf}: {exc}"
                    continue
                if len(rows) < 250:
                    continue
                self.streams[(sym, tf)] = Stream(sym, tf, _series_from_rows(sym, tf, rows))

    def tick_live(self) -> None:  # pragma: no cover - needs the exchange
        import feed
        for key, st in list(self.streams.items()):
            try:
                rows = feed.klines(self.client, key[0], key[1], bars=WARMUP)
            except Exception as exc:
                self.error = f"{key[0]} {key[1]}: {exc}"
                continue
            if not rows:
                continue
            s = _series_from_rows(key[0], key[1], rows)
            st.s = s
            st.i = len(s) - 2  # the last bar is still forming
            self.clock_ms = int(s.t[st.i]) * 60000
            self._on_bar(st, st.i)

    # ------------------------------------------------------------------- view
    def snapshot(self, limit: int = 300) -> dict:
        with self.lock:
            rows = sorted(self.memory, key=lambda r: -r["signal_ms"])[:limit]
            open_ = [st.open[0].row() for st in self.streams.values() if st.open]
            pend = [st.pending.row() for st in self.streams.values() if st.pending]
            stats = self._stats()
            return {
                "mode": self.mode, "clock_ms": self.clock_ms, "steps": self.steps,
                "speed": self.speed, "finished": self.finished, "error": self.error,
                "symbols": self.symbols, "tfs": self.tfs, "cut": self.cut,
                "model": self.model_label, "fee_bp": self.fee_rt * 1e4,
                "geometry": self.geometry, "horizon": self.horizon,
                "streams": [{"symbol": st.symbol, "tf": st.tf,
                             "time_ms": int(st.s.t[st.i]) * 60000 if st.i >= 0 else None,
                             "bar": st.i, "bars": len(st.s)} for st in self.streams.values()],
                "signals": rows, "open": open_, "pending": pend, "stats": stats,
            }

    def _stats(self) -> dict:
        out = {"all": _agg(self.memory, self.geometry["rr"])}
        by_band: dict[str, list[dict]] = {}
        for r in self.memory:
            by_band.setdefault(r["band"], []).append(r)
        out["bands"] = {b: _agg(v, self.geometry["rr"]) for b, v in sorted(by_band.items())}
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


def _series_from_rows(symbol: str, tf: str, rows: list[list[float]]):
    a = np.array(rows, dtype=np.float64)
    t = (a[:, 0] / 60).astype(np.int64)
    # MEXC order: time, open, close, high, low, vol
    s = labdata.Series(symbol=symbol, tf=tf, t=t, o=a[:, 1], h=a[:, 3], l=a[:, 4],
                       c=a[:, 2], v=a[:, 5], qv=a[:, 5] * a[:, 2], n=np.ones(len(a)),
                       tb=a[:, 5] * 0.5)
    return s
