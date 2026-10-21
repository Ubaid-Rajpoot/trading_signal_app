"""Is this market trending or just chopping sideways?

The trader's second rule: **a setup only counts in a trending market.** A
range prints the same sweep-and-break pattern as a trend - that is exactly why
it is dangerous - so the bot has to be able to tell the two apart *before* it
takes the pattern.

Two independent measures are used, because either one alone can be fooled:

``ADX(14)``      Wilder's directional index. Strength of directional movement,
                 blind to which way. < 20-25 is the textbook "no trend".
``ER(50)``       Kaufman's efficiency ratio:

                     |close[now] - close[now-n]|
                     --------------------------        n = 50
                       sum |close[i] - close[i-1]|

                 How much of the distance travelled actually went anywhere.
                 A clean trend is ~0.4-0.8, a range ~0.1-0.3. Price can thrash
                 100 points and end where it started: ADX may blink, ER cannot.

A candle is ``trend`` only when **both** are above their thresholds. The
direction (for "only trade with the trend") comes from EMA(20) vs EMA(50).

Both functions here are batches (the reference used by the tests and by
``tools/scan_history.py``); ``RegimeTracker`` is the same maths maintained
incrementally for the live bot, and a test proves the two agree bar for bar.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_REGIME: dict[str, float] = {
    "adx_period": 14,
    "adx_min": 22.0,
    "er_period": 50,
    "er_min": 0.30,
    "ema_fast": 20,
    "ema_slow": 50,
}


@dataclass
class RegimePoint:
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    er: float | None
    ema_fast: float | None
    ema_slow: float | None
    atr: float | None

    # ------------------------------------------------------------- direction
    @property
    def direction(self) -> int | None:
        """+1 up, -1 down, None while the EMAs are still warming up."""
        if self.ema_fast is None or self.ema_slow is None:
            return None
        if self.ema_fast > self.ema_slow:
            return 1
        if self.ema_fast < self.ema_slow:
            return -1
        return 0

    def state(self, adx_min: float, er_min: float) -> str | None:
        """``"trend"``, ``"range"`` or ``None`` while the measures warm up."""
        if self.adx is None or self.er is None:
            return None
        return "trend" if (self.adx >= adx_min and self.er >= er_min) else "range"


# --------------------------------------------------------------------- batch
def ema(values: list[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first ``period`` values (Pine's ta.ema)."""
    period = max(1, int(period))
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1.0)
    for i in range(period, len(values)):
        out[i] = out[i - 1] + k * (values[i] - out[i - 1])  # type: ignore[operator]
    return out


def true_range(candles: list[dict]) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    for i in range(1, len(candles)):
        high, low = float(candles[i]["high"]), float(candles[i]["low"])
        prev_close = float(candles[i - 1]["close"])
        out[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))
    return out


def _wilder(values: list[float | None], period: int) -> list[float | None]:
    """Wilder's smoothing: sum of the first ``period`` values, then a running
    ``prev - prev/period + value``. ``None`` before the window is full."""
    period = max(1, int(period))
    out: list[float | None] = [None] * len(values)
    running = 0.0
    count = 0
    for i, value in enumerate(values):
        if value is None:
            continue
        if count < period:
            running += value
            count += 1
            if count == period:
                out[i] = running
            continue
        running = running - running / period + value
        out[i] = running
    return out


def atr(candles: list[dict], period: int = 14) -> list[float | None]:
    """Wilder's ATR (the smoothed true range, not its average)."""
    smoothed = _wilder(true_range(candles), period)
    return [None if v is None else v / period for v in smoothed]


def adx(candles: list[dict], period: int = 14) -> list[RegimePoint]:
    """Wilder's ADX with +DI / -DI, in the classic formulation."""
    period = max(1, int(period))
    n = len(candles)
    plus_dm: list[float | None] = [None] * n
    minus_dm: list[float | None] = [None] * n
    for i in range(1, n):
        up = float(candles[i]["high"]) - float(candles[i - 1]["high"])
        down = float(candles[i - 1]["low"]) - float(candles[i]["low"])
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    tr_s = _wilder(true_range(candles), period)
    plus_s = _wilder(plus_dm, period)
    minus_s = _wilder(minus_dm, period)

    points: list[RegimePoint] = [RegimePoint(None, None, None, None, None, None, None) for _ in range(n)]
    dx_values: list[float | None] = [None] * n
    for i in range(n):
        tr, up, down = tr_s[i], plus_s[i], minus_s[i]
        if tr is None or up is None or down is None or tr == 0:
            continue
        plus_di = 100.0 * up / tr
        minus_di = 100.0 * down / tr
        total = plus_di + minus_di
        dx_values[i] = 0.0 if total == 0 else 100.0 * abs(plus_di - minus_di) / total
        points[i].plus_di = plus_di
        points[i].minus_di = minus_di

    # first ADX = average of the first `period` DX values, then Wilder-smoothed
    seen: list[float] = []
    current: float | None = None
    for i in range(n):
        dx = dx_values[i]
        if dx is None:
            continue
        if current is None:
            seen.append(dx)
            if len(seen) == period:
                current = sum(seen) / period
                points[i].adx = current
            continue
        current = (current * (period - 1) + dx) / period
        points[i].adx = current
    return points


def efficiency_ratio(closes: list[float], period: int = 50) -> list[float | None]:
    period = max(1, int(period))
    out: list[float | None] = [None] * len(closes)
    for i in range(period, len(closes)):
        travelled = 0.0
        for j in range(i - period + 1, i + 1):
            travelled += abs(closes[j] - closes[j - 1])
        if travelled == 0:
            out[i] = 0.0
        else:
            out[i] = abs(closes[i] - closes[i - period]) / travelled
    return out


def regime_series(candles: list[dict], **settings) -> list[RegimePoint]:
    """Every candle's regime reading - the reference implementation."""
    cfg = {**DEFAULT_REGIME, **(settings or {})}
    closes = [float(c["close"]) for c in candles]
    directional = adx(candles, int(cfg["adx_period"]))
    er = efficiency_ratio(closes, int(cfg["er_period"]))
    fast = ema(closes, int(cfg["ema_fast"]))
    slow = ema(closes, int(cfg["ema_slow"]))
    tr = atr(candles, int(cfg["adx_period"]))
    for i, point in enumerate(directional):
        point.er = er[i]
        point.ema_fast = fast[i]
        point.ema_slow = slow[i]
        point.atr = tr[i]
    return directional


# ------------------------------------------------------------------ live one
class RegimeTracker:
    """The same numbers, O(1) per candle.

    Wilder's smoothing is recursive, so the tracker only has to keep the last
    smoothed totals plus the handful of values the first ADX needs. Every value
    it produces is compared with :func:`regime_series` in the tests.
    """

    def __init__(
        self,
        adx_period: int = 14,
        er_period: int = 50,
        ema_fast: int = 20,
        ema_slow: int = 50,
    ) -> None:
        self.adx_period = max(1, int(adx_period))
        self.er_period = max(1, int(er_period))
        self.ema_fast_period = max(1, int(ema_fast))
        self.ema_slow_period = max(1, int(ema_slow))

        self.last_bar: dict | None = None
        self.point = RegimePoint(None, None, None, None, None, None, None)

        # Wilder accumulators (None until the first `period` values are in)
        self._tr_count = 0
        self._tr_sum = 0.0
        self._tr_smooth: float | None = None
        self._plus_count = 0
        self._plus_sum = 0.0
        self._plus_smooth: float | None = None
        self._minus_count = 0
        self._minus_sum = 0.0
        self._minus_smooth: float | None = None

        self._dx_seen: list[float] = []
        self._adx: float | None = None

        self._closes: list[float] = []
        self._ema_fast: float | None = None
        self._ema_slow: float | None = None
        self._ema_fast_seed: list[float] = []
        self._ema_slow_seed: list[float] = []

    # ------------------------------------------------------------- internals
    def _step_ema(self, close: float, seed: list[float], current: float | None, period: int) -> float | None:
        if current is None:
            seed.append(close)
            if len(seed) < period:
                return None
            return sum(seed) / period
        return current + (2.0 / (period + 1.0)) * (close - current)

    def _step_wilder(self, value: float, count_attr: str, sum_attr: str, smooth_attr: str) -> float | None:
        period = self.adx_period
        count = getattr(self, count_attr)
        if getattr(self, smooth_attr) is None:
            total = getattr(self, sum_attr) + value
            setattr(self, sum_attr, total)
            setattr(self, count_attr, count + 1)
            if count + 1 == period:
                setattr(self, smooth_attr, total)
                return total
            return None
        smooth = getattr(self, smooth_attr) - getattr(self, smooth_attr) / period + value
        setattr(self, smooth_attr, smooth)
        return smooth

    # ------------------------------------------------------------------ api
    def add(self, bar: dict) -> RegimePoint:
        close = float(bar["close"])
        self._closes.append(close)
        if len(self._closes) > self.er_period + 1:
            self._closes.pop(0)

        self._ema_fast = self._step_ema(close, self._ema_fast_seed, self._ema_fast, self.ema_fast_period)
        self._ema_slow = self._step_ema(close, self._ema_slow_seed, self._ema_slow, self.ema_slow_period)

        if self.last_bar is not None:
            high, low = float(bar["high"]), float(bar["low"])
            prev_close = float(self.last_bar["close"])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            up = high - float(self.last_bar["high"])
            down = float(self.last_bar["low"]) - low
            plus = up if (up > down and up > 0) else 0.0
            minus = down if (down > up and down > 0) else 0.0

            tr_s = self._step_wilder(tr, "_tr_count", "_tr_sum", "_tr_smooth")
            plus_s = self._step_wilder(plus, "_plus_count", "_plus_sum", "_plus_smooth")
            minus_s = self._step_wilder(minus, "_minus_count", "_minus_sum", "_minus_smooth")

            if tr_s is not None and plus_s is not None and minus_s is not None and tr_s != 0:
                plus_di = 100.0 * plus_s / tr_s
                minus_di = 100.0 * minus_s / tr_s
                total = plus_di + minus_di
                dx = 0.0 if total == 0 else 100.0 * abs(plus_di - minus_di) / total
                if self._adx is None:
                    self._dx_seen.append(dx)
                    if len(self._dx_seen) == self.adx_period:
                        self._adx = sum(self._dx_seen) / self.adx_period
                else:
                    self._adx = (self._adx * (self.adx_period - 1) + dx) / self.adx_period
                self.point.plus_di = plus_di
                self.point.minus_di = minus_di
                self.point.atr = tr_s / self.adx_period
            else:
                self.point.plus_di = self.point.minus_di = None
            self.point.adx = self._adx

        self.last_bar = bar
        self.point.ema_fast = self._ema_fast
        self.point.ema_slow = self._ema_slow
        self.point.er = self._er()
        return self.snapshot()

    def snapshot(self) -> RegimePoint:
        """A copy - the tracker keeps mutating its own point."""
        p = self.point
        return RegimePoint(p.adx, p.plus_di, p.minus_di, p.er, p.ema_fast, p.ema_slow, p.atr)

    def _er(self) -> float | None:
        if len(self._closes) < self.er_period + 1:
            return None
        travelled = sum(abs(self._closes[i] - self._closes[i - 1]) for i in range(1, len(self._closes)))
        if travelled == 0:
            return 0.0
        return abs(self._closes[-1] - self._closes[0]) / travelled
