"""Deterministic demo OHLCV generator.

The sandbox (and some regions) cannot reach Binance. Rather than showing an
empty app, we generate a realistic, *stable* candle series per symbol so every
feature - charting, signals, backtests, alerts - is fully usable offline. The
API always reports ``source: "demo"`` so the UI can label it clearly.

Stability matters: bar ``i`` always has exactly the same OHLCV no matter when it
is requested, so history never rewrites itself between refreshes. Randomness is
derived per aligned block of bars from a hash of (symbol, timeframe, block), and
price levels come from a two-level random walk (block drift + within-block
noise) plus a slow cyclical trend so indicators have real structure to find.
"""

from __future__ import annotations

import hashlib
import math
import time
from functools import lru_cache

import numpy as np
import pandas as pd

from ...config import DEMO_ANCHOR_PRICES, TIMEFRAMES

_EPOCH = 1_704_067_200  # 2024-01-01T00:00:00Z
_BLOCK = 512
_VOL_BY_TF = {"1m": 0.0009, "5m": 0.0017, "15m": 0.0026, "1h": 0.0045, "4h": 0.010, "1d": 0.026}


def _seed(*parts: str | int) -> int:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _anchor_price(symbol: str) -> float:
    if symbol in DEMO_ANCHOR_PRICES:
        return DEMO_ANCHOR_PRICES[symbol]
    rng = np.random.default_rng(_seed(symbol))
    return float(np.exp(rng.uniform(math.log(0.05), math.log(400.0))))


def _step_seconds(timeframe: str) -> int:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return int(TIMEFRAMES[timeframe]["seconds"])


# --------------------------------------------------------------- bar streams
@lru_cache(maxsize=512)
def _block_returns(symbol: str, timeframe: str, block: int) -> np.ndarray:
    """Log-returns for bars inside one block (stable per absolute index)."""
    rng = np.random.default_rng(_seed(symbol, timeframe, "returns", block))
    sigma = _VOL_BY_TF.get(timeframe, 0.01)
    i = np.arange(block * _BLOCK, (block + 1) * _BLOCK, dtype=np.float64)
    vol_env = 0.6 + 0.9 * np.abs(np.sin(2 * np.pi * i / 610.0 + (_seed(symbol) % 100)))
    shock = rng.standard_normal(_BLOCK) * sigma * vol_env
    jump = np.where(rng.random(_BLOCK) < 0.0012, rng.normal(0.0, sigma * 6.0, _BLOCK), 0.0)
    return shock + jump


@lru_cache(maxsize=256)
def _block_noise(symbol: str, timeframe: str, block: int) -> np.ndarray:
    """Per-bar shape/volume randomness: (open_noise, wick_a, wick_b, vol)."""
    rng = np.random.default_rng(_seed(symbol, timeframe, "shape", block))
    return np.stack(
        [
            rng.normal(0.0, 1.0, _BLOCK),
            rng.random(_BLOCK),
            rng.random(_BLOCK),
            rng.random(_BLOCK),
        ]
    )


@lru_cache(maxsize=128)
def _block_cumulative(symbol: str, timeframe: str, up_to: int) -> np.ndarray:
    """Cumulative log-return at the start of each block 0..up_to (inclusive)."""
    cum = np.zeros(up_to + 2)
    running = 0.0
    for b in range(up_to + 1):
        cum[b] = running
        running += float(_block_returns(symbol, timeframe, b).sum())
    cum[up_to + 1] = running
    return cum


def _gather(start_idx: int, length: int, fetch) -> np.ndarray:
    """Concatenate per-block arrays covering [start_idx, start_idx+length)."""
    first = start_idx // _BLOCK
    last = (start_idx + length - 1) // _BLOCK
    chunks = [np.asarray(fetch(b)) for b in range(first, last + 1)]
    joined = np.concatenate(chunks, axis=-1)
    offset = start_idx - first * _BLOCK
    return joined[..., offset: offset + length]


def _trend_component(symbol: str, timeframe: str, idx: np.ndarray) -> np.ndarray:
    """Slow, bounded cycles so price oscillates around its anchor."""
    sigma = _VOL_BY_TF.get(timeframe, 0.01)
    scale = max(sigma * 28.0, 0.05)
    phase = _seed(symbol) % 360
    return scale * (
        0.55 * np.sin(2 * np.pi * idx / 430.0 + phase)
        + 0.30 * np.sin(2 * np.pi * idx / 97.0 + phase * 1.7)
        + 0.15 * np.sin(2 * np.pi * idx / 29.0 + phase * 2.3)
    )


@lru_cache(maxsize=512)
def _log_price_window(symbol: str, timeframe: str, start_idx: int, length: int) -> tuple:
    last_block = (start_idx + length - 1) // _BLOCK
    cum = _block_cumulative(symbol, timeframe, last_block)

    first = start_idx // _BLOCK

    def block_log_returns(b: int) -> np.ndarray:
        return cum[b] + np.cumsum(_block_returns(symbol, timeframe, b))

    log_cum = _gather(start_idx, length, block_log_returns)
    idx = np.arange(start_idx, start_idx + length, dtype=np.float64)
    log_price = math.log(_anchor_price(symbol)) + log_cum + _trend_component(symbol, timeframe, idx)
    return tuple(np.exp(log_price).tolist())


def generate_candles(
    symbol: str,
    timeframe: str,
    limit: int = 400,
    now: float | None = None,
) -> pd.DataFrame:
    """Return ``limit`` demo candles ending at the current (closed) bar."""
    symbol = symbol.upper()
    step = _step_seconds(timeframe)
    sigma = _VOL_BY_TF.get(timeframe, 0.01)
    now = time.time() if now is None else now
    end_ts = int(math.floor(now / step) * step)

    start_idx = max(0, int((end_ts - (limit - 1) * step - _EPOCH) // step))
    # One extra bar so the first open has a real previous close.
    closes = np.asarray(_log_price_window(symbol, timeframe, max(start_idx - 1, 0), limit + 1))
    prev_close = closes[:-1]
    closes = closes[1:]

    noise = _gather(max(start_idx - 1, 0), limit + 1, lambda b: _block_noise(symbol, timeframe, b))[:, 1:]
    open_noise, wick_a, wick_b, vol_noise = noise

    open_px = prev_close * (1 + open_noise * 0.0003)
    body_hi = np.maximum(open_px, closes)
    body_lo = np.minimum(open_px, closes)
    wick = (0.4 + wick_a) * closes * sigma * 0.55
    high = body_hi + wick * (0.2 + wick_b)
    low = np.maximum(body_lo - wick * (0.2 + wick_a), 1e-9)

    bar_move = np.abs(np.diff(np.log(closes), prepend=np.log(closes[0])))
    base_vol = 1_500_000 if closes[-1] > 1 else 40_000_000
    volume = base_vol * (0.35 + vol_noise) * (1 + 55 * bar_move)

    times_ms = (np.arange(end_ts - (limit - 1) * step, end_ts + step, step) * 1000).astype(np.int64)

    return pd.DataFrame(
        {
            "time": times_ms,
            "open": open_px,
            "high": high,
            "low": low,
            "close": closes,
            "volume": volume,
        }
    )
