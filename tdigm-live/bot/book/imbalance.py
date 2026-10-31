"""Imbalance: the footprint the algorithm leaves when it moves price.

Three ideas, all from the book:

* **Fair value gap (FVG) / liquidity void** (p.67-71): three candles where the
  first and third do not overlap. Price moved so fast that one side of the book
  was never traded. *"The market will always come back to fill the void"*, and a
  filled FVG that gets rejected is the entry (p.87).
* **High-volume imbalance (HVI)** (p.81-83): the FVG that carries obvious
  volume. The stop goes behind it (p.106: *"you can set your stop loss above the
  high volume imbalance"*), because that is where the algorithm's own risk sits.
* **Algo candle (AC)** (p.67-69): the displacement candle itself - a body far
  larger than the recent average that leaves an FVG and takes liquidity. The
  *vector candle* is the same idea when the body engulfs the previous candle.
  A very strong AC grabs liquidity, breaks structure **and** leaves an FVG.

``algo_candle`` therefore looks at three candles (``i-1, i, i+1``) and is only
known once ``i+1`` has closed. That delay is deliberate: a gap is not a gap
until the candle after it exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def body(bar: dict) -> float:
    return abs(float(bar["close"]) - float(bar["open"]))


def span(bar: dict) -> float:
    return float(bar["high"]) - float(bar["low"])


def candle_atr(candles: list[dict], period: int = 14) -> float:
    """Average true range over the last ``period`` closed candles."""
    if len(candles) < 2:
        return 0.0
    window = candles[-(period + 1):]
    total = 0.0
    count = 0
    for prev, cur in zip(window, window[1:]):
        high, low = float(cur["high"]), float(cur["low"])
        prev_close = float(prev["close"])
        total += max(high - low, abs(high - prev_close), abs(low - prev_close))
        count += 1
    return total / count if count else 0.0


def volume_ratio(candles: list[dict], index: int, period: int = 20) -> float:
    """Volume of ``candles[index]`` against the average of the ones before it."""
    if index <= 0:
        return 0.0
    start = max(0, index - period)
    window = candles[start:index]
    if not window:
        return 0.0
    average = sum(float(bar.get("volume") or 0.0) for bar in window) / len(window)
    if average <= 0:
        return 0.0
    return float(candles[index].get("volume") or 0.0) / average


@dataclass
class Imbalance:
    """A fair value gap, with the volume story attached."""

    kind: str            # "bull" or "bear"
    bottom: float
    top: float
    index: int           # index of the displacement candle
    time: int
    body_atr: float      # displacement size in ATRs
    volume_ratio: float  # volume of the displacement candle vs its 20-bar mean
    vector: bool         # did the body engulf the previous candle?

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def high_volume(self) -> bool:
        return self.volume_ratio >= 1.5

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "bottom": self.bottom,
            "top": self.top,
            "mid": self.mid,
            "time": self.time,
            "body_atr": round(self.body_atr, 2) if self.body_atr else None,
            "volume_ratio": round(self.volume_ratio, 2) if self.volume_ratio else None,
            "vector": self.vector,
        }


def fair_value_gap(candles: list[dict], index: int) -> tuple[str, float, float] | None:
    """The gap left by ``candles[index]``, confirmed by ``candles[index+1]``."""
    if index < 1 or index + 1 >= len(candles):
        return None
    before, gap_bar, after = candles[index - 1], candles[index], candles[index + 1]
    if float(after["low"]) > float(before["high"]):
        return ("bull", float(before["high"]), float(after["low"]))
    if float(after["high"]) < float(before["low"]):
        return ("bear", float(after["high"]), float(before["low"]))
    return None


def is_vector(bar: dict, prev: dict) -> bool:
    """p.71: the vector candle engulfs the previous candle's body."""
    open_, close = float(bar["open"]), float(bar["close"])
    p_open, p_close = float(prev["open"]), float(prev["close"])
    up = close > open_
    prev_up = p_close > p_open
    if up and not prev_up:
        return close >= p_open and open_ <= p_close
    if up and prev_up:
        return close > p_close and open_ <= p_open and (close - open_) > (p_close - p_open)
    if not up and prev_up:
        return close <= p_open and open_ >= p_close
    return (close < p_close and open_ >= p_open
            and (open_ - close) > (p_open - p_close))


def algo_candle(
    candles: list[dict],
    index: int,
    *,
    atr: float,
    min_body_atr: float = 1.0,
    vol_mult: float = 1.3,
    need_vector: bool = False,
) -> Imbalance | None:
    """Is ``candles[index]`` an algo candle? Only knowable from ``index + 1``.

    ``candles[index]`` is the candidate displacement bar; the FVG it leaves is
    measured against ``index - 1`` and ``index + 1``.
    """
    if index < 1 or index + 1 >= len(candles) or atr <= 0:
        return None
    gap = fair_value_gap(candles, index)
    if gap is None:
        return None
    kind, bottom, top = gap
    bar = candles[index]
    size = body(bar) / atr
    if size < min_body_atr:
        return None
    up = float(bar["close"]) > float(bar["open"])
    if (kind == "bull") != up:
        return None
    vector = is_vector(bar, candles[index - 1])
    if need_vector and not vector:
        return None
    ratio = volume_ratio(candles, index)
    if vol_mult and ratio < vol_mult:
        return None
    return Imbalance(
        kind=kind,
        bottom=bottom,
        top=top,
        index=index,
        time=int(bar.get("close_time") or bar.get("open_time") or 0),
        body_atr=size,
        volume_ratio=ratio,
        vector=vector,
    )


def filled_fraction(imbalance: Imbalance, candles: list[dict], start: int) -> float:
    """How much of the gap price has traded back into, from ``start`` onwards.

    A bullish gap sits below price; price filling it means trading down into it.
    """
    if imbalance.height <= 0:
        return 1.0
    for bar in candles[start:]:
        if imbalance.kind == "bull":
            if float(bar["low"]) <= imbalance.bottom:
                return 1.0
            if float(bar["low"]) < imbalance.top:
                return (imbalance.top - float(bar["low"])) / imbalance.height
        else:
            if float(bar["high"]) >= imbalance.top:
                return 1.0
            if float(bar["high"]) > imbalance.bottom:
                return (float(bar["high"]) - imbalance.bottom) / imbalance.height
    return 0.0


def premium_discount(price: float, range_high: float, range_low: float) -> float:
    """Where price sits inside the dealing range: 0 = discount, 1 = premium."""
    if range_high <= range_low:
        return 0.5
    position = (price - range_low) / (range_high - range_low)
    return max(0.0, min(1.0, position))
