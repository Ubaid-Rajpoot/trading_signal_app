"""Real exchange candles that live on disk - no network needed.

The exchange REST API is not reachable from every network (and not at all from
some sandboxes). Instead of falling back to synthetic data, the app can read
**real Binance klines** from a local dataset:

    backend/data/cache/datasets/<SYMBOL>/*.csv

Those files are the official Binance data format - daily or monthly kline dumps,
the same files ``data.binance.vision`` serves (they can also be downloaded from a
GitHub mirror, see ``scripts/fetch_real_data.py``). This module:

* merges the daily files into one 1-minute series (deduped, sorted),
* resamples that to any timeframe **from the real 1m bars** - so 3m/5m/15m/30m/1h
  and 4h are all genuine, not separate downloads that could disagree,
* caches the result per timeframe so the second read is instant,
* reports ``source="dataset"`` so nothing downstream can mistake it for a demo.

One minute of base data is enough: every supported timeframe is a whole number of
minutes, so resampling loses nothing (the first bar of each bucket sets the open,
the last sets the close, extremes set high/low, volume is summed).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pandas as pd

from ...config import CACHE_DIR, TIMEFRAMES

DATASET_DIR = CACHE_DIR / "datasets"
_LOCK = threading.Lock()
_BASE: dict[str, pd.DataFrame] = {}

# Binance kline CSV order (data.binance.vision / API dumps).
_COLUMNS = [
    "time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
]


def _normalise_time(values: pd.Series) -> pd.Series:
    """Binance dumps use milliseconds; older mirrors and some tools use µs or s."""
    median = float(values.iloc[0]) if len(values) else 0.0
    if median > 1e14:  # microseconds
        return (values // 1000).astype("int64")
    if median > 1e11:  # milliseconds
        return values.astype("int64")
    return (values * 1000).astype("int64")  # seconds


def reload() -> None:
    """Drop the in-memory cache after new files are imported."""
    with _LOCK:
        _BASE.clear()


def dataset_dir(symbol: str) -> Path:
    return DATASET_DIR / symbol.upper()


def has_dataset(symbol: str) -> bool:
    folder = dataset_dir(symbol)
    return folder.is_dir() and any(folder.glob("*.csv"))


def dataset_symbols() -> list[str]:
    if not DATASET_DIR.is_dir():
        return []
    return sorted(p.name for p in DATASET_DIR.iterdir() if p.is_dir() and has_dataset(p.name))


def load_base(symbol: str, refresh: bool = False) -> pd.DataFrame:
    """The raw 1-minute series for ``symbol``, merged from every CSV in its folder."""
    symbol = symbol.upper()
    with _LOCK:
        if not refresh and symbol in _BASE:
            return _BASE[symbol]

    folder = dataset_dir(symbol)
    files = sorted(folder.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"no dataset for {symbol} in {folder}")

    parts: list[pd.DataFrame] = []
    for path in files:
        frame = pd.read_csv(path, header=None, names=_COLUMNS)
        if "open" not in frame.columns:  # a header row slipped in
            frame = pd.read_csv(path)
        parts.append(frame[["time", "open", "high", "low", "close", "volume"]])

    data = pd.concat(parts, ignore_index=True)
    data["time"] = _normalise_time(data["time"])
    data = data.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    for column in ("open", "high", "low", "close", "volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "high", "low", "close"])

    with _LOCK:
        _BASE[symbol] = data
    return data


def resample(base: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate a 1-minute series into ``timeframe`` (open time, left labelled)."""
    seconds = int(TIMEFRAMES[timeframe]["seconds"])
    if seconds < 60:
        raise ValueError("dataset base data is 1m; sub-minute timeframes are not supported")
    if seconds == 60:
        return base.copy()

    indexed = base.set_index(pd.to_datetime(base["time"], unit="ms"))
    rule = f"{seconds // 60}min" if seconds < 3600 else f"{seconds // 3600}h"
    frame = indexed.resample(rule, label="left", closed="left", origin="epoch").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame.index.name = "ts"
    frame = frame.reset_index()
    # datetime64 units vary (ms/µs/ns) with the pandas version, so convert
    # through a Timedelta instead of dividing raw integers.
    frame["time"] = (
        (frame["ts"] - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)
    ).astype("int64")
    return frame[["time", "open", "high", "low", "close", "volume"]]


def get_dataset_history(
    symbol: str,
    timeframe: str,
    days: int | None = None,
    refresh: bool = False,
) -> tuple[pd.DataFrame, str]:
    """``(candles, "dataset")`` for the last ``days`` days of real 1m data."""
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe '{timeframe}'")
    base = load_base(symbol, refresh=refresh)
    frame = resample(base, timeframe)
    if days:
        cutoff = int(frame["time"].max()) - int(days * 24 * 3600 * 1000)
        frame = frame[frame["time"] >= cutoff]
    return frame.reset_index(drop=True), "dataset"


def coverage(symbol: str) -> dict:
    """What the dataset for this symbol actually contains."""
    base = load_base(symbol)
    return {
        "symbol": symbol.upper(),
        "files": len(list(dataset_dir(symbol).glob("*.csv"))),
        "bars_1m": int(len(base)),
        "first": pd.to_datetime(int(base["time"].iloc[0]), unit="ms").strftime("%Y-%m-%d %H:%M UTC"),
        "last": pd.to_datetime(int(base["time"].iloc[-1]), unit="ms").strftime("%Y-%m-%d %H:%M UTC"),
        "timeframes": [tf for tf in TIMEFRAMES if int(TIMEFRAMES[tf]["seconds"]) >= 60],
    }
