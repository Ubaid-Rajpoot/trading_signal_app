"""Long-range historical candles (paged downloads + local cache).

Binance returns at most 1000 klines per request, so a 30-day window on the 1m
chart (43,200 bars) has to be paged. Downloads are cached on disk as CSV, so a
backtest can be re-run instantly and works offline once the data is there.

Order of preference:
  1. fresh disk cache covering the requested window
  2. live Binance (paged)
  3. deterministic demo candles, clearly labelled ``demo``
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pandas as pd

from ...config import BINANCE_HOSTS, CACHE_DIR, HTTP_TIMEOUT, TIMEFRAMES, USER_AGENT
from ..utils_paths import history_cache_path
from . import real
from .synthetic import generate_candles

_MAX_BARS_PER_REQUEST = 1000
_PACE_SECONDS = 0.12  # stay well inside the exchange rate limit


def _parse_klines(rows: list) -> pd.DataFrame:
    return pd.DataFrame(
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


def download_range(symbol: str, timeframe: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Page through Binance klines until ``end_ms`` is covered."""
    interval = str(TIMEFRAMES[timeframe]["interval"])
    frames: list[pd.DataFrame] = []
    cursor = start_ms
    last_error: Exception | None = None

    for host in BINANCE_HOSTS:
        frames.clear()
        cursor = start_ms
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
                while cursor < end_ms:
                    resp = client.get(
                        f"{host}/api/v3/klines",
                        params={
                            "symbol": symbol.upper(),
                            "interval": interval,
                            "startTime": cursor,
                            "endTime": end_ms,
                            "limit": _MAX_BARS_PER_REQUEST,
                        },
                    )
                    if resp.status_code != 200:
                        raise RuntimeError(f"{host} -> HTTP {resp.status_code}: {resp.text[:120]}")
                    rows = resp.json()
                    if not rows:
                        break
                    chunk = _parse_klines(rows)
                    frames.append(chunk)
                    next_cursor = int(chunk["time"].iloc[-1]) + int(TIMEFRAMES[timeframe]["seconds"]) * 1000
                    if next_cursor <= cursor:  # no progress - stop rather than loop forever
                        break
                    cursor = next_cursor
                    if len(rows) < _MAX_BARS_PER_REQUEST:
                        break
                    time.sleep(_PACE_SECONDS)
            if frames:
                df = pd.concat(frames, ignore_index=True).drop_duplicates("time").sort_values("time")
                now_ms = int(time.time() * 1000)
                df = df[df["close_time"] <= now_ms].reset_index(drop=True)
                return df.drop(columns=["close_time"])
            raise RuntimeError("no candles returned")
        except Exception as exc:  # try the next host
            last_error = exc
            continue

    raise RuntimeError(f"Live download failed: {last_error}")


def _cache_meta(path: Path) -> dict:
    import json

    meta_file = path.with_suffix(".json")
    if not meta_file.exists():
        return {}
    try:
        return json.loads(meta_file.read_text())
    except Exception:
        return {}


def _read_cache(path: Path) -> tuple[pd.DataFrame, dict]:
    df = pd.read_csv(path)
    return df, _cache_meta(path)


def _write_cache(path: Path, df: pd.DataFrame, start_ms: int, end_ms: int, source: str) -> None:
    import json

    df.to_csv(path, index=False)
    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "start": int(df["time"].iloc[0]),
                "end": int(df["time"].iloc[-1]),
                "requested_start": int(start_ms),
                "requested_end": int(end_ms),
                "bars": int(len(df)),
                "source": source,
                "fetched_at": int(time.time() * 1000),
            }
        )
    )


def _cache_covers(df: pd.DataFrame, start_ms: int, end_ms: int, timeframe: str) -> bool:
    if df.empty:
        return False
    bar_ms = int(TIMEFRAMES[timeframe]["seconds"]) * 1000
    return int(df["time"].iloc[0]) <= start_ms + bar_ms * 2 and int(df["time"].iloc[-1]) >= end_ms - bar_ms * 5


def get_history(
    symbol: str,
    timeframe: str,
    days: int,
    allow_demo: bool = True,
    refresh: bool = False,
) -> tuple[pd.DataFrame, str]:
    """Return ``(candles, source)`` for the last ``days`` days.

    source is ``live`` (downloaded now), ``dataset`` (real candles from a local
    Binance dump), ``cache`` (previously downloaded) or ``demo`` (deterministic
    synthetic candles - NOT real market data).
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe '{timeframe}'")
    symbol = symbol.upper()
    seconds = int(TIMEFRAMES[timeframe]["seconds"])
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000

    path = history_cache_path(symbol, timeframe, days)

    # A real dataset on disk is the best offline option: it is genuine exchange
    # data, so it beats both a stale cache and the synthetic feed.
    if real.has_dataset(symbol) and not refresh:
        try:
            frame, source = real.get_dataset_history(symbol, timeframe, days)
            if len(frame) > 10:
                return frame, source
        except Exception:
            pass

    if path.exists() and not refresh:
        try:
            cached, meta = _read_cache(path)
            if _cache_covers(cached, start_ms, now_ms, timeframe):
                age = (now_ms - int(meta.get("fetched_at", 0))) / 1000
                # Small windows refresh quickly; long ones are fine for a day.
                if age < max(3600, seconds * 3):
                    return cached, "cache"
        except Exception:
            pass

    try:
        df = download_range(symbol, timeframe, start_ms, now_ms)
        df = df[df["time"] >= start_ms].reset_index(drop=True)
        _write_cache(path, df, start_ms, now_ms, "live")
        return df, "live"
    except Exception:
        # If a stale cache exists, it is still real data - prefer it to synthetic.
        if path.exists():
            try:
                cached, _ = _read_cache(path)
                if len(cached) > 100:
                    return cached, "cache"
            except Exception:
                pass
        if not allow_demo:
            raise RuntimeError("Live data unavailable and no cache present")

    bars_needed = max(int(days * 24 * 3600 / seconds), 200)
    demo = generate_candles(symbol, timeframe, limit=bars_needed)
    return demo, "demo"


def probe(symbol: str, timeframe: str, days: int = 30, allow_demo: bool = True) -> dict:
    """Explain exactly where the candles for this window would come from.

    Hits the exchange directly (one small request per host), reports the cache,
    then reports what ``get_history`` would actually return. Used by
    ``scripts/winrate.py --explain`` so a data source can never be a mystery.
    """
    from datetime import datetime, timezone

    def iso(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    dataset = None
    if real.has_dataset(symbol):
        try:
            dataset = real.coverage(symbol)
        except Exception:
            dataset = None

    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe '{timeframe}'")
    symbol = symbol.upper()
    seconds = int(TIMEFRAMES[timeframe]["seconds"])
    interval = str(TIMEFRAMES[timeframe]["interval"])
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    path = history_cache_path(symbol, timeframe, days)

    # --- 1. do the exchanges answer at all? ---------------------------------
    hosts = []
    for host in BINANCE_HOSTS:
        t0 = time.time()
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
                resp = client.get(
                    f"{host}/api/v3/klines",
                    params={"symbol": symbol, "interval": interval, "limit": 2},
                )
            ok = resp.status_code == 200
            sample = resp.json()[-1] if ok and resp.json() else None
            hosts.append(
                {
                    "host": host,
                    "ok": ok,
                    "ms": int((time.time() - t0) * 1000),
                    "detail": (
                        f"HTTP {resp.status_code}, last close {float(sample[4]):,.2f} at {iso(int(sample[0]))}"
                        if sample
                        else f"HTTP {resp.status_code}: {resp.text[:100]}"
                    ),
                }
            )
        except Exception as exc:
            hosts.append(
                {
                    "host": host,
                    "ok": False,
                    "ms": int((time.time() - t0) * 1000),
                    "detail": f"{type(exc).__name__}: {str(exc)[:110]}",
                }
            )

    # --- 2. is there a cache? ------------------------------------------------
    cache = {"path": str(path), "exists": False}
    if path.exists():
        try:
            cached, meta = _read_cache(path)
            cache.update(
                {
                    "exists": True,
                    "bars": int(len(cached)),
                    "covers_window": _cache_covers(cached, start_ms, now_ms, timeframe),
                    "from": iso(int(cached["time"].iloc[0])),
                    "to": iso(int(cached["time"].iloc[-1])),
                    "fetched_at": (
                        iso(int(meta["fetched_at"])) if meta.get("fetched_at") else "unknown"
                    ),
                    "recorded_source": meta.get("source", "unknown"),
                }
            )
        except Exception as exc:
            cache["error"] = str(exc)

    # --- 3. what would actually be used? ------------------------------------
    df, source = get_history(symbol, timeframe, days, allow_demo=allow_demo)
    bars_expected = int(days * 24 * 3600 / seconds)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "days": days,
        "window": {"start": iso(start_ms), "end": iso(now_ms)},
        "bar_seconds": seconds,
        "bars_expected": bars_expected,
        "hosts": hosts,
        "hosts_ok": [h["host"] for h in hosts if h["ok"]],
        "dataset": dataset,
        "cache": cache,
        "source": source,
        "dataset": real.has_dataset(symbol),
        "bars": int(len(df)),
        "first_candle": iso(int(df["time"].iloc[0])),
        "last_candle": iso(int(df["time"].iloc[-1])),
        "first_close": round(float(df["close"].iloc[0]), 2),
        "last_close": round(float(df["close"].iloc[-1]), 2),
        "is_real": source in ("live", "dataset", "cache"),
    }
