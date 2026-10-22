"""Roll 1-minute candles up into every timeframe the bot watches.

Binance streams one 1m kline per symbol. 3m/5m/15m/30m/1h candles are built here
by aggregation (open = first, close = last, high = max, low = min, volume = sum),
bucket-aligned on the clock, so a 15m candle closes at :00/:15/:30/:45 - never a
rolling window that keeps changing its own history.
"""

from __future__ import annotations

TIMEFRAMES: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
}


def bucket_start(open_time_ms: int, timeframe: str) -> int:
    step = TIMEFRAMES[timeframe]
    return (int(open_time_ms) // step) * step


def merge(candle: dict, bar: dict) -> dict:
    """Merge a new 1m bar into an existing (possibly forming) candle."""
    if candle is None:
        return dict(bar)
    candle = dict(candle)
    candle["high"] = max(candle["high"], bar["high"])
    candle["low"] = min(candle["low"], bar["low"])
    candle["close"] = bar["close"]
    candle["volume"] = candle.get("volume", 0.0) + bar.get("volume", 0.0)
    candle["close_time"] = bar["close_time"]
    candle["trades"] = candle.get("trades", 0) + bar.get("trades", 0)
    return candle


class Rollup:
    """Turns a stream of closed 1m candles into closed candles of one timeframe."""

    def __init__(self, timeframe: str) -> None:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {timeframe!r}")
        self.timeframe = timeframe
        self.start: int | None = None
        self.candle: dict | None = None

    def add(self, bar: dict) -> list[dict]:
        """Feed a closed 1m bar; return the timeframe candles it completed."""
        if bar.get("open_time") is None:
            raise ValueError("candle needs open_time")
        start = bucket_start(bar["open_time"], self.timeframe)
        completed: list[dict] = []

        if self.start is None:
            self.start, self.candle = start, dict(bar)
            self.candle["open_time"] = start
            return completed

        if start == self.start:
            self.candle = merge(self.candle, bar)
            return completed

        # A new bucket began: the previous one is final.
        if self.candle is not None:
            completed.append(self._close(self.candle))
        self.start, self.candle = start, dict(bar)
        self.candle["open_time"] = start
        return completed

    def preview(self, bar: dict) -> dict:
        """The candle that is still forming, without touching the closed state.

        The page needs to see the current candle grow between closes; this
        merges a (possibly partial) 1m bar into the bucket that is open now.
        """
        start = bucket_start(bar["open_time"], self.timeframe)
        if self.candle is None:
            candle = dict(bar)
            candle["open_time"] = start
        else:
            candle = merge(self.candle, bar)
            candle["open_time"] = self.start
        candle["close_time"] = int(start) + TIMEFRAMES[self.timeframe] - 1
        candle["timeframe"] = self.timeframe
        candle["closed"] = False
        return candle

    def flush(self) -> list[dict]:
        """Close whatever is open (used at the end of a replay)."""
        if self.candle is None:
            return []
        done = self._close(self.candle)
        self.start, self.candle = None, None
        return [done]

    def _close(self, candle: dict) -> dict:
        candle = dict(candle)
        candle["open_time"] = int(self.start)
        candle["close_time"] = int(self.start) + TIMEFRAMES[self.timeframe] - 1
        candle["timeframe"] = self.timeframe
        candle["closed"] = True
        return candle
