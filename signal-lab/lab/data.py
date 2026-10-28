"""Market data: loading the 1-minute archive and resampling it.

The whole lab works off two files per symbol that are produced once by
``tools/build_dataset.py``:

    <SYMBOL>_1m.npy   float32, columns: open high low close volume quote_volume trades taker_buy
    <SYMBOL>_t.npy    int64, minutes since the unix epoch (UTC)

Everything above 1 minute is aggregated from the 1-minute bars *by minute
index*, never by position, so a gap in the feed cannot shift a 1-hour candle.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

COLS = ("open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy")
DATA_DIR = os.environ.get("SIGNAL_LAB_DATA", os.path.join(os.path.dirname(__file__), "..", "data"))

TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


@dataclass
class Series:
    """One instrument at one timeframe."""

    symbol: str
    tf: str
    t: np.ndarray  # int64 minutes, bar open time
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    qv: np.ndarray
    n: np.ndarray  # trade count
    tb: np.ndarray  # taker buy base volume

    def __len__(self) -> int:
        return len(self.t)

    def year(self) -> np.ndarray:
        return (self.t // (60 * 24 * 365)).astype(np.int32)  # unused, kept for symmetry

    def slice(self, lo: int, hi: int) -> "Series":
        return Series(self.symbol, self.tf, self.t[lo:hi], self.o[lo:hi], self.h[lo:hi],
                      self.l[lo:hi], self.c[lo:hi], self.v[lo:hi], self.qv[lo:hi],
                      self.n[lo:hi], self.tb[lo:hi])


def load_raw(symbol: str, data_dir: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    d = data_dir or DATA_DIR
    arr = np.load(os.path.join(d, f"{symbol}_1m.npy"))
    t = np.load(os.path.join(d, f"{symbol}_t.npy"))
    return arr, t


def load(symbol: str, tf: str = "1m", data_dir: str | None = None) -> Series:
    arr, t = load_raw(symbol, data_dir)
    if tf == "1m":
        return Series(symbol, tf, t, arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3],
                      arr[:, 4], arr[:, 5], arr[:, 6], arr[:, 7])
    return resample(symbol, tf, t, arr)


def resample(symbol: str, tf: str, t: np.ndarray, arr: np.ndarray) -> Series:
    step = TF_MINUTES[tf]
    g = t // step
    # boundaries of each group
    starts = np.flatnonzero(np.r_[True, g[1:] != g[:-1]])
    ends = np.r_[starts[1:], len(g)] - 1
    out = Series(
        symbol=symbol, tf=tf,
        t=starts * 0 + g[starts] * step,
        o=arr[starts, 0],
        h=np.maximum.reduceat(arr[:, 1], starts),
        l=np.minimum.reduceat(arr[:, 2], starts),
        c=arr[ends, 3],
        v=np.add.reduceat(arr[:, 4], starts),
        qv=np.add.reduceat(arr[:, 5], starts),
        n=np.add.reduceat(arr[:, 6], starts),
        tb=np.add.reduceat(arr[:, 7], starts),
    )
    # a bar built from fewer than the nominal minutes is a hole in the feed:
    # keep it, but mark it so features can drop it
    out.bars_in = (ends - starts + 1).astype(np.int32)
    return out


def index_of(t: np.ndarray, ts_ms: int) -> int:
    """Position of the first bar at or after a UTC millisecond timestamp."""
    return int(np.searchsorted(t, ts_ms // 60000))


def utc(t_min: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(int(t_min) * 60, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def minute_of_day(t: np.ndarray) -> np.ndarray:
    return (t % 1440).astype(np.int32)


def day_of_week(t: np.ndarray) -> np.ndarray:
    # 1970-01-01 was a Thursday; minutes since epoch -> weekday 0=Mon
    return ((t // 1440 + 3) % 7).astype(np.int32)
