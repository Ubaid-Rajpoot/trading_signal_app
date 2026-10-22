"""The book's confluences, computed for one setup.

Everything here comes from *Advanced ICT Institutional SMC Trading* (David
Woods), the PDF in ``docs/reference/book``. Only the parts that can be defined
exactly are coded; anything that needs a human eye is left out on purpose and
listed in ``docs/book-coverage.md``.

    p.42  STRONG HIGH / WEAK LOW
          "Strong High is the High that Caused Manipulation and Structure
          Breaking ... Weak Low is the Low that fail to break structure. For
          every strong LOW there is a weak HIGH."
          -> the level that was swept and then gave way = STRONG.
             the level that the reversal broke                     = WEAK.

          PREMIUM / DISCOUNT
          "ABOVE THE BSL = PREMIUM PRICE, BELOW THE SSL = DISCOUNT PRICE"
          -> the dealing range is the last major swing low .. last major swing
             high; above its middle is premium (sell zone), below is discount.

    p.49  FBMS / p.53-54  SHIFT and FAILURE SWING
          "price fail to create a new LL / a new higher high"
          -> a SHIFT is a break of a *minor* swing, MS is a break of a *major*
             one, and a failure swing is a swing that fails to extend.

    p.87  "CONFLUENCE: full fill FVG > mitigate LTF algorithm candle >
          reverse shift in market structure (-MS)"
          -> the displacement leg has to leave an imbalance (FVG); an entry
             with no imbalance behind it is just a poke.

    p.92  HIGH VOLUME BREAKOUT (see ``bot/indicators.py``: the price line
          crossing the centre band with speed).

Nothing in this file decides a trade on its own - it describes the setup, and
``bot/filters.py`` turns each description into an optional gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FairValueGap:
    """A three-candle imbalance: p.87's FVG."""

    side: str  # "bullish" (a gap below price) or "bearish" (a gap above price)
    top: float
    bottom: float
    index: int
    time: int

    @property
    def size(self) -> float:
        return abs(self.top - self.bottom)

    def filled_fraction(self, candles: list[dict], upto: int) -> float:
        """How much of the gap price has traded back through (0 = untouched)."""
        if self.size <= 0:
            return 0.0
        deepest = 0.0
        for bar in candles[self.index + 1 : upto + 1]:
            if self.side == "bullish":
                # price coming down into the gap: measured from its top
                deepest = max(deepest, self.top - float(bar["low"]))
            else:
                deepest = max(deepest, float(bar["high"]) - self.bottom)
        return max(0.0, min(1.0, deepest / self.size))


def fair_value_gaps(candles: list[dict], start: int = 0, end: int | None = None) -> list[FairValueGap]:
    """Every FVG printed between ``start`` and ``end`` (inclusive)."""
    end = len(candles) - 1 if end is None else min(end, len(candles) - 1)
    out: list[FairValueGap] = []
    for i in range(max(2, start), end + 1):
        a, c = candles[i - 2], candles[i]
        if float(c["low"]) > float(a["high"]):  # bullish imbalance
            out.append(FairValueGap("bullish", float(c["low"]), float(a["high"]), i, int(c["close_time"])))
        elif float(c["high"]) < float(a["low"]):  # bearish imbalance
            out.append(FairValueGap("bearish", float(a["low"]), float(c["high"]), i, int(c["close_time"])))
    return out


def liquidity_pools(
    candles: list[dict],
    lookback: int = 30,
    tolerance: float = 0.25,
    atr: float | None = None,
) -> dict[str, list[tuple[float, int]]]:
    """p.42 / p.53: clusters of equal highs and equal lows - resting liquidity.

    Two extremes are "equal" when they sit within ``tolerance`` x ATR of each
    other. Returns every cluster with at least two touches, as
    ``{"highs": [(price, touches), ...], "lows": [...]}``.
    """
    window = candles[-lookback:]
    if not window or not atr or atr <= 0:
        return {"highs": [], "lows": []}
    band = tolerance * atr

    def clusters(values: list[float]) -> list[tuple[float, int]]:
        found: list[tuple[float, int]] = []
        used = [False] * len(values)
        for i, anchor in enumerate(values):
            if used[i]:
                continue
            members = [j for j, value in enumerate(values) if not used[j] and abs(value - anchor) <= band]
            if len(members) >= 2:
                for j in members:
                    used[j] = True
                found.append((sum(values[j] for j in members) / len(members), len(members)))
        return sorted(found, key=lambda item: -item[1])

    return {
        "highs": clusters([float(c["high"]) for c in window]),
        "lows": clusters([float(c["low"]) for c in window]),
    }


def pools_ahead(
    pools: dict[str, list[tuple[float, int]]], side: str, entry: float
) -> int:
    """How many equal extremes sit in front of the trade (biggest cluster).

    A short aims down, so a pile of equal **lows** below the entry is a magnet
    and a wall at the same time - that is the "aimed into a pool" case the
    trader's own spec rejects. A long is the mirror image.
    """
    if side == "SHORT":
        ahead = [count for price, count in pools.get("lows", []) if price < entry]
    else:
        ahead = [count for price, count in pools.get("highs", []) if price > entry]
    return max(ahead) if ahead else 0


def equal_extremes(
    candles: list[dict],
    lookback: int = 30,
    tolerance: float = 0.25,
    atr: float | None = None,
) -> dict[str, int]:
    """Largest cluster of equal highs / equal lows (the display number)."""
    pools = liquidity_pools(candles, lookback, tolerance, atr)
    return {
        "highs": max((count for _, count in pools["highs"]), default=0),
        "lows": max((count for _, count in pools["lows"]), default=0),
    }


def failing_swings(candles: list[dict], pivot_len: int = 5) -> dict[str, Any] | None:
    """p.53-54 failure swing: the last swing failed to extend the one before it.

    ``bullish`` = a lower low was *not* made (price failed to make a new LL)
    while the swing before it was a low - the sell side ran out of sellers.
    ``bearish`` = the mirror image at a high.
    """
    n = max(1, pivot_len)
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(n, len(candles) - n):
        window = candles[i - n : i + n + 1]
        if candles[i]["high"] >= max(float(c["high"]) for c in window):
            highs.append((i, float(candles[i]["high"])))
        if candles[i]["low"] <= min(float(c["low"]) for c in window):
            lows.append((i, float(candles[i]["low"])))
    if len(lows) >= 2 and lows[-1][1] > lows[-2][1]:
        return {"kind": "bullish", "level": lows[-1][1], "previous": lows[-2][1], "index": lows[-1][0]}
    if len(highs) >= 2 and highs[-1][1] < highs[-2][1]:
        return {"kind": "bearish", "level": highs[-1][1], "previous": highs[-2][1], "index": highs[-1][0]}
    return None


def dealing_range(high: float | None, low: float | None) -> dict[str, float] | None:
    """p.42: the range whose middle separates premium from discount."""
    if high is None or low is None or high <= low:
        return None
    return {"high": float(high), "low": float(low), "equilibrium": (float(high) + float(low)) / 2}


def position_in_range(price: float, bounds: dict[str, float] | None) -> float | None:
    """0.0 at the range low, 1.0 at the range high."""
    if not bounds:
        return None
    span = bounds["high"] - bounds["low"]
    if span <= 0:
        return None
    return max(0.0, min(1.0, (price - bounds["low"]) / span))


def zone_of(position: float | None, equilibrium_band: float = 0.05) -> str | None:
    """``premium`` above the middle, ``discount`` below it, ``equilibrium`` at it."""
    if position is None:
        return None
    if position > 0.5 + equilibrium_band:
        return "premium"
    if position < 0.5 - equilibrium_band:
        return "discount"
    return "equilibrium"


@dataclass
class Confluence:
    """Everything the book says about one setup, in one object."""

    side: str
    break_kind: str | None = None          # "SHIFT" (minor break) or "MS" (major break)
    external_trend: int | None = None      # direction of the last major break
    swept_label: str | None = None         # "strong high" / "strong low"  (p.42)
    broke_label: str | None = None         # "weak low" / "weak high"      (p.42)
    range_low: float | None = None
    range_high: float | None = None
    range_high_time: int | None = None
    range_low_time: int | None = None
    position: float | None = None          # 0..1 inside the dealing range (entry)
    zone: str | None = None                # premium / discount / equilibrium (entry)
    sweep_position: float | None = None    # where the MANIPULATION happened
    sweep_zone: str | None = None          # p.42: sell in premium, buy in discount
    fvgs_in_leg: int = 0                   # imbalances the displacement printed
    fvg_filled: float = 0.0                # how much of the best one is filled
    equal_highs: int = 0
    equal_lows: int = 0
    pools_ahead: int = 0
    structure_atr: float | None = None     # height of the swept->broke structure
    failure_swing: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "break_kind": self.break_kind,
            "external_trend": self.external_trend,
            "swept_label": self.swept_label,
            "broke_label": self.broke_label,
            "range_low": self.range_low,
            "range_high": self.range_high,
            "range_high_time": self.range_high_time,
            "range_low_time": self.range_low_time,
            "position": None if self.position is None else round(self.position, 3),
            "zone": self.zone,
            "sweep_position": None if self.sweep_position is None else round(self.sweep_position, 3),
            "sweep_zone": self.sweep_zone,
            "fvgs_in_leg": self.fvgs_in_leg,
            "fvg_filled": round(self.fvg_filled, 2),
            "equal_highs": self.equal_highs,
            "equal_lows": self.equal_lows,
            "pools_ahead": self.pools_ahead,
            "structure_atr": None if self.structure_atr is None else round(self.structure_atr, 2),
            "failure_swing": self.failure_swing,
            "notes": list(self.notes),
        }
