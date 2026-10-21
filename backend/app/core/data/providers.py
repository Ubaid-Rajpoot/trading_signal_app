"""Market data access with a live -> cache -> demo provider chain.

Priority:
1. Binance public REST (no key needed) for real market data.
2. A recently cached CSV if Binance is unreachable.
3. Deterministic synthetic candles, clearly reported as ``demo``.

The active provider is memoised briefly so an offline environment does not pay
an 8s timeout on every request in the watchlist.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pandas as pd

from ...config import (
    BINANCE_HOSTS,
    CACHE_DIR,
    HTTP_TIMEOUT,
    TIMEFRAMES,
    USER_AGENT,
)
from . import real
from .synthetic import generate_candles

_LOCK = threading.Lock()
_PROVIDER_STATE: dict[str, float] = {"live_failed_at": 0.0}
_PROVIDER_TTL = 90.0  # seconds to stop retrying a failing live endpoint
_MEMORY_CACHE: dict[tuple[str, str, int], tuple[float, pd.DataFrame, str]] = {}
_MEMORY_TTL = 20.0


class MarketDataError(Exception):
    pass


def _cache_path(symbol: str, timeframe: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}_{timeframe}.csv"


def _meta_path(symbol: str, timeframe: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}_{timeframe}.json"


def _read_csv_cache(symbol: str, timeframe: str, max_age: float) -> pd.DataFrame | None:
    path = _cache_path(symbol, timeframe)
    meta = _meta_path(symbol, timeframe)
    if not path.exists() or not meta.exists():
        return None
    try:
        info = json.loads(meta.read_text())
        if time.time() - float(info.get("fetched_at", 0)) > max_age:
            return None
        df = pd.read_csv(path)
        return df
    except Exception:  # pragma: no cover - corrupt cache should never break the app
        return None


def _write_csv_cache(symbol: str, timeframe: str, df: pd.DataFrame, source: str) -> None:
    try:
        df.to_csv(_cache_path(symbol, timeframe), index=False)
        _meta_path(symbol, timeframe).write_text(
            json.dumps({"fetched_at": time.time(), "source": source})
        )
    except Exception:  # pragma: no cover
        pass


def _normalise_klines(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(
        [
            {
                "time": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "close_time": int(r[6]),
            }
            for r in rows
        ]
    )
    return df


def fetch_binance(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    interval = TIMEFRAMES[timeframe]["interval"]
    params = {"symbol": symbol.upper(), "interval": interval, "limit": min(int(limit), 1000)}
    last_error: Exception | None = None
    for host in BINANCE_HOSTS:
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
                resp = client.get(f"{host}/api/v3/klines", params=params)
                if resp.status_code == 200:
                    rows = resp.json()
                    if not rows:
                        raise MarketDataError(f"No candles returned for {symbol}")
                    df = _normalise_klines(rows)
                    # Drop the still-forming candle so indicators/signals only
                    # ever see closed bars.
                    now_ms = int(time.time() * 1000)
                    df = df[df["close_time"] <= now_ms].reset_index(drop=True)
                    if df.empty:
                        raise MarketDataError("Only an unclosed candle was returned")
                    return df.drop(columns=["close_time"])
                last_error = MarketDataError(f"{host} -> HTTP {resp.status_code}")
        except Exception as exc:  # network errors, invalid symbol, ...
            last_error = exc
    raise MarketDataError(str(last_error) if last_error else "Binance unavailable")


def provider_status() -> dict:
    """Cheap health snapshot used by the UI banner."""
    symbols = real.dataset_symbols()
    if symbols:
        try:
            info = real.coverage(symbols[0])
            return {
                "mode": "dataset",
                "reason": (
                    f"Real Binance candles from a local dataset: "
                    f"{', '.join(symbols)} {info['first']} to {info['last']}"
                ),
                "datasets": symbols,
            }
        except Exception:
            pass
    with _LOCK:
        failed_at = _PROVIDER_STATE["live_failed_at"]
    if failed_at and time.time() - failed_at < _PROVIDER_TTL:
        return {"mode": "demo", "reason": "Live exchange unreachable - showing demo data"}
    return {"mode": "live", "reason": "Live Binance market data"}


def get_candles(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 400,
    force_refresh: bool = False,
    replay_bars: int = 0,
) -> tuple[pd.DataFrame, str]:
    """Return ``(candles, source)``.

    source is ``live`` (exchange now), ``dataset`` (real candles from a local
    Binance dump), ``cache`` (previously downloaded) or ``demo`` (synthetic).

    ``replay_bars`` shifts the window back N bars. It only applies to a dataset
    (which has a fixed end), so you can inspect any moment of real history as if
    you were watching it live.
    """
    symbol = symbol.upper().strip()
    if timeframe not in TIMEFRAMES:
        raise MarketDataError(f"Unsupported timeframe '{timeframe}'")
    limit = max(30, min(int(limit), 1500))
    if replay_bars:
        replay_bars = max(0, int(replay_bars))
        frame, _ = real.get_dataset_history(symbol, timeframe)
        end = len(frame) - replay_bars
        window = frame.iloc[max(0, end - limit):end].reset_index(drop=True)
        if window.empty:
            raise MarketDataError(f"replay {replay_bars} bars is past the start of the data")
        return window, "dataset"

    key = (symbol, timeframe, limit)
    now = time.time()
    if not force_refresh:
        with _LOCK:
            hit = _MEMORY_CACHE.get(key)
        if hit and now - hit[0] < _MEMORY_TTL:
            return hit[1].copy(), hit[2]

    tf_seconds = int(TIMEFRAMES[timeframe]["seconds"])
    with _LOCK:
        failed_recently = now - _PROVIDER_STATE["live_failed_at"] < _PROVIDER_TTL

    if not failed_recently:
        try:
            df = fetch_binance(symbol, timeframe, limit)
            _write_csv_cache(symbol, timeframe, df, "live")
            with _LOCK:
                _MEMORY_CACHE[key] = (now, df, "live")
                _PROVIDER_STATE["live_failed_at"] = 0.0
            return df.copy(), "live"
        except Exception:
            with _LOCK:
                _PROVIDER_STATE["live_failed_at"] = now

    # Real candles already on disk beat both a stale cache and synthetic data.
    if real.has_dataset(symbol):
        try:
            frame, _ = real.get_dataset_history(symbol, timeframe)
            if len(frame) >= 30:
                window = frame.tail(limit).reset_index(drop=True)
                with _LOCK:
                    _MEMORY_CACHE[key] = (now, window, "dataset")
                return window.copy(), "dataset"
        except Exception:
            pass

    # Fall back to a recent disk cache before fabricating data.
    cached = _read_csv_cache(symbol, timeframe, max_age=tf_seconds * 3)
    if cached is not None and len(cached) >= 30:
        cached = cached.tail(limit).reset_index(drop=True)
        with _LOCK:
            _MEMORY_CACHE[key] = (now, cached, "cache")
        return cached.copy(), "cache"

    demo = generate_candles(symbol, timeframe, limit)
    with _LOCK:
        _MEMORY_CACHE[key] = (now, demo, "demo")
    return demo.copy(), "demo"


def latest_price(symbol: str, timeframe: str = "1h") -> float:
    df, _ = get_candles(symbol, timeframe, limit=30)
    return float(df["close"].iloc[-1])
