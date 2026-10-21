"""TDIGM (Traders Dynamic Index) and the two detections this bot needs.

The indicator is the trader's own setting - **TDIGM (50, 50, 1, 9)**:

    r     = RSI(close, 50)          # rsi_period
    fast  = SMA(r, 1)               # the purple line (fast = 1, so it is r itself)
    slow  = SMA(r, 9)               # the signal line
    mbl   = SMA(r, 50)              # market base line = the centre band
    band  = 1.6185 * stdev(r, 50)   # volatility band around the base line

    HVB (bullish) = fast crosses ABOVE the base line
    HVB (bearish) = fast crosses BELOW the base line

Everything is plain Python (no numpy/pandas) so the bot has almost no
dependencies and the numbers are the same on any machine.
"""

from __future__ import annotations

from dataclasses import dataclass


def sma(values: list[float], period: int) -> list[float | None]:
    """Simple moving average; ``None`` until the window is full."""
    period = max(1, int(period))
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= period:
            running -= values[i - period]
        if i + 1 >= period:
            out[i] = running / period
    return out


def stdev(values: list[float], period: int) -> list[float | None]:
    """Population standard deviation over a rolling window (same as Pine's stdev)."""
    period = max(1, int(period))
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i + 1 < period:
            continue
        window = values[i + 1 - period : i + 1]
        mean = sum(window) / period
        variance = sum((v - mean) ** 2 for v in window) / period
        out[i] = variance ** 0.5
    return out


def rsi(closes: list[float], period: int = 50) -> list[float | None]:
    """Wilder's RSI. The first value appears at index ``period``."""
    period = max(1, int(period))
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    return out


@dataclass
class TdiPoint:
    fast: float | None
    slow: float | None
    mbl: float | None
    upper: float | None
    lower: float | None


def tdi(
    closes: list[float],
    rsi_period: int = 50,
    band_period: int = 50,
    fast_period: int = 1,
    slow_period: int = 9,
    band_mult: float = 1.6185,
) -> list[TdiPoint]:
    """The full TDIGM series. Index i belongs to candle i."""
    r = rsi(closes, rsi_period)
    # SMA over the RSI, ignoring the warm-up Nones.
    first = next((i for i, v in enumerate(r) if v is not None), None)
    if first is None:
        return [TdiPoint(None, None, None, None, None) for _ in closes]

    compact = [v for v in r[first:] if v is not None]
    fast_c = sma(compact, fast_period)
    slow_c = sma(compact, slow_period)
    mbl_c = sma(compact, band_period)
    std_c = stdev(compact, band_period)

    points: list[TdiPoint] = [TdiPoint(None, None, None, None, None) for _ in closes]
    for j in range(len(compact)):
        i = first + j
        mbl = mbl_c[j]
        sd = std_c[j]
        points[i] = TdiPoint(
            fast=fast_c[j],
            slow=slow_c[j],
            mbl=mbl,
            upper=(mbl + band_mult * sd) if mbl is not None and sd is not None else None,
            lower=(mbl - band_mult * sd) if mbl is not None and sd is not None else None,
        )
    return points


def crossed_above(prev_a, prev_b, a, b) -> bool:
    if None in (prev_a, prev_b, a, b):
        return False
    return prev_a <= prev_b and a > b


def crossed_below(prev_a, prev_b, a, b) -> bool:
    if None in (prev_a, prev_b, a, b):
        return False
    return prev_a >= prev_b and a < b


CENTRE_REFERENCES = {"mbl": "middle of the volatility bands", "fifty": "50 midline"}


def centre_of(point: TdiPoint, reference: str = "mbl") -> float | None:
    """The centre band the price line has to cross.

    ``mbl``   = the middle of the volatility bands (the TDIGM ``mid``), i.e.
                ``(upper + lower) / 2`` = ``SMA(RSI, bandLength)``.
    ``fifty`` = the TDI's central axis, the 50 level.
    """
    if reference == "fifty":
        return 50.0 if point.fast is not None else None
    return point.mbl


def line_speed(points: list[TdiPoint], index: int, window: int = 50) -> float | None:
    """How fast the price line moved into this candle, in its own units.

    ``|fast - fast_prev| / mean(|fast - fast_prev|)`` over the last ``window``
    candles. 1.0 = an average step, 3.0 = three times its normal stride. This is
    what "tezi" (speed) has to mean in code: an absolute number of RSI points
    cannot, because the line is much twitchier on 1m than on 1h.
    """
    if index < 1:
        return None
    window = max(2, int(window))
    start = max(1, index - window + 1)
    steps = []
    for i in range(start, index + 1):
        a, b = points[i - 1].fast, points[i].fast
        if a is None or b is None:
            continue
        steps.append(abs(b - a))
    if not steps:
        return None
    typical = sum(steps) / len(steps)
    now = abs(points[index].fast - points[index - 1].fast)
    if typical <= 0:
        return 0.0 if now == 0 else None
    return now / typical


def hvb_signal(
    points: list[TdiPoint],
    index: int,
    min_points: float = 0.0,
    reference: str = "mbl",
    min_speed: float = 0.0,
    speed_window: int = 50,
) -> str | None:
    """``"up"`` / ``"down"`` when the price line breaks the CENTRE band here.

    Three gates, all needed by the trader's definition:

    1. **the centre band only.** ``centre_of`` is ``(upper + lower) / 2`` (the
       yellow market base line) or the 50 axis. The upper/lower bands take no
       part in the rule - touching them is not an HVB, and the line does not
       have to reach them either.
    2. **crossed on this candle**: previous candle on the other side, this
       candle strictly beyond.
    3. **with speed.** ``min_points`` = how far beyond the centre the line
       closed, ``min_speed`` = how big this candle's step was compared with the
       line's average step. A drift back through the band is not an HVB.
    """
    if index < 1 or index >= len(points):
        return None
    prev, now = points[index - 1], points[index]
    prev_centre = centre_of(prev, reference)
    centre = centre_of(now, reference)
    if prev.fast is None or now.fast is None or prev_centre is None or centre is None:
        return None

    speed = line_speed(points, index, speed_window) if min_speed > 0 else None
    if min_speed > 0 and (speed is None or speed < min_speed):
        return None

    if crossed_above(prev.fast, prev_centre, now.fast, centre) and (now.fast - centre) >= min_points:
        return "up"
    if crossed_below(prev.fast, prev_centre, now.fast, centre) and (centre - now.fast) >= min_points:
        return "down"
    return None


def break_signal(candles: list[dict], index: int, lookback: int = 20) -> str | None:
    """``"high"`` when this candle closes above the prior ``lookback`` highs.

    Uses the close so a wick alone does not count, and the previous bars only
    (index ``lookback`` back through ``index - 1``), so nothing looks ahead.
    """
    if index < lookback or index >= len(candles):
        return None
    window = candles[index - lookback : index]
    if len(window) < lookback:
        return None
    close = candles[index]["close"]
    if close > max(c["high"] for c in window):
        return "high"
    if close < min(c["low"] for c in window):
        return "low"
    return None


class TdiTracker:
    """Incremental TDIGM - O(1) per candle instead of recomputing the series.

    The batch functions above are the reference implementation (and are used in
    the tests to prove this class produces exactly the same numbers). A live bot
    sees a candle every few seconds, so the work per candle has to be tiny.
    """

    def __init__(
        self,
        rsi_period: int = 50,
        band_period: int = 50,
        fast_period: int = 1,
        slow_period: int = 9,
        band_mult: float = 1.6185,
        maxlen: int = 400,
    ) -> None:
        self.rsi_period = max(1, int(rsi_period))
        self.band_period = max(1, int(band_period))
        self.fast_period = max(1, int(fast_period))
        self.slow_period = max(1, int(slow_period))
        self.band_mult = float(band_mult)
        self.maxlen = int(maxlen)

        self.closes: list[float] = []
        self.r_values: list[float] = []
        self.points: list[TdiPoint] = []

        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._rsi_seed: list[float] = []

        # rolling sums over the r series
        self._sum_fast = 0.0
        self._sum_slow = 0.0
        self._sum_mbl = 0.0
        self._sum_sq_mbl = 0.0

    # ------------------------------------------------------------- helpers
    def _rsi_step(self, close: float) -> float | None:
        if not self.closes:
            self.closes.append(close)
            return None
        change = close - self.closes[-1]
        self.closes.append(close)
        if len(self.closes) <= self.maxlen + 1 and len(self.closes) > self.maxlen:
            self.closes.pop(0)

        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0

        if self._avg_gain is None:
            self._rsi_seed.append(change)
            if len(self._rsi_seed) < self.rsi_period:
                return None
            gains = sum(1 for x in self._rsi_seed if x > 0)
            gains = sum(x for x in self._rsi_seed if x > 0) / self.rsi_period
            losses = sum(-x for x in self._rsi_seed if x < 0) / self.rsi_period
            self._avg_gain, self._avg_loss = gains, losses
        else:
            p = self.rsi_period
            self._avg_gain = (self._avg_gain * (p - 1) + gain) / p
            self._avg_loss = (self._avg_loss * (p - 1) + loss) / p

        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def _rolling(self) -> TdiPoint | None:
        r = self.r_values
        if len(r) < max(self.fast_period, self.slow_period, self.band_period):
            return None
        fast = sum(r[-self.fast_period :]) / self.fast_period
        slow = sum(r[-self.slow_period :]) / self.slow_period
        window = r[-self.band_period :]
        mbl = sum(window) / self.band_period
        variance = max(0.0, sum(v * v for v in window) / self.band_period - mbl * mbl)
        sd = variance ** 0.5
        return TdiPoint(
            fast=fast,
            slow=slow,
            mbl=mbl,
            upper=mbl + self.band_mult * sd,
            lower=mbl - self.band_mult * sd,
        )

    # ---------------------------------------------------------------- api
    def add(self, close: float) -> TdiPoint | None:
        value = self._rsi_step(close)
        if value is None:
            return None
        self.r_values.append(value)
        if len(self.r_values) > self.maxlen:
            self.r_values.pop(0)
        point = self._rolling()
        if point is not None:
            self.points.append(point)
            if len(self.points) > self.maxlen:
                self.points.pop(0)
        return point

    @property
    def history(self) -> list[TdiPoint]:
        """The points kept in memory, oldest first (for the UI sparkline)."""
        return self.points

    @property
    def last(self) -> TdiPoint | None:
        return self.points[-1] if self.points else None

    @property
    def prev(self) -> TdiPoint | None:
        return self.points[-2] if len(self.points) >= 2 else None
