"""Deterministic demo OHLCV generator.

The sandbox (and some regions) cannot reach Binance. Rather than showing an
empty app, we generate a realistic, *stable* candle series per symbol so every
feature - charting, signals, backtests, alerts - is fully usable offline. The
API always reports ``source: "demo"`` so the UI can label it clearly.

Two properties make this useful rather than confusing:

1. **Stable** - a given bar always has the same OHLCV, whenever it is requested,
   so history never rewrites itself between refreshes.
2. **Consistent across timeframes** - the underlying price path is generated on
   a *minute grid keyed by wall-clock time*, and every timeframe samples that
   same path. The 1m close and the 30m close at 12:00 are the same number, so
   BTC trades around one believable level everywhere.
"""

from __future__ import annotations

import hashlib
import math
import time
from functools import lru_cache

import numpy as np
import pandas as pd

from ...config import DEMO_ANCHOR_PRICES, TIMEFRAMES

_EPOCH = 1_735_689_600  # 2025-01-01T00:00:00Z (demo history starts here)
_BLOCK = 512             # minutes per random-walk block
_VOL_BY_TF = {
    "1m": 0.0009,
    "3m": 0.0013,
    "5m": 0.0017,
    "15m": 0.0026,
    "30m": 0.0034,
    "1h": 0.0045,
    "4h": 0.010,
    "1d": 0.026,
}
# The walk is generated once on the minute grid and reused by every timeframe.
_MINUTE_VOL = 0.0009


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


# ------------------------------------------------------------- minute grid
@lru_cache(maxsize=1024)
def _minute_block(symbol: str, block: int) -> np.ndarray:
    """Log-returns for one block of 512 minutes (stable per absolute block)."""
    rng = np.random.default_rng(_seed(symbol, "minutes", block))
    minute_index = np.arange(block * _BLOCK, (block + 1) * _BLOCK, dtype=np.float64)
    # volatility clustering over ~a day
    env = 0.6 + 0.9 * np.abs(np.sin(2 * np.pi * minute_index / 1440.0 + (_seed(symbol) % 100)))
    shock = rng.standard_normal(_BLOCK) * _MINUTE_VOL * env
    jump = np.where(rng.random(_BLOCK) < 0.00025, rng.normal(0.0, _MINUTE_VOL * 8.0, _BLOCK), 0.0)
    return shock + jump


@lru_cache(maxsize=256)
def _block_prefix(symbol: str, up_to_block: int) -> np.ndarray:
    """Cumulative log-return at the start of each block, 0..up_to_block."""
    prefix = np.zeros(up_to_block + 2)
    running = 0.0
    for b in range(up_to_block + 1):
        prefix[b] = running
        running += float(_minute_block(symbol, b).sum())
    prefix[up_to_block + 1] = running
    return prefix


def _cumulative_log_return(symbol: str, minutes: np.ndarray) -> np.ndarray:
    """Cumulative log-return at each absolute minute, from the epoch."""
    minutes = np.asarray(minutes, dtype=np.int64)
    blocks = minutes // _BLOCK
    offsets = minutes % _BLOCK
    prefix = _block_prefix(symbol, int(blocks.max()) if blocks.size else 0)
    out = prefix[blocks].astype(np.float64)
    for b in np.unique(blocks):
        mask = blocks == b
        block_returns = _minute_block(symbol, int(b))
        out[mask] += np.concatenate([[0.0], np.cumsum(block_returns)])[offsets[mask]]
    return out


def _cycles(minutes: np.ndarray) -> np.ndarray:
    """Very slow swings (weeks / months) layered on the walk.

    Deliberately no intraday components: a short cycle would distort the
    higher timeframes, because their candle-to-candle moves would inherit the
    cycle's slope instead of the walk's volatility.
    """
    phase = _seed(_PHASE_KEY) % 360
    m = minutes.astype(np.float64)
    return (
        0.12 * np.sin(2 * np.pi * m / 10080.0 + phase)            # ~weekly
        + 0.06 * np.sin(2 * np.pi * m / 43200.0 + phase * 1.7)    # ~monthly
    )


_PHASE_KEY = "phase"


# The walk is generated from a fixed epoch. A wide soft bound keeps the demo
# price in a believable neighbourhood of the anchor without flattening
# volatility: local returns survive because the bound is ~3 sigma away and tanh
# is close to linear there, unlike a tight clamp.
_WALK_BOUND = 2.0
_WALK_GAIN = 0.55


def price_at(symbol: str, timestamps_ms: np.ndarray) -> np.ndarray:
    """Close price at the given bar-close timestamps (same for every timeframe)."""
    # Minutes since the demo epoch (NOT since 1970 - that would accumulate
    # decades of random walk and saturate the bound).
    minutes = np.asarray(timestamps_ms, dtype=np.int64) // 60_000 - _EPOCH // 60
    minutes = np.clip(minutes, 0, None)
    walk = _cumulative_log_return(symbol, minutes) * _WALK_GAIN
    bounded = _WALK_BOUND * np.tanh(walk / _WALK_BOUND)
    log_price = math.log(_anchor_price(symbol)) + bounded + _cycles(minutes)
    return np.exp(log_price)


def _bar_noise(symbol: str, timeframe: str, start_bar: int, length: int) -> np.ndarray:
    """Per-candle shape randomness, stable per absolute bar index."""
    first, last = start_bar // _BLOCK, (start_bar + length - 1) // _BLOCK
    chunks = []
    for b in range(first, last + 1):
        rng = np.random.default_rng(_seed(symbol, timeframe, "shape", b))
        chunks.append(
            np.stack(
                [
                    rng.normal(0.0, 1.0, _BLOCK),  # open gap
                    rng.random(_BLOCK),            # upper wick
                    rng.random(_BLOCK),            # lower wick
                    rng.random(_BLOCK),            # volume
                ]
            )
        )
    joined = np.concatenate(chunks, axis=-1)
    offset = start_bar - first * _BLOCK
    return joined[:, offset: offset + length]


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
    start_ts = end_ts - (limit - 1) * step

    times_s = np.arange(start_ts, end_ts + step, step, dtype=np.int64)
    closes = price_at(symbol, times_s * 1000)
    prev_close = price_at(symbol, (times_s - step) * 1000)
    closes, prev_close = closes[1:], prev_close[1:]
    bar_times = times_s[1:]
    n = len(closes)

    start_bar = int((start_ts - _EPOCH) // step)
    open_noise, wick_a, wick_b, vol_noise = _bar_noise(symbol, timeframe, start_bar, n)

    open_px = prev_close * (1 + open_noise * 0.0003)
    body_hi = np.maximum(open_px, closes)
    body_lo = np.minimum(open_px, closes)
    wick = (0.4 + wick_a) * closes * sigma * 0.55
    high = body_hi + wick * (0.2 + wick_b)
    low = np.maximum(body_lo - wick * (0.2 + wick_a), 1e-9)

    # Volume must behave like a real tape: it expands on impulsive candles and
    # dries up on quiet ones. Without this the demo feed can never show an HVB,
    # because a volume breakout is the whole point of the pattern.
    bar_move = np.abs(np.diff(np.log(closes), prepend=np.log(closes[0])))
    normal_move = np.maximum(np.median(bar_move), 1e-9)
    impulse = bar_move / normal_move
    base_vol = 1_500_000 if closes[-1] > 1 else 40_000_000
    volume = base_vol * (0.55 + 0.5 * vol_noise) * (0.55 + 0.65 * impulse)

    return pd.DataFrame(
        {
            "time": (bar_times * 1000).astype(np.int64),
            "open": open_px,
            "high": high,
            "low": low,
            "close": closes,
            "volume": volume,
        }
    )
