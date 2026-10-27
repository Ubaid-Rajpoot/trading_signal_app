"""The market map: swings, liquidity, strong / very strong levels, algo candles,
rejection blocks - and the mitigation life-cycle of every zone.

Every rule here comes from *Advanced ICT Institutional SMC Trading* (David
Woods). The page numbers are in the docstrings so the code can always be
checked against the book, and `docs/book-notes.md` quotes each passage.

    p.42   Strong High = the High that caused Manipulation and Structure
           Breaking.  Weak Low = the Low that failed to break structure.

    p.45   VERY STRONG HIGH = HIGH GRAB LIQUIDITY (HIGH CAUSED MANIPULATION)
           BREAK STRONG HIGH + HAVE AN INDUCEMENT

    p.48   WHEN A HIGH OR A LOW DEVELOPS WEAKNESS THE STRONG H/L BECOMES WEAK
           MITIGATION WHEN THE PRICE IS MITIGATED.

    p.67   AC = ALGO CANDLE - "the candle is what develops when it absorbs
           ... and it forms FVG immediately afterwards"

    p.68   AC IS WHAT MAKES THE HIGH OR LOW STRONG

    p.69   VERY STRONG ALGO CANDLE = GRAB LIQUIDITY / BREAK STRONG HIGH OR LOW
           (MOMENTUM SHIFT) HAVE AN INDUCEMENT / FVG

    p.88   HTF REJECTION BLOCK IS A LTF ALGO CANDLE
    p.91   STRONG REJECTION BLOCK = HAVE AN INDUCEMENT + FORMS ON A SESSION
           (HIGH OR LOW)

So the words mean, exactly:

    sweep        price trades beyond a pivot high/low - the liquidity is taken
    break        a later CLOSE beyond the opposite pivot - the structure broke
    strong high  a pivot high that was swept and then broke the structure down
                 (mirror: strong low)
    inducement   a small liquidity pool (equal highs/lows) that lures the crowd
                 in before the real move
    algo candle  the last candle at the extreme, the one that absorbed the
                 orders; the displacement out of it leaves an FVG
    very strong  the same thing, but the level it grabbed was itself a strong
                 (major) level, the break is a major one, and there is an
                 inducement + an FVG - all four are recorded as `evidence`
    rejection block  an algo candle that sits on a session high/low
    mitigation   price coming back INTO the zone and filling the orders there
    invalidation price closing beyond the extreme - the strong level became
                 weak, so it is no longer supply/demand
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------- config
DEFAULTS: dict[str, Any] = {
    "pivot_len": 3,          # bars each side of a minor swing  (SHIFT territory)
    "major_len": 12,         # bars each side of a major swing  (MS / external)
    "disp_bars": 3,          # candles the displacement may take to print its FVG
    "min_fvg_atr": 0.25,     # an FVG smaller than this fraction of ATR is noise
    "lookback": 240,         # bars kept for FVG / inducement / session work
    "induce_tol": 0.25,      # equal levels are within this fraction of ATR
    "induce_count": 2,       # this many equal levels = an inducement
    "max_zone_age": 2000,    # bars a zone may stay on the map
    "mitigation_mode": "zone",  # "zone" (touch the block) or "level" (the exact extreme)
    "atr_period": 14,
    # --- quality layer (round 5) -------------------------------------------
    # These settings never decide *whether* a pattern exists - they decide which
    # order-block candle the rectangle is drawn on, and how trustworthy the
    # pattern is. Detection itself is unchanged.
    "ob_lookback": 8,           # bars searched backwards for the order-block candle
    "ob_min_height_atr": 0.12,  # a block is never thinner than this fraction of ATR
    "trend_lookback": 40,       # bars behind the efficiency ratio
    "trend_er_min": 0.25,       # Kaufman efficiency ratio: at/above this the market trends
    "trend_adx_min": 18.0,      # and the ADX has to agree
    # --- round 13: the book's own structure test, and the range gate -------------
    # "RANGES account for 80% of a price's swings" (p.58). A setup inside a range is
    # the trap the book spends its first thirty pages on, so a zone is only allowed
    # to become a *signal* when the structure is a trend - on its own timeframe AND
    # on the one above it - and when a sell sits in premium / a buy in discount
    # (p.42, p.110). Tolerances are in ATR so they scale with the instrument.
    "structure_equal_atr": 0.15,   # two highs/lows this close are "equal" = liquidity, not structure
    "structure_deep_retrace": 0.79,  # a pullback deeper than this of the last leg = no structure
    "structure_min_bars": 30,      # not enough bars for two swing highs and two lows yet
    "gate_premium_atr": 0.25,      # distance from the dealing range's middle, in ATR
    # The gate is a *rule* (the book's, and the user's), not a profit switch: on the
    # held-out 90 days it lifts the A band from 95.4 % to 97.4 % hit but cuts the
    # signal count by 91 % and the expectancy from +0.16 R to +0.14 R. It is on by
    # default; `false` shows every measured setup as a signal again.
    "structure_gate": True,
    "confluence_tol_atr": 0.5,  # "the same level" means within this fraction of ATR
    "confluence_age": 400,      # bars a strong level stays relevant for confluence
    # --- the execution plan that goes with every zone ------------------------
    "plan_stop_atr": 0.10,      # stop distance beyond the exact high/low, in ATR
    # A stop that is closer than the round-trip fee cannot be traded: the fee is
    # charged on the whole notional while the risk is only the stop distance, so
    # the cost in R is fee / risk. Measured on 90d of 1m BTC the median stop was
    # 0.11% of price against a 0.12% round-trip taker fee - i.e. 1.07 R of fees
    # per trade, which is why a 95% "win rate" still loses money. This floor is
    # the minimum stop distance as a percentage of price; 0 keeps the old plan.
    "plan_min_risk_pct": 0.0,
    "plan_pool_bars": 120,      # how far back the next liquidity pool is looked for
    "plan_pool_age": 600,       # bars a strong level stays a valid target
    # Where the stop goes. "level" is the tight one the app shipped with (a
    # tenth of an ATR beyond the exact high/low); "block" puts it beyond the far
    # side of the red box as well, which is the same rule the book states for the
    # invalidation and is what the round-8 measurement asked for: on 90d of 1m
    # BTC it lifts the hit rate from 61% to 74% and triples the number of plans
    # the fee leaves tradable. Measured in tools/plan_study.py.
    # Round 9: "block" is the shipped stop - the user's own rule ("stop beyond the
    # structure / the red box"), and it is also what makes futures pay. Measured
    # on a 36-day replay of the recorded 1m BTC futures candles, same model, same
    # fee gate, only the stop changed:
    #   level (0.10 ATR beyond the exact high/low):  4.1 signals/day, 42% hit, -0.25 R
    #   block (beyond the whole order block):       14.8 signals/day, 65% hit, -0.08 R
    # The tight stop is inside one candle's noise, so it is taken before the trade
    # can breathe. "level" is still available in the settings.
    "plan_stop_mode": "block",  # "level" | "block"
    # The exit is still the next liquidity level, but a pool half a tick away is
    # not a target - it pays a fraction of the risk and less than the fee. This
    # is the minimum distance to a pool, in ATR, that can be called "the next
    # liquidity"; pools closer than this are skipped and the next one is used.
    # Round 8: the shipped value is 1.0 ATR. Measured on 90 days of real 1m BTC
    # (every timeframe, held-out block): with the nearest pool the plan pays a
    # fifth of its risk and the fee eats the rest (net -0.01 R per trade); with
    # a pool at least 1 ATR away it pays 0.75 R and the selected signals net
    # +0.54 R at 97% accuracy. 0 restores the old "nearest pool" behaviour.
    "plan_target_atr_min": 1.0,
    # --- the checklist behind the grade (round 7) ----------------------------
    "cond_disp_atr": 2.0,       # "aggressive displacement" = this many ATR
    # The letter is the measured probability, not a count: on 90d of 1m BTC the
    # count of conditions did not rank outcomes (docs/conditions.md). Round 8
    # re-cut the bands so that every letter states an accuracy the held-out
    # block actually delivered (docs/signals.md):
    #   A+ >= 90% -> measured 97%    A >= 85% -> 97%
    #   B  >= 75% -> 95%             C >= 60% -> 90%    below C: not a signal
    "prob_cuts": {"a_plus": 0.90, "a": 0.85, "b": 0.75, "c": 0.60},
    # What counts as a signal on the page: a plan the model has measured at 85%
    # or better *and* whose fee the reward can pay for.
    "signal_prob_min": 0.85,
    # MEXC futures: 0.02% maker, 0.06% taker. The plan is limit-in, limit-out,
    # and a stop crosses the spread - so a plan costs (maker + taker) in R.
    "fee_maker_bps": 2.0,
    "fee_taker_bps": 6.0,
    "fee_max_r": 0.30,          # more than this share of the risk is fee: not tradable
    "sessions": [            # UTC, inclusive start, exclusive end
        {"name": "Asia", "start": 0, "end": 8},
        {"name": "London", "start": 7, "end": 16},
        {"name": "New York", "start": 12, "end": 21},
    ],
}

HIGH = "high"
LOW = "low"

# The kinds the page can filter on. ``side`` says which liquidity it is about:
# a high zone is supply (sell-side of the range), a low zone is demand.
KINDS = (
    "strong_high",
    "very_strong_high",
    "strong_low",
    "very_strong_low",
    "algo_bear",              # algo candle at a HIGH (bearish displacement)
    "algo_bull",              # algo candle at a LOW
    "very_strong_algo_bear",
    "very_strong_algo_bull",
    "rejection_block_bear",   # algo candle on a session HIGH
    "rejection_block_bull",   # algo candle on a session LOW
)

# ---------------------------------------------------------------- conditions
# The book states, for each pattern, a list of things that make it stronger:
# a grabbed level, a major (strong) level taken, an inducement, an algo candle,
# aggressive displacement, a real order block, a session extreme, another strong
# level at the same price - and the user's own rule that a setup must not form
# in a sideways market. Every zone is checked against **all ten** of them, so a
# card can say "6/10 conditions, these four are missing" instead of showing a
# bare letter. The names are the vocabulary used by the API, the page and the
# study tools; the grade is derived from how many are met (see grade_for).
CONDITIONS: tuple[tuple[str, str], ...] = (
    ("grab", "liquidity grabbed: the previous swing was taken"),
    ("ms", "structure broke the other way (MS)"),
    ("major", "the level it grabbed was a strong (major) level"),
    ("inducement", "inducement: equal highs/lows at the extreme"),
    ("algo", "algo candle: displacement left an FVG"),
    ("displacement", "displacement of 2 ATR or more"),
    ("block", "a real opposite-colour order block was found"),
    ("session", "the extreme is a session high/low"),
    ("confluence", "another strong level sits at the same price"),
    ("trend", "the market is trending, not ranging"),
)
CONDITION_NAMES = tuple(name for name, _ in CONDITIONS)
CONDITION_LABELS = dict(CONDITIONS)


# ---------------------------------------------------------------------- pieces
@dataclass
class Pivot:
    side: str            # HIGH or LOW
    price: float
    index: int
    time: int
    major: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"side": self.side, "price": self.price, "time": self.time, "major": self.major}


@dataclass
class FairValueGap:
    side: str            # "bearish" (above price) or "bullish" (below price)
    top: float
    bottom: float
    index: int
    time: int

    @property
    def size(self) -> float:
        return abs(self.top - self.bottom)


@dataclass
class Zone:
    """One thing the page draws and keeps drawing until it is resolved.

    ``top``/``bottom`` is the block; ``level`` is the exact high/low the trader
    is watching (the "specific high" that has to be mitigated).
    """

    kind: str
    side: str            # HIGH = supply, LOW = demand
    symbol: str
    timeframe: str
    top: float
    bottom: float
    level: float
    index: int
    time: int
    status: str = "active"        # active | mitigated | invalidated
    strength: int = 1             # how many of the book's conditions were met
    evidence: list[str] = field(default_factory=list)
    swept_level: float | None = None
    break_level: float | None = None
    fvg: dict[str, Any] | None = None
    inducement: int = 0
    session: str | None = None
    mitigated_at: int | None = None
    mitigated_index: int | None = None
    mitigated_price: float | None = None
    invalidated_at: int | None = None
    invalidated_price: float | None = None
    created_index: int = 0
    # --- round 5: what the rectangle is drawn on, and how good the pattern is
    ob: dict[str, Any] | None = None      # the opposite-colour order-block candle
    block_src: str = "pivot candle"       # "order block candle" or "pivot candle"
    trend: dict[str, Any] | None = None   # market regime when the zone formed
    confluence: list[str] = field(default_factory=list)
    htf: list[str] = field(default_factory=list)
    grade: str = "C"                      # A+ / A / B / C
    score: int = 0
    # --- round 7: the checklist behind the letter ---------------------------
    conds: dict[str, bool] = field(default_factory=dict)   # every condition, met or not
    cond_met: int = 0
    cond_total: int = len(CONDITION_NAMES)
    # --- round 7: the measured number behind the letter ---------------------
    prob: float | None = None      # P(target before stop), from bot/signal_model.json
    fee_r: float | None = None     # round-trip cost of the plan, in R
    net_r: float | None = None     # expectancy in R after that cost
    tradable: bool = False         # the fee does not swallow the edge
    # --- round 8: is this one of the signals the user trades ----------------
    signal: bool = False           # measured at signal_prob_min or better, fee clears
    measured: dict[str, Any] | None = None   # the band's out-of-sample record
    # the regime at the moment price came back into the block: this is the
    # moment a trade is actually taken, so it gets its own snapshot
    entry_trend: dict[str, Any] | None = None
    # measurable features (the study in tools/zone_quality.py uses exactly these)
    fvg_atr: float | None = None
    disp_atr: float | None = None
    block_atr: float | None = None
    swept_major: bool = False
    # --- round 13: is the market trending, and is this the right side of it -----
    gate: dict[str, Any] | None = None     # {"ok", "why", "structure", "htf", "location"}
    restored: bool = False                  # adopted from the journal after a restart
    # entry / stop / target, from the book's own exit rule (next liquidity)
    plan: dict[str, Any] | None = None
    # and what happened to it: waiting -> filled -> target | stopped
    plan_state: str = "waiting"
    plan_filled_at: int | None = None
    plan_filled_price: float | None = None
    plan_result_at: int | None = None
    plan_result_r: float | None = None

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.timeframe}|{self.kind}|{self.time}|{self.side}"

    @property
    def direction(self) -> str:
        """What the zone is for: a high is where you look for a short."""
        return "SHORT" if self.side == HIGH else "LONG"

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "side": self.side,
            "direction": self.direction,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "top": round(self.top, 8),
            "bottom": round(self.bottom, 8),
            "level": round(self.level, 8),
            "index": self.index,
            "time": self.time,
            "status": self.status,
            "strength": self.strength,
            "evidence": list(self.evidence),
            "swept_level": self.swept_level,
            "break_level": self.break_level,
            "fvg": self.fvg,
            "inducement": self.inducement,
            "session": self.session,
            "mitigated_at": self.mitigated_at,
            "mitigated_price": self.mitigated_price,
            "invalidated_at": self.invalidated_at,
            "invalidated_price": self.invalidated_price,
            # round 5 - the rectangle, the regime and the grade
            "ob": self.ob,
            "block_src": self.block_src,
            "trend": self.trend,
            "trend_state": (self.trend or {}).get("state"),
            "trend_er": (self.trend or {}).get("er"),
            "trend_adx": (self.trend or {}).get("adx"),
            "aligned": (self.trend or {}).get("aligned"),
            "confluence": list(self.confluence),
            "htf": list(self.htf),
            "grade": self.grade,
            "score": self.score,
            "conds": dict(self.conds),
            "cond_met": self.cond_met,
            "cond_total": self.cond_total,
            "cond_missing": [n for n in CONDITION_NAMES if not self.conds.get(n)],
            "prob": self.prob,
            "fee_r": self.fee_r,
            "net_r": self.net_r,
            "tradable": self.tradable,
            "signal": self.signal,
            "measured": self.measured,
            "gate": self.gate,
            "restored": self.restored,
            "entry_trend": self.entry_trend,
            "entry_state": (self.entry_trend or {}).get("state"),
            "entry_aligned": (self.entry_trend or {}).get("aligned"),
            "fvg_atr": self.fvg_atr,
            "disp_atr": self.disp_atr,
            "block_atr": self.block_atr,
            "swept_major": self.swept_major,
            "plan": self.plan,
            "plan_state": self.plan_state,
            "plan_filled_at": self.plan_filled_at,
            "plan_filled_price": self.plan_filled_price,
            "plan_result_at": self.plan_result_at,
            "plan_result_r": self.plan_result_r,
        }


def isfinite_p(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


# ------------------------------------------------------------------- helpers
def true_range_series(bars: list[dict]) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for i in range(1, len(bars)):
        high, low = float(bars[i]["high"]), float(bars[i]["low"])
        prev = float(bars[i - 1]["close"])
        out[i] = max(high - low, abs(high - prev), abs(low - prev))
    return out


def wilder_atr(bars: list[dict], period: int = 14) -> list[float | None]:
    tr = true_range_series(bars)
    out: list[float | None] = [None] * len(bars)
    period = max(1, int(period))
    seeded = 0
    total = 0.0
    running: float | None = None
    for i, value in enumerate(tr):
        if value is None:
            continue
        if running is None:
            total += value
            seeded += 1
            if seeded == period:
                running = total / period
                out[i] = running
            continue
        running = (running * (period - 1) + value) / period
        out[i] = running
    return out


def session_of(time_ms: int, sessions: list[dict]) -> str | None:
    minute = (int(time_ms) // 60_000) % 1440
    for window in sessions or []:
        start, end = int(window["start"]) * 60, int(window["end"]) * 60
        inside = (start <= minute < end) if start < end else (minute >= start or minute < end)
        if inside:
            return str(window.get("name") or "session")
    return None


def equal_levels(values: list[float], tolerance: float) -> int:
    """How many of these extremes sit inside the same ``tolerance`` band."""
    if not values or tolerance <= 0:
        return 0
    values = sorted(values, reverse=True)
    best = 1
    for i, anchor in enumerate(values):
        count = sum(1 for v in values[i:] if abs(anchor - v) <= tolerance)
        best = max(best, count)
    return best


def fair_value_gap(bar0: dict, bar1: dict, bar2: dict, index: int) -> FairValueGap | None:
    """The three-candle imbalance of p.67/p.79 (the displacement's void)."""
    a_high, a_low = float(bar0["high"]), float(bar0["low"])
    c_high, c_low = float(bar2["high"]), float(bar2["low"])
    if a_low > c_high:  # market fell but left a gap above
        return FairValueGap("bearish", top=a_low, bottom=c_high, index=index, time=int(bar2["close_time"]))
    if a_high < c_low:  # market rose and left a gap below
        return FairValueGap("bullish", top=c_low, bottom=a_high, index=index, time=int(bar2["close_time"]))
    return None


# ------------------------------------------------------- trend / range gauge
# the kinds that are themselves a strong level (used for confluence)
TREND_KINDS = ("strong_high", "very_strong_high", "strong_low", "very_strong_low")
class TrendGauge:
    """Is this market trending, and in which direction - updated one bar at a time.

    Two independent, well-known measurements, both computed from closed candles
    only, so nothing here can look into the future:

    * **Efficiency ratio** (Kaufman): net travel / total travel over N bars. In a
      trend price goes somewhere; in a range it goes everywhere and ends up
      roughly where it started, so the ratio collapses. This is the number that
      separates "wide range that keeps breaking swings in both directions" from
      a real trend.
    * **ADX(14)** (Wilder): the strength of directional movement. Below ~18 the
      market is not directional whatever the chart looks like.
    """

    def __init__(self, er_period: int = 40, adx_period: int = 14) -> None:
        self.er_period = max(2, int(er_period))
        self.adx_period = max(2, int(adx_period))
        self.closes: deque[float] = deque(maxlen=self.er_period + 1)
        self.er: float | None = None
        self.adx: float | None = None
        self.plus_di: float | None = None
        self.minus_di: float | None = None
        self._prev: tuple[float, float, float] | None = None
        self._tr: float | None = None
        self._plus_dm = 0.0
        self._minus_dm = 0.0
        self._seeded = 0
        self._dx_sum = 0.0
        self._dx_count = 0
        self.bars = 0

    def update(self, high: float, low: float, close: float) -> None:
        self.bars += 1
        self.closes.append(close)
        n = self.er_period
        if len(self.closes) == n + 1:
            net = abs(self.closes[-1] - self.closes[0])
            travel = sum(abs(self.closes[i] - self.closes[i - 1]) for i in range(1, len(self.closes)))
            self.er = (net / travel) if travel > 0 else 0.0

        prev = self._prev
        self._prev = (high, low, close)
        if prev is None:
            return
        prev_high, prev_low, prev_close = prev
        up = high - prev_high
        down = prev_low - low
        plus_dm = up if (up > down and up > 0) else 0.0
        minus_dm = down if (down > up and down > 0) else 0.0
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        period = self.adx_period
        if self._seeded < period:
            self._seeded += 1
            self._tr = (self._tr or 0.0) + tr
            self._plus_dm += plus_dm
            self._minus_dm += minus_dm
            if self._seeded == period:
                self._calc(tr_sum=self._tr)
            return
        # Wilder's smoothing
        self._tr = self._tr - (self._tr / period) + tr
        self._plus_dm = self._plus_dm - (self._plus_dm / period) + plus_dm
        self._minus_dm = self._minus_dm - (self._minus_dm / period) + minus_dm
        self._calc(tr_sum=self._tr)

    def _calc(self, tr_sum: float) -> None:
        period = self.adx_period
        if not tr_sum:
            return
        self.plus_di = 100.0 * self._plus_dm / tr_sum
        self.minus_di = 100.0 * self._minus_dm / tr_sum
        total = self.plus_di + self.minus_di
        dx = 100.0 * abs(self.plus_di - self.minus_di) / total if total else 0.0
        if self.adx is None:
            self._dx_sum += dx
            self._dx_count += 1
            if self._dx_count >= period:
                self.adx = self._dx_sum / self._dx_count
        else:
            self.adx = (self.adx * (period - 1) + dx) / period


def order_block_candle(
    bars: list[dict], k: int, side: str, lookback: int = 8, level: float | None = None
) -> tuple[int, dict] | None:
    """The candle the rectangle belongs on.

    The order block is the **last opposite-colour candle with the biggest body**
    before the extreme - the candle the institutions were filling while the
    crowd was buying (at a high) or selling (at a low). For a high we look for a
    green/up candle, for a low for a red/down one, walking back from the extreme
    pivot. On a tie the one closest to the extreme wins, because that is the one
    that actually supplied the liquidity.

    A candidate that does not overlap the extreme's side is skipped: a candle
    sitting *above* the high (or *below* the low) belongs to the previous swing,
    not to this block. That is what keeps the rectangle on the right candles.
    """
    if not bars:
        return None
    k = max(0, min(len(bars) - 1, int(k)))
    start = max(0, k - max(1, int(lookback)))
    best: tuple[int, dict] | None = None
    best_body = -1.0
    for j in range(k, start - 1, -1):
        bar = bars[j]
        open_, close = float(bar["open"]), float(bar["close"])
        up = close >= open_
        wanted = up if side == HIGH else not up
        if not wanted:
            continue
        if level is not None:
            if side == HIGH and float(bar["low"]) >= level:
                continue      # this candle sits above the high: not our block
            if side == LOW and float(bar["high"]) <= level:
                continue      # ... and this one sits below the low
        body = abs(close - open_)
        if body > best_body:
            best, best_body = (j, bar), body
    return best


def block_geometry(
    bars: list[dict], k: int, level: float, side: str, settings: dict[str, Any], atr: float | None
) -> tuple[float, float, dict[str, Any] | None, str]:
    """Where the rectangle goes: the exact high (low) down (up) to the order block.

    Returns ``(top, bottom, ob, source)``. The extreme of the zone is always the
    exact high/low the pattern is about - that is the level a stop goes behind -
    and the block extends to the far side of the order-block candle, so the
    rectangle covers the whole area price has to trade back through. That is why
    a zone no longer looks "active" after price has already come back into it.
    """
    found = order_block_candle(bars, k, side, int(settings.get("ob_lookback", 8)), level)
    atr = float(atr or 0.0)
    minimum = float(settings.get("ob_min_height_atr", 0.12)) * atr
    if minimum <= 0:
        # no ATR yet (the first bars of a warm-up): keep a visible, non-zero box
        minimum = max(abs(float(level)) * 1e-5, 1e-9)
    if found is None:
        pivot = bars[max(0, min(len(bars) - 1, k))]
        ob = {
            "index": None,
            "time": int(pivot.get("close_time") or pivot.get("open_time") or 0),
            "open": float(pivot["open"]),
            "high": float(pivot["high"]),
            "low": float(pivot["low"]),
            "close": float(pivot["close"]),
            "body": abs(float(pivot["close"]) - float(pivot["open"])),
            "color": "up" if float(pivot["close"]) >= float(pivot["open"]) else "down",
        }
        source = "pivot candle"
        if side == HIGH:
            return level, min(float(pivot["low"]), level - minimum), ob, source
        return max(float(pivot["high"]), level + minimum), level, ob, source

    j, bar = found
    open_, close = float(bar["open"]), float(bar["close"])
    ob = {
        "index": j,
        "time": int(bar.get("close_time") or bar.get("open_time") or 0),
        "open": open_,
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "close": close,
        "body": abs(close - open_),
        "color": "up" if close >= open_ else "down",
    }
    if side == HIGH:
        # the block always covers the order-block candle; the minimum height is
        # only a floor for a degenerate (zero-range) candle, never a reason to
        # push the rectangle away from the candle it marks
        bottom = min(float(bar["low"]), level)
        if level - bottom <= 1e-9:
            bottom = level - minimum
        return level, bottom, ob, "order block candle"
    top = max(float(bar["high"]), level)
    if top - level <= 1e-9:
        top = level + minimum
    return top, level, ob, "order block candle"


def grade_for_conditions(met: int, total: int = len(CONDITION_NAMES), cfg: dict | None = None) -> str:
    """The letter is the count of the book's conditions the setup satisfies.

    The cut-offs are not a taste: they were set on 90 days of real 1m BTC and
    then checked on a held-out 90 days (``tools/condition_study.py`` and
    ``docs/conditions.md``). ``grade_cuts`` in the settings moves them without a
    code change, so a different instrument can be re-calibrated from the same
    tool instead of the numbers being hard-coded.
    """
    cuts = (cfg or {}).get("grade_cuts") or {}
    a_plus = int(cuts.get("a_plus", 5))
    a = int(cuts.get("a", 4))
    b = int(cuts.get("b", 3))
    if total <= 0:
        return "C"
    if met >= a_plus:
        return "A+"
    if met >= a:
        return "A"
    if met >= b:
        return "B"
    return "C"


def grade_for(score: int) -> str:
    """The old score-only grade, kept for the ranking study and old reports."""
    if score >= 62:
        return "A+"
    if score >= 48:
        return "A"
    if score >= 34:
        return "B"
    return "C"


GRADE_ORDER = {"A+": 3, "A": 2, "B": 1, "C": 0}


# ------------------------------------------------------------------- the map
class MarketMap:
    """One instrument on one timeframe, updated bar by bar.

    The class is deliberately incremental: a bar costs a few dozen operations, so
    a thousand instruments can stream at once.
    """

    def __init__(self, symbol: str, timeframe: str, settings: dict[str, Any] | None = None) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.cfg = {**DEFAULTS, **(settings or {})}
        self.pivot_len = max(1, int(self.cfg["pivot_len"]))
        self.major_len = max(self.pivot_len + 1, int(self.cfg["major_len"]))
        self.disp_bars = max(1, int(self.cfg["disp_bars"]))
        self.keep = max(120, int(self.cfg["lookback"]))
        self.max_age = max(50, int(self.cfg["max_zone_age"]))
        self.mitigation_mode = str(self.cfg.get("mitigation_mode") or "zone")

        self.bars: deque[dict] = deque(maxlen=self.keep)
        self.atr: deque[float | None] = deque(maxlen=self.keep)
        self.period = max(1, int(self.cfg["atr_period"]))
        self._tr_seed: list[float] = []
        self.count = 0

        self.minor: deque[Pivot] = deque(maxlen=64)
        self.major: deque[Pivot] = deque(maxlen=16)
        self.zones: list[Zone] = []

        # live sweep per side: {"pivot": Pivot, "extreme": float, "index": int}
        self.sweeps: dict[str, dict[str, Any] | None] = {HIGH: None, LOW: None}
        self.external_trend: int | None = None
        self.gauge = TrendGauge(
            er_period=int(self.cfg.get("trend_lookback", 40)),
            adx_period=max(2, int(self.cfg.get("atr_period", 14))),
        )
        self.last_events: dict[str, Any] = {}
        self.seen: set[str] = set()
        # The candles around every confirmed pivot. A sweep can be confirmed long
        # after its pivot has fallen out of the 240-bar window, and the block must
        # still be drawn on the candle that really made the high - so keep it.
        self.pivot_ctx: dict[int, list[dict]] = {}
        # Round 13: the structure of the timeframe ABOVE this one, pushed in by the
        # engine on every bar. A setup is only a signal when the bigger picture is a
        # trend in the same direction - the book's "top down analysis" (p.104).
        self.context: dict[str, Any] = {}

    # ------------------------------------------------------------------ util
    @property
    def last_price(self) -> float | None:
        return float(self.bars[-1]["close"]) if self.bars else None

    def atr_now(self) -> float | None:
        for value in reversed(self.atr):
            if value is not None:
                return value
        return None

    @property
    def trend_label(self) -> str:
        return {1: "bull", -1: "bear", None: "unknown"}[self.external_trend]

    def active(self) -> list[Zone]:
        return [z for z in self.zones if z.status == "active"]

    # --------------------------------------------------------------- regime
    def regime(self) -> dict[str, Any]:
        """Trend or range, in numbers a trader can check.

        ``er``  the Kaufman efficiency ratio over ``trend_lookback`` bars,
        ``adx`` Wilder's ADX(14), ``dir`` +1/-1/0 the net direction over the same
        window. A market is only called trending when BOTH the efficiency ratio
        and the ADX agree, which is what keeps a wide, choppy range from being
        mistaken for a trend.
        """
        er = self.gauge.er
        adx = self.gauge.adx
        closes = list(self.gauge.closes)
        n = max(2, int(self.cfg.get("trend_lookback", 40)))
        direction = 0
        if len(closes) >= 2:
            span = closes[-1] - closes[0]
            direction = 1 if span > 0 else -1 if span < 0 else 0
        er_min = float(self.cfg.get("trend_er_min", 0.25))
        adx_min = float(self.cfg.get("trend_adx_min", 18.0))
        enough = er is not None and adx is not None
        trending = bool(enough and er >= er_min and adx >= adx_min and direction != 0)
        if not enough:
            state = "unknown"
        elif trending:
            state = "trend_up" if direction > 0 else "trend_down"
        else:
            state = "range"
        return {
            "state": state,
            "er": round(er, 4) if er is not None else None,
            "adx": round(adx, 2) if adx is not None else None,
            "dir": direction,
            "lookback": n,
        }

    # ------------------------------------------------------------- structure
    def structure(self) -> dict[str, Any]:
        """Trend or range, read the way the book reads it - from swing structure.

        The book's definitions, in order (all from confirmed pivots only, so
        nothing here can see the future):

        * **strong high / strong low** - a high or low that grabbed liquidity and
          then broke structure (p.42, p.45). ``self.major`` holds the ones that
          broke something; ``external_trend`` is the side of the last major break.
        * **a trend is HH + HL** (up) or **LH + LL** (down) - p.52: "external
          liquidity is the leg's highest point"; a trend that keeps failing to
          make a new high or a new higher low is not a trend.
        * **a range is equal highs or equal lows** - p.58: "RANGES account for 80%
          of a price's swings", and equal highs/lows are *liquidity being built*,
          not structure (p.42: "for every strong low there is a weak high").
        * **a deep retracement is not structure** - p.38: if price breaks structure
          and fails to deliver to a new premium/discount, that swing was just
          liquidity, so it does not count as an HL or LH.

        Returns a dict the page prints verbatim, so a card can say *why* a market
        was called a range instead of asserting it.
        """
        atr = self.atr_now() or 0.0
        highs = [p for p in self.major if p.side == HIGH]
        lows = [p for p in self.major if p.side == LOW]
        out: dict[str, Any] = {
            "state": "unknown",
            "why": "not enough confirmed swing highs and lows yet",
            "highs": [round(p.price, 8) for p in highs[-3:]],
            "lows": [round(p.price, 8) for p in lows[-3:]],
            "bos": self.external_trend,
            "bars": self.count,
        }
        if len(self.bars) < int(self.cfg.get("structure_min_bars", 30)) or not atr:
            return out
        if len(highs) < 2 or len(lows) < 2:
            out["why"] = "needs two confirmed swing highs and two swing lows"
            return out

        eq = float(self.cfg.get("structure_equal_atr", 0.15)) * atr
        (h2, h1) = highs[-2].price, highs[-1].price            # older, newer
        (l2, l1) = lows[-2].price, lows[-1].price
        hh = h1 > h2 + eq
        lh = h1 < h2 - eq
        hl = l1 > l2 + eq
        ll = l1 < l2 - eq
        out.update(
            {
                "hh": hh, "hl": hl, "lh": lh, "ll": ll,
                "last_high": round(h1, 8), "prev_high": round(h2, 8),
                "last_low": round(l1, 8), "prev_low": round(l2, 8),
                # the dealing range a discount/premium decision is made in
                "range_high": round(max(p.price for p in highs[-3:]), 8),
                "range_low": round(min(p.price for p in lows[-3:]), 8),
                "atr": round(atr, 8),
            }
        )
        mid = (out["range_high"] + out["range_low"]) / 2.0
        out["mid"] = round(mid, 8)
        price = self.last_price
        out["price"] = round(price, 8) if price is not None else None
        out["location"] = (
            "premium" if price is not None and price > mid
            else "discount" if price is not None and price < mid
            else "middle"
        )

        if hh and hl:
            state = "trend_up"
        elif lh and ll:
            state = "trend_down"
        elif not (hh or lh) or not (hl or ll):
            # equal highs or equal lows: the book's "price builds liquidity"
            state = "range"
            out["why"] = ("equal highs" if not (hh or lh) else "equal lows") +                          " - liquidity being built, not structure (p.42, p.58)"
        else:
            state = "range"
            out["why"] = "mixed structure: no clean HH+HL or LH+LL"

        # p.38: a pullback that takes back almost the whole leg is not structure
        if state == "trend_up":
            leg = out["last_high"] - out["prev_low"] if "prev_low" in out else 0.0
            if leg > 0 and price is not None and price < out["last_high"] - 0.0001:
                giveback = (out["last_high"] - price) / leg
                if giveback > float(self.cfg.get("structure_deep_retrace", 0.79)):
                    state = "range"
                    pct = min(giveback, 1.0) * 100
                    out["why"] = (f"price gave back {pct:.0f}% of the last up-leg - "
                                  "a deep retracement, not structure (p.38)")
        elif state == "trend_down":
            leg = out["prev_high"] - out["last_low"]
            if leg > 0 and price is not None and price > out["last_low"] + 0.0001:
                giveback = (price - out["last_low"]) / leg
                if giveback > float(self.cfg.get("structure_deep_retrace", 0.79)):
                    state = "range"
                    pct = min(giveback, 1.0) * 100
                    out["why"] = (f"price retraced {pct:.0f}% of the last down-leg - "
                                  "a deep retracement, not structure (p.38)")

        # the last major break has to agree with the swings, or it is a failed one
        if state in ("trend_up", "trend_down") and self.external_trend is not None:
            agree = (state == "trend_up" and self.external_trend > 0) or                     (state == "trend_down" and self.external_trend < 0)
            if not agree:
                out["why"] = "the last major break points the other way"
                state = "range"

        out["state"] = state
        if state != "range":
            # the earlier "why" described a reading that did not become the answer
            out["why"] = ("higher highs and higher lows (p.52)" if state == "trend_up"
                          else "lower highs and lower lows (p.52)")
        return out

    def trade_gate(self, zone: Zone, structure: dict[str, Any] | None = None,
                   context: dict[str, Any] | None = None) -> dict[str, Any]:
        """May this block be a *signal* - or is the market the wrong kind of market?

        This is the filter the user asked for in plain words: "even when the market
        is ranging you still give signals". It has three parts, each traceable to
        the book, and it returns the reasons in the negative so the page can print
        *why* a pattern was not promoted:

        1. **this timeframe must not be a range** - p.58;
        2. **the timeframe above it must not be a range either**, and the trade must
           be with it: long only in an up-structure, short only in a down-structure
           (p.39, p.52 - "go to the HTF and use the cycle to see where you are from
           the HTF structure");
        3. **premium / discount** - p.42 and p.110: a sell belongs above the middle
           of the dealing range (premium), a buy below it (discount).

        A weak level is never a signal, whatever the market is doing.
        """
        st = structure or self.structure()
        # `context` is the structure of the timeframe above, pushed in by the engine
        # on every bar; fall back to it so a zone can never be graded without it
        htf = self.context if context is None else context
        htf = htf or {}
        why: list[str] = []
        want = "trend_down" if zone.side == HIGH else "trend_up"
        side_name = "short" if zone.side == HIGH else "long"

        if zone.kind in ("weak_high", "weak_low"):
            why.append("a weak level is context (build-up), not a setup")
        if st.get("state") in ("range", "unknown"):
            detail = st.get("why") or st.get("state")
            why.append(f"{self.timeframe} structure is not a trend ({detail})")
        elif st.get("state") != want:
            why.append(f"{self.timeframe} structure is {st['state']} - a {side_name} needs {want}")

        mid = st.get("mid")
        # where the *block* sits in the dealing range - not where price is right now
        block_loc = None
        if mid is not None:
            block_loc = "premium" if zone.level > mid else "discount" if zone.level < mid else "middle"
        atr = st.get("atr") or 0.0
        edge = float(self.cfg.get("gate_premium_atr", 0.25)) * atr
        if zone.side == HIGH:
            if mid is not None and zone.level < mid + edge:
                why.append("the block sits in discount - a sell belongs in premium (p.42)")
        else:
            if mid is not None and zone.level > mid - edge:
                why.append("the block sits in premium - a buy belongs in discount (p.42)")

        htf_state = htf.get("state")
        htf_tf = htf.get("timeframe") or ""
        if htf:
            if htf_state in ("range", "unknown") or not htf_state:
                why.append(f"{htf_tf or 'the timeframe above'} structure is not a trend "
                           f"({htf.get('why') or htf_state or 'unknown'})")
            elif htf_state != want:
                why.append(f"{htf_tf} is {htf_state} - a {side_name} fights the bigger trend")

        return {
            "ok": not why,
            "why": why,
            "structure": st.get("state"),
            "structure_why": st.get("why"),
            "htf": htf_state,
            "htf_timeframe": htf_tf or None,
            "location": block_loc,
            "price_location": st.get("location"),
            "side": side_name,
            "mid": mid,
        }

    def confluence_at(self, side: str, level: float) -> list[str]:
        """Strong / very strong levels of this timeframe sitting at this level."""
        atr = self.atr_now() or 0.0
        tol = float(self.cfg.get("confluence_tol_atr", 0.5)) * atr if atr else 0.0
        if not tol:
            return []
        max_age = int(self.cfg.get("confluence_age", 400))
        out: list[str] = []
        for other in self.zones:
            if other.side != side or other.kind not in TREND_KINDS:
                continue
            if self.count - other.created_index > max_age:
                continue
            if abs(other.level - level) > tol:
                continue
            label = "very strong" if other.kind.startswith("very_strong") else "strong"
            text = f"{label} {side} at the same level"
            if text not in out:
                out.append(text)
        return out

    def next_liquidity(self, side: str, entry: float) -> tuple[float | None, str | None]:
        """The next pool of liquidity on the other side of this trade.

        This is the exit the trader asked for - "target = previous low/high" -
        not an invented R multiple. Candidates, nearest first:

        * a strong / very strong level of the opposite side already on the map,
        * a minor swing low (for a short) or swing high (for a long),
        * the lowest low (highest high) of the last ``plan_pool_bars`` candles.

        Only levels in front of the entry count: above it for a short, below it
        for a long.
        """
        atr = self.atr_now() or 0.0
        min_dist = float(self.cfg.get("plan_target_atr_min", 0.0) or 0.0) * atr
        closest: tuple[float, str] | None = None

        def offer(price: float | None, label: str) -> None:
            nonlocal closest
            if price is None or not isfinite_p(price):
                return
            if min_dist > 0 and abs(price - entry) < min_dist:
                return      # too close to pay for the risk: not a target
            if side == HIGH:          # a short: the target is below the entry
                if price >= entry:
                    return
                if closest is None or price > closest[0]:
                    closest = (price, label)
            else:                     # a long: the target is above the entry
                if price <= entry:
                    return
                if closest is None or price < closest[0]:
                    closest = (price, label)

        for zone in self.zones:
            if zone.side == side:
                continue
            if self.count - zone.created_index > int(self.cfg.get("plan_pool_age", 600)):
                continue
            label = "very strong level" if zone.kind.startswith("very_strong") else "strong level"
            offer(float(zone.level), f"next {zone.kind.replace('_', ' ')}")
        for pivot in self.minor:
            if pivot.side == side:
                continue
            offer(float(pivot.price), "swing")

        window = self._bars_list()[-int(self.cfg.get("plan_pool_bars", 120)) :]
        if window:
            if side == HIGH:
                offer(min(float(b["low"]) for b in window), "session low")
            else:
                offer(max(float(b["high"]) for b in window), "session high")
        if closest is None:
            return None, None
        if atr and abs(closest[0] - entry) < 0.25 * atr:
            return None, None  # too close to be a target
        return closest

    def plan_for(self, zone: Zone) -> dict[str, Any]:
        """Entry, stop and target for the trade this zone is about.

        * **entry** - the near edge of the block (its bottom for a short from a
          high, its top for a long from a low): the limit order fills the moment
          price comes back into the block, which is what "mitigation" means.
        * **stop** - ``plan_stop_atr`` ATR beyond the exact high/low. A wick
          through the level is the invalidation the book describes, so the stop
          sits just outside it.
        * **target** - :meth:`next_liquidity`: the next pool of liquidity, the
          trader's own exit rule.
        """
        atr = self.atr_now() or 0.0
        buffer = float(self.cfg.get("plan_stop_atr", 0.10)) * atr
        if buffer <= 0:
            # no ATR yet (the first bars of a warm-up still produce zones): the
            # stop must still sit *beyond* the level, so use a hair of the price
            buffer = max(abs(float(zone.level)) * 1e-4, 1e-9)
        high_side = zone.side == HIGH
        entry = float(zone.bottom) if high_side else float(zone.top)
        # "block": the invalidation is the whole red box, not just the exact
        # high/low - a wick through the box is noise, a close beyond it is the
        # pattern failing. The stop still sits a tenth of an ATR outside it.
        block = abs(float(zone.top) - float(zone.bottom)) if self.cfg.get("plan_stop_mode") == "block" else 0.0
        stop = (float(zone.level) + buffer + block) if high_side else (float(zone.level) - buffer - block)
        # a stop tighter than this fraction of price is inside the fee
        floor = float(self.cfg.get("plan_min_risk_pct", 0.0)) / 100.0 * abs(entry)
        distance = abs(stop - entry)
        if floor > 0 and distance < floor:
            distance = floor
            stop = entry + distance if high_side else entry - distance
        target, source = self.next_liquidity(zone.side, entry)
        risk = abs(stop - entry)
        reward = abs(entry - target) if target is not None else None
        rr = (reward / risk) if (risk > 0 and reward) else None
        return {
            "side": "short" if high_side else "long",
            "entry": round(entry, 8),
            "stop": round(stop, 8),
            "target": round(target, 8) if target is not None else None,
            "target_source": source,
            "risk": round(risk, 8),
            "reward": round(reward, 8) if reward is not None else None,
            "rr": round(rr, 2) if rr is not None else None,
            "entry_kind": "limit at the block edge",
            "stop_kind": self._stop_kind(zone, buffer, block, floor, distance, stop),
            "risk_pct": round(distance / abs(entry) * 100.0, 4) if entry else None,
        }

    def _stop_kind(self, zone: Zone, buffer: float, block: float, floor: float, distance: float,
                   stop: float) -> str:
        """A one-line reason for the stop, so a number is never unexplained."""
        if floor > 0 and distance > 0 and abs(abs(stop - float(zone.level)) - buffer - block) > 1e-12:
            return f"{self.cfg.get('plan_min_risk_pct')}% of price (fee floor)"
        atr_part = f"{self.cfg.get('plan_stop_atr', 0.10)} ATR"
        if block > 0:
            return f"beyond the red box + {atr_part}"
        return f"{atr_part} beyond the level"

    def evaluate_conditions(self, zone: Zone, regime: dict[str, Any] | None = None) -> Zone:
        """Check the zone against all ten conditions of the book.

        Nothing here changes *whether* a pattern exists - it only says which of
        the book's strengthening conditions this particular one satisfies, so the
        grade is a count of real, named conditions rather than a bare score.
        """
        regime = regime or self.regime()
        state = regime.get("state")
        conds = {
            "grab": zone.swept_level is not None,
            "ms": zone.break_level is not None,
            "major": bool(zone.swept_major),
            "inducement": zone.inducement >= int(self.cfg["induce_count"]),
            "algo": zone.fvg is not None,
            "displacement": (zone.disp_atr or 0.0) >= float(self.cfg.get("cond_disp_atr", 2.0)),
            "block": zone.block_src == "order block candle",
            "session": zone.session is not None,
            "confluence": bool(zone.confluence),
            "trend": state in ("trend_up", "trend_down"),
        }
        zone.conds = conds
        zone.cond_met = sum(1 for met in conds.values() if met)
        zone.cond_total = len(conds)
        return zone

    def score_zone(self, zone: Zone, regime: dict[str, Any] | None = None) -> Zone:
        """Turn the evidence into one number, and say why.

        The weights are a ranking, not a new rule; they are printed with every
        zone (``evidence`` keeps the book's reasons, ``trend``/``confluence``
        keep the market context) so a grade can always be traced back.
        """
        regime = regime or self.regime()
        kind = zone.kind
        # Weights calibrated on 90 days of real 1m BTC candles by
        # tools/zone_quality.py. Measured lift on "did price travel 2 ATR in
        # your favour before a close beyond the high/low":
        #   block >=1 ATR          79%   block <0.25 ATR        44%
        #   displacement >=4 ATR   82%   displacement <2 ATR     66%
        #   rejection block / VSA  83-88%  plain strong high/low 70%
        #   FVG >=1.5 ATR          80%   FVG <0.4 ATR            72%
        # Same-level strong confluence and the trend at the touch showed no lift
        # on this sample, so they are shown but weighted lightly.
        score = 0
        if kind.startswith("rejection_block"):
            score += 24
        elif kind.startswith("very_strong"):
            score += 22
        elif kind.startswith("algo_"):
            score += 16
        elif kind.startswith("strong_"):
            score += 8

        block = zone.block_atr
        if block is not None:
            if block >= 1.0:
                score += 20
            elif block >= 0.5:
                score += 8
            elif block >= 0.25:
                score += 3

        disp = zone.disp_atr
        if disp is not None:
            if disp >= 4.0:
                score += 12
            elif disp >= 2.0:
                score += 6
            elif disp >= 1.0:
                score += 2

        fvg = zone.fvg_atr
        if fvg is not None:
            if fvg >= 1.5:
                score += 6
            elif fvg >= 0.8:
                score += 4
            elif fvg >= 0.4:
                score += 2

        if zone.inducement >= int(self.cfg["induce_count"]):
            score += 4
        if zone.session:
            score += 3
        if zone.confluence:
            score += 3

        state = regime.get("state")
        trending = state in ("trend_up", "trend_down")
        aligned = (state == "trend_down" and zone.side == HIGH) or (state == "trend_up" and zone.side == LOW)
        if trending and aligned:
            score += 4
        zone.score = int(max(0, min(100, score)))
        self.evaluate_conditions(zone, regime)
        zone.grade = grade_for_conditions(zone.cond_met, zone.cond_total, self.cfg)
        zone.trend = {
            **regime,
            "aligned": bool(trending and aligned),
            "label": (
                "with the trend" if trending and aligned
                else "against the trend" if trending
                else "range" if state == "range"
                else "unknown"
            ),
        }
        return zone

    def adopt(self, data: dict[str, Any]) -> Zone | None:
        """Put a stored plan back on the map, so a restart does not lose the trade.

        Only the fields the plan needs are taken from the snapshot - the block, the
        side, the entry/stop/target and how far the plan had got. Its ``index``
        starts at "now" so the age limit cannot expire a zone the moment the server
        comes back; the key (which includes the formation time) is kept, so if the
        map later re-forms the same block it is recognised instead of duplicated.
        """
        key = data.get("key")
        if not key or key in self.seen:
            return None
        try:
            zone = Zone(
                kind=str(data["kind"]), side=str(data["side"]), symbol=self.symbol,
                timeframe=self.timeframe, top=float(data["top"]), bottom=float(data["bottom"]),
                level=float(data["level"]), index=self.count, time=int(data["time"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        zone.status = "active"
        zone.created_index = self.count
        zone.plan = data.get("plan")
        zone.plan_state = str(data.get("plan_state") or "waiting")
        zone.plan_filled_at = data.get("plan_filled_at")
        zone.plan_filled_price = data.get("plan_filled_price")
        zone.grade = str(data.get("grade") or zone.grade)
        zone.prob = data.get("prob")
        zone.net_r = data.get("net_r")
        zone.fee_r = data.get("fee_r")
        zone.tradable = bool(data.get("tradable"))
        zone.signal = bool(data.get("signal"))
        zone.measured = data.get("measured")
        zone.gate = data.get("gate")
        zone.conds = dict(data.get("conds") or {})
        zone.cond_met = int(data.get("cond_met") or 0)
        zone.cond_total = int(data.get("cond_total") or len(CONDITION_NAMES))
        zone.restored = True
        self.seen.add(key)
        self.zones.append(zone)
        return zone

    def _add_zone(self, zone: Zone) -> Zone | None:
        if zone.key in self.seen:
            return None
        self.seen.add(zone.key)
        zone.created_index = self.count
        # grade it now: the market context and the strong levels already on the
        # map are known at this instant, so a grade never depends on the future
        atr = self.atr_now() or 0.0
        if atr:
            zone.block_atr = round(abs(zone.top - zone.bottom) / atr, 3)
            if zone.fvg:
                zone.fvg_atr = round(abs(zone.fvg["top"] - zone.fvg["bottom"]) / atr, 3)
            if zone.swept_level is not None:
                zone.swept_major = any(
                    pivot.side == zone.side and abs(pivot.price - float(zone.swept_level)) < 1e-9
                    for pivot in self.major
                )
        zone.confluence = self.confluence_at(zone.side, zone.level)
        self.score_zone(zone)
        # round 13: is the market trending, on this timeframe and the one above, and
        # is the block on the right side of the dealing range? Computed here, at
        # formation, from closed candles only - the same inputs a trader has.
        zone.gate = self.trade_gate(zone)
        if zone.kind not in ("weak_high", "weak_low"):
            zone.plan = self.plan_for(zone)
            from .model import model as _model

            _model().rate(zone, self.cfg)
        self.zones.append(zone)
        # keep the list bounded: drop the oldest resolved zone first
        if len(self.zones) > 400:
            for i, candidate in enumerate(self.zones):
                if candidate.status != "active":
                    del self.zones[i]
                    break
        return zone

    # ------------------------------------------------------------------- add
    def add(self, bar: dict) -> dict[str, Any]:
        """Feed one CLOSED candle; return everything that happened on it."""
        self.bars.append(
            {
                "open_time": int(bar["open_time"]),
                "close_time": int(bar.get("close_time") or bar["open_time"]),
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": float(bar.get("volume") or 0.0),
            }
        )
        self._push_atr(bar)
        # the regime gauge only ever sees closed candles, like everything else
        try:
            self.gauge.update(float(bar["high"]), float(bar["low"]), float(bar["close"]))
        except Exception:  # a malformed bar must never stop the map
            pass
        index = self.count
        self.count += 1

        events: dict[str, Any] = {"pivots": [], "sweeps": [], "breaks": [], "zones": [], "resolved": []}
        self._confirm_pivots(index, events)
        self._resolve_zones(bar, index, events)
        self._track_plans(bar, index, events)
        self._track_sweeps(bar, index, events)
        self.last_events = {k: v for k, v in events.items() if v}
        return events

    def _push_atr(self, bar: dict) -> None:
        """Wilder's ATR, one bar at a time - a warm-up must not be quadratic."""
        if len(self.bars) < 2:
            self.atr.append(None)
            return
        prev_close = float(self.bars[-2]["close"])
        high, low = float(bar["high"]), float(bar["low"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        last = self.atr[-1] if self.atr else None
        if last is None:
            self._tr_seed.append(tr)
            if len(self._tr_seed) >= self.period:
                self.atr.append(sum(self._tr_seed[: self.period]) / self.period)
            else:
                self.atr.append(None)
        else:
            self.atr.append((last * (self.period - 1) + tr) / self.period)

    def _bars_list(self) -> list[dict]:
        return list(self.bars)

    def _pivot_at(self, position: int, width: int) -> tuple[str, float] | None:
        bars = self._bars_list()
        start = max(0, position - width)
        window = bars[start : position + width + 1]
        if len(window) < width * 2 + 1:
            return None
        bar = bars[position]
        if float(bar["high"]) >= max(float(b["high"]) for b in window):
            return HIGH, float(bar["high"])
        if float(bar["low"]) <= min(float(b["low"]) for b in window):
            return LOW, float(bar["low"])
        return None

    # ---------------------------------------------------------------- pivots
    def _confirm_pivots(self, index: int, events: dict) -> None:
        bars = self._bars_list()
        for width, bucket, major in (
            (self.pivot_len, self.minor, False),
            (self.major_len, self.major, True),
        ):
            position = len(bars) - 1 - width
            if position < width:
                continue
            found = self._pivot_at(position, width)
            if found is None:
                continue
            side, price = found
            if any(p.price == price and p.side == side and p.major == major for p in bucket):
                continue
            pivot = Pivot(side=side, price=price, index=index - width, time=int(bars[position]["close_time"]), major=major)
            bucket.append(pivot)
            depth = int(self.cfg.get("ob_lookback", 8))
            self.pivot_ctx[pivot.time] = [
                {
                    "open_time": int(b["open_time"]),
                    "close_time": int(b.get("close_time") or b["open_time"]),
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "volume": float(b.get("volume") or 0.0),
                }
                for b in bars[max(0, position - depth) : position + 1]
            ]
            if len(self.pivot_ctx) > 256:  # bounded, oldest first
                for stale in list(self.pivot_ctx)[: len(self.pivot_ctx) - 256]:
                    self.pivot_ctx.pop(stale, None)
            events["pivots"].append(pivot.as_dict())
            if major:
                # the last major break gives the external trend (p.53 MS)
                if side == HIGH and float(bars[-1]["close"]) > price:
                    self.external_trend = 1
                elif side == LOW and float(bars[-1]["close"]) < price:
                    self.external_trend = -1
            else:
                self._inspect_algo_candle(pivot, index, events)

    # ------------------------------------------------------- algo candle (p.67)
    def _inspect_algo_candle(self, pivot: Pivot, index: int, events: dict) -> None:
        """The pivot bar is the candidate: is it the candle that absorbed orders?"""
        bars = self._bars_list()
        offset = self.count - len(bars)
        k = pivot.index - offset  # position of the pivot candle in the window
        if k < 0 or k >= len(bars):
            return
        bar = bars[k]
        wanted = "bearish" if pivot.side == HIGH else "bullish"
        gap = None
        for j in range(k + 2, min(len(bars), k + 2 + self.disp_bars)):
            found = fair_value_gap(bars[j - 2], bars[j - 1], bars[j], j)
            if found is not None and found.side == wanted:
                gap = found
                break
        if gap is None:
            return  # no displacement, no FVG -> not an algo candle

        atr = self.atr_now() or 0.0
        if atr and gap.size < float(self.cfg["min_fvg_atr"]) * atr:
            return  # a gap a fraction of the ATR is noise, not displacement
        # the rectangle: from the exact high/low down (up) to the far side of the
        # last opposite-colour candle with the biggest body - the order block
        level = float(bar["high"]) if pivot.side == HIGH else float(bar["low"])
        top, bottom, ob, block_src = block_geometry(bars, k, level, pivot.side, self.cfg, atr)
        # p.89: "when the momentum shift is aggressive, the breaker is very
        # strong" - so measure the aggression of the leg out of the extreme
        leg = bars[k + 1 :]
        if leg and atr:
            if pivot.side == HIGH:
                furthest = min(float(b["low"]) for b in leg)
                disp = (level - furthest) / atr
            else:
                furthest = max(float(b["high"]) for b in leg)
                disp = (furthest - level) / atr
        else:
            disp = None

        evidence = ["displacement left an FVG"] if gap else []
        strength = 1

        # inducement / liquidity pool around the extreme (p.45, p.69, p.91)
        window = bars[max(0, k - 30) : k + 1]
        tol = float(self.cfg["induce_tol"]) * atr if atr else 0.0
        if pivot.side == HIGH:
            induce = equal_levels([float(b["high"]) for b in window], tol) - 1
        else:
            induce = equal_levels([float(b["low"]) for b in window], tol) - 1
        induce = max(0, induce)
        if induce >= int(self.cfg["induce_count"]):
            evidence.append(f"inducement: {induce + 1} equal {'highs' if pivot.side == HIGH else 'lows'}")
            strength += 1

        # liquidity grab: did this candle take out an earlier swing? (p.45, p.69)
        earlier = [p for p in self.minor if p.index < pivot.index and p.side == pivot.side]
        grabbed = None
        if earlier:
            prior = max(earlier, key=lambda p: p.index)
            if (pivot.side == HIGH and level > prior.price) or (pivot.side == LOW and level < prior.price):
                grabbed = prior.price
                evidence.append("grabbed the previous swing's liquidity")

        # momentum shift: the displacement closed beyond the opposite swing
        opposite = [p for p in self.minor if p.side != pivot.side and p.index < pivot.index]
        shift_level = None
        if opposite:
            prior = max(opposite, key=lambda p: p.index)
            closes = [float(b["close"]) for b in bars[k + 1 :]]
            if pivot.side == HIGH and any(c < prior.price for c in closes):
                shift_level = prior.price
                evidence.append("closed through the opposite swing (momentum shift)")
            elif pivot.side == LOW and any(c > prior.price for c in closes):
                shift_level = prior.price
                evidence.append("closed through the opposite swing (momentum shift)")

        session = session_of(pivot.time, self.cfg["sessions"])
        if session:
            evidence.append(f"forms on the {session} session")

        if grabbed is not None:
            strength += 1
        if shift_level is not None:
            strength += 1

        base_kind = "algo_bear" if pivot.side == HIGH else "algo_bull"
        very_strong = grabbed is not None and shift_level is not None and gap is not None and induce >= int(self.cfg["induce_count"])
        kind = ("very_strong_algo_bear" if pivot.side == HIGH else "very_strong_algo_bull") if very_strong else base_kind

        zone = Zone(
            kind=kind,
            side=pivot.side,
            symbol=self.symbol,
            timeframe=self.timeframe,
            top=top,
            bottom=bottom,
            level=level,
            index=pivot.index,
            time=pivot.time,
            strength=strength,
            evidence=evidence,
            swept_level=grabbed,
            break_level=shift_level,
            fvg={"side": gap.side, "top": gap.top, "bottom": gap.bottom, "time": gap.time},
            inducement=induce,
            session=session,
            ob=ob,
            block_src=block_src,
        )
        zone.disp_atr = round(disp, 3) if disp is not None else None
        if self._add_zone(zone):
            events["zones"].append(zone.as_dict())

        # The same candle is a REJECTION BLOCK when it is the extreme of a
        # session AND has inducement - that is the book's "STRONG REJECTION
        # BLOCK = HAVE AN INDUCEMENT + forms on a session high/low" (p.90/91).
        if (
            session
            and induce >= int(self.cfg["induce_count"])
            and self._is_session_extreme(k, bars, session, pivot.side)
        ):
            kind = "rejection_block_bear" if pivot.side == HIGH else "rejection_block_bull"
            block = Zone(
                kind=kind,
                side=pivot.side,
                symbol=self.symbol,
                timeframe=self.timeframe,
                top=top,
                bottom=bottom,
                level=level,
                index=pivot.index,
                time=pivot.time,
                strength=strength,
                evidence=evidence + [f"session high/low of the {session} session"],
                swept_level=grabbed,
                break_level=shift_level,
                fvg={"side": gap.side, "top": gap.top, "bottom": gap.bottom, "time": gap.time},
                inducement=induce,
                session=session,
                ob=ob,
                block_src=block_src,
            )
            if self._add_zone(block):
                events["zones"].append(block.as_dict())

    def _is_session_extreme(self, k: int, bars: list[dict], session: str, side: str) -> bool:
        """Is bar ``k`` the high (low) of its own session, not just inside one?"""
        sessions = self.cfg["sessions"]
        start = k
        while start - 1 >= 0 and session_of(int(bars[start - 1]["close_time"]), sessions) == session:
            start -= 1
        end = k
        while end + 1 < len(bars) and session_of(int(bars[end + 1]["close_time"]), sessions) == session:
            end += 1
        if end - start + 1 < 6:
            return False  # too young to have a high/low worth drawing
        window = bars[start : end + 1]
        if side == HIGH:
            return float(bars[k]["high"]) >= max(float(b["high"]) for b in window)
        return float(bars[k]["low"]) <= min(float(b["low"]) for b in window)

    # ---------------------------------------------------------------- sweeps
    def _track_sweeps(self, bar: dict, index: int, events: dict) -> None:
        """Liquidity grab first, then the break that confirms it (p.42).

        The pending grab is checked BEFORE the current candle is allowed to arm
        a new one - otherwise a candle that sweeps one side would erase the grab
        still waiting on the other side.
        """
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        for side in (HIGH, LOW):
            sweep = self.sweeps[side]
            if sweep is None:
                continue
            pivot: Pivot = sweep["pivot"]
            if side == HIGH:
                sweep["extreme"] = max(sweep["extreme"], high)
            else:
                sweep["extreme"] = min(sweep["extreme"], low)
            opposite = self._opposite_pivot(side, pivot)
            broke = False
            if opposite is not None and index > sweep["index"]:
                broke = close < opposite.price if side == HIGH else close > opposite.price
            if broke:
                self._make_strong_high(pivot, opposite, sweep["extreme"], bar, index, events)
                self.sweeps[side] = None
            elif index - sweep["index"] > max(30, self.pivot_len * 10):
                self.sweeps[side] = None  # stale, forget it

        for side in (HIGH, LOW):
            if self.sweeps[side] is not None:
                continue
            level = self._sweepable(side, high, low)
            if level is None:
                continue
            self.sweeps[side] = {"pivot": level, "extreme": high if side == HIGH else low, "index": index}
            events["sweeps"].append({"side": side, "price": level.price, "time": int(bar["close_time"])})

    def _sweepable(self, side: str, high: float, low: float) -> Pivot | None:
        for pivot in reversed(self.minor):
            if pivot.side != side:
                continue
            if self.count - pivot.index > self.max_age:
                continue
            if side == HIGH and high > pivot.price:
                return pivot
            if side == LOW and low < pivot.price:
                return pivot
        return None

    def _opposite_pivot(self, side: str, pivot: Pivot) -> Pivot | None:
        """The swing whose break confirms the manipulation (p.42).

        The pullback that forms AFTER the grab is the one that matters; only if
        there is none yet do we fall back to an older swing on the other side.
        """
        candidates = [
            p
            for p in self.minor
            if p.side != side and p.index != pivot.index
            and ((p.price < pivot.price) if side == HIGH else (p.price > pivot.price))
        ]
        if not candidates:
            return None
        after = [p for p in candidates if p.index > pivot.index]
        pool = after or [p for p in candidates if p.index < pivot.index]
        return max(pool, key=lambda p: p.index) if pool else None

    # ------------------------------------------------- strong / very strong (p.42, p.45)
    def _make_strong_high(
        self, pivot: Pivot, opposite: Pivot, extreme: float, bar: dict, index: int, events: dict
    ) -> None:
        bars = self._bars_list()
        offset = self.count - len(bars)
        k = pivot.index - offset
        k = max(0, min(len(bars) - 1, k))
        side = pivot.side
        ctx = self.pivot_ctx.get(pivot.time)
        if k < 0 or k >= len(bars) or float(bars[k].get("close_time") or 0) != float(pivot.time):
            # the pivot is older than the window: use the candles kept with it
            if not ctx:
                return
            candle = ctx[-1]
            level = float(candle["high"]) if side == HIGH else float(candle["low"])
            top, bottom, ob, block_src = block_geometry(ctx, len(ctx) - 1, level, side, self.cfg, self.atr_now())
        else:
            candle = bars[k]
            level = float(candle["high"]) if side == HIGH else float(candle["low"])
            top, bottom, ob, block_src = block_geometry(bars, k, level, side, self.cfg, self.atr_now())

        evidence = [
            "liquidity grabbed above the swing" if side == HIGH else "liquidity grabbed below the swing",
            "structure broke the other way",
        ]
        strength = 2

        # was the swept level itself a strong (major) level? p.45
        strong_level = any(p.side == side and abs(p.price - pivot.price) < 1e-9 for p in self.major)
        if strong_level:
            evidence.append("the level it grabbed was a strong (major) level")
            strength += 1

        # a major break on this candle? (p.53 MS)
        major_break = any(
            p.side != side and ((side == HIGH and float(bar["close"]) < p.price) or (side == LOW and float(bar["close"]) > p.price))
            for p in self.major
        )
        if major_break:
            evidence.append("this break is a major one (MS)")
            strength += 1

        # an FVG in the leg that broke? p.69/p.87
        gap = None
        for j in range(max(2, k), len(bars)):
            found = fair_value_gap(bars[j - 2], bars[j - 1], bars[j], j)
            if found is not None and found.side == ("bearish" if side == HIGH else "bullish"):
                gap = found
                break
        if gap is not None:
            evidence.append("the leg left an FVG")
            strength += 1

        atr = self.atr_now() or 0.0
        tol = float(self.cfg["induce_tol"]) * atr if atr else 0.0
        window = bars[max(0, k - 30) : k + 1]
        values = [float(b["high"]) for b in window] if side == HIGH else [float(b["low"]) for b in window]
        induce = max(0, equal_levels(values, tol) - 1)
        if induce >= int(self.cfg["induce_count"]):
            evidence.append(f"inducement: {induce + 1} equal {'highs' if side == HIGH else 'lows'}")
            strength += 1

        session = session_of(int(candle["close_time"]), self.cfg["sessions"])

        # p.89: how aggressive was the leg out of this extreme? Measured on every
        # pattern, not only on the algo-candle path, because the checklist below
        # asks the same question of all of them.
        disp = None
        if atr:
            leg = bars[k + 1 :]
            if leg:
                if side == HIGH:
                    furthest = min(float(b["low"]) for b in leg)
                    disp = (level - furthest) / atr
                else:
                    furthest = max(float(b["high"]) for b in leg)
                    disp = (furthest - level) / atr

        # strong high/low
        kind = "strong_high" if side == HIGH else "strong_low"
        zone = Zone(
            kind=kind,
            side=side,
            symbol=self.symbol,
            timeframe=self.timeframe,
            top=top,
            bottom=bottom,
            level=level,
            index=pivot.index,
            time=pivot.time,
            strength=strength,
            evidence=evidence,
            swept_level=pivot.price,
            break_level=opposite.price,
            fvg={"side": gap.side, "top": gap.top, "bottom": gap.bottom, "time": gap.time} if gap else None,
            inducement=induce,
            session=session,
            ob=ob,
            block_src=block_src,
        )
        if disp is not None and disp > 0:
            zone.disp_atr = round(disp, 3)
        # p.45: very strong = grabbed liquidity + broke a strong high/low + inducement
        if strong_level and induce >= int(self.cfg["induce_count"]):
            zone.kind = "very_strong_high" if side == HIGH else "very_strong_low"
            zone.evidence.append("very strong: strong level + inducement")
        if self._add_zone(zone):
            events["zones"].append(zone.as_dict())

        # the opposite side is now the WEAK high/low of the pair (p.42)
        weak = Zone(
            kind="weak_low" if side == HIGH else "weak_high",
            side=LOW if side == HIGH else HIGH,
            symbol=self.symbol,
            timeframe=self.timeframe,
            top=max(opposite.price, opposite.price),  # a line, drawn as a flat block
            bottom=opposite.price,
            level=opposite.price,
            index=opposite.index,
            time=opposite.time,
            strength=1,
            evidence=["the swing that failed to hold (weak)"],
            swept_level=pivot.price,
            break_level=opposite.price,
        )
        if self._add_zone(weak):
            events["zones"].append(weak.as_dict())
        events["breaks"].append({"side": side, "level": opposite.price, "time": int(bar["close_time"])})

    def _fill_plan(self, zone: Zone, index: int, at: float) -> None:
        """Price came back into the block: the plan's limit order is filled."""
        if not zone.plan:
            # a weak high/low is context, not a setup: price revisiting it fills
            # nothing. (This used to mark it "filled", which - once liveness is
            # the plan - would have kept a planless zone on the live list for ever.)
            return
        if zone.plan_state != "waiting":
            return
        zone.plan_state = "filled"
        zone.plan_filled_at = int(at)
        zone.plan_filled_price = zone.plan.get("entry") if zone.plan else zone.mitigated_price
        # a target that the very same candle already reached counts as filled and done
        if zone.plan and zone.plan.get("target") is not None:
            bar = self._bars_list()[-1]
            target = float(zone.plan["target"])
            short = zone.plan.get("side") == "short"
            if (short and float(bar["low"]) <= target) or ((not short) and float(bar["high"]) >= target):
                zone.plan_state = "target"
                zone.plan_result_at = int(at)
                zone.plan_result_r = zone.plan.get("rr")

    def _break_plan(self, zone: Zone, bar: dict) -> None:
        """A close through the level: whatever the plan's state, the trade is over."""
        if not zone.plan or zone.plan.get("target") is None:
            return
        if zone.plan_state in ("target", "stopped"):
            return
        if zone.plan_state == "waiting":
            # price had to trade through the block to close beyond the level,
            # so the limit filled first and the stop was taken
            zone.plan_state = "filled"
            zone.plan_filled_at = int(bar["close_time"])
            zone.plan_filled_price = zone.plan.get("entry")
        zone.plan_state = "stopped"
        zone.plan_result_at = int(bar["close_time"])
        zone.plan_result_r = -1.0

    def _entry_regime(self, zone: Zone) -> dict[str, Any]:
        """Regime at the touch, plus whether it agrees with the zone's trade."""
        regime = self.regime()
        aligned = (regime["state"] == "trend_down" and zone.side == HIGH) or (
            regime["state"] == "trend_up" and zone.side == LOW
        )
        return {**regime, "aligned": bool(aligned)}

    # ---------------------------------------------------------- mitigation
    def _resolve_zones(self, bar: dict, index: int, events: dict) -> None:
        """p.48: a zone lives until price mitigates it, or until it is broken."""
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        for zone in self.zones:
            if zone.status != "active":
                continue
            age = index - zone.created_index
            if age > self.max_age:
                zone.status = "invalidated"
                zone.invalidated_at = int(bar["close_time"])
                zone.invalidated_price = close
                events["resolved"].append({"key": zone.key, "status": "expired"})
                continue
            if zone.side == HIGH:
                # the high is broken for good when a candle CLOSES above it
                if close > zone.top:
                    zone.status = "invalidated"
                    zone.invalidated_at = int(bar["close_time"])
                    zone.invalidated_price = close
                    self._break_plan(zone, bar)
                    events["resolved"].append({"key": zone.key, "status": "invalidated"})
                    continue
                reached = high >= zone.level if self.mitigation_mode == "level" else high >= zone.bottom
                if reached:
                    zone.status = "mitigated"
                    zone.mitigated_at = int(bar["close_time"])
                    zone.mitigated_index = index
                    # the price a sell would actually have been filled at: the
                    # highest point of the returning candle inside the block
                    zone.mitigated_price = high if self.mitigation_mode == "level" else min(high, zone.top)
                    zone.entry_trend = self._entry_regime(zone)
                    self._fill_plan(zone, index, float(bar["close_time"]))
                    events["resolved"].append({"key": zone.key, "status": "mitigated"})
            else:
                if close < zone.bottom:
                    zone.status = "invalidated"
                    zone.invalidated_at = int(bar["close_time"])
                    zone.invalidated_price = close
                    self._break_plan(zone, bar)
                    events["resolved"].append({"key": zone.key, "status": "invalidated"})
                    continue
                reached = low <= zone.level if self.mitigation_mode == "level" else low <= zone.top
                if reached:
                    zone.status = "mitigated"
                    zone.mitigated_at = int(bar["close_time"])
                    zone.mitigated_index = index
                    zone.mitigated_price = low if self.mitigation_mode == "level" else max(low, zone.bottom)
                    zone.entry_trend = self._entry_regime(zone)
                    self._fill_plan(zone, index, float(bar["close_time"]))
                    events["resolved"].append({"key": zone.key, "status": "mitigated"})

    def _track_plans(self, bar: dict, index: int, events: dict) -> None:
        """Follow a filled plan until it reaches its target or its stop.

        This is bookkeeping, not a new rule: the target and the stop are the ones
        already printed on the zone, and they are only ever checked on candles
        that came **after** the entry filled. If a single candle can touch both,
        the stop is assumed - the conservative reading.
        """
        high, low = float(bar["high"]), float(bar["low"])
        for zone in self.zones:
            if zone.plan_state != "filled" or not zone.plan:
                continue
            plan = zone.plan
            stop, target = plan.get("stop"), plan.get("target")
            if stop is None or target is None:
                continue
            short = plan.get("side") == "short"
            hit_stop = high >= stop if short else low <= stop
            hit_target = low <= target if short else high >= target
            if hit_stop:
                zone.plan_state = "stopped"
                zone.plan_result_at = int(bar["close_time"])
                zone.plan_result_r = -1.0
                events["resolved"].append({"key": zone.key, "status": "plan_stopped"})
            elif hit_target:
                zone.plan_state = "target"
                zone.plan_result_at = int(bar["close_time"])
                zone.plan_result_r = plan.get("rr")
                events["resolved"].append({"key": zone.key, "status": "plan_target"})

    # ------------------------------------------------------------------ view
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for zone in self.zones:
            out[zone.kind] = out.get(zone.kind, 0) + 1
        return out

    def snapshot(self, limit: int = 60) -> dict[str, Any]:
        active = sorted(self.active(), key=lambda z: -z.time)
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": len(self.bars),
            "price": self.last_price,
            "atr": self.atr_now(),
            "external_trend": self.external_trend,
            "regime": self.regime(),
            "counts": self.counts(),
            "zones": [z.as_dict() for z in active[:limit]],
        }
