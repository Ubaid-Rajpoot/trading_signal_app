"""A setup is not a moment - it is a thing with a life.

The engine used to shout once, on the candle that confirmed the setup, and then
forget about it. That is why a setup the trader could see on their own chart
("it is still valid, it has not been mitigated yet") showed up in the app as
nothing at all: it was confirmed before the app was running, or the alert
scrolled away, and there was no such thing as "the state of a setup".

Here every published setup becomes a record with a state machine:

    pending      price has not come back into the order block yet  -> tradeable
    active       price is in the zone (the entry is filled)        -> in a trade
    win          the target was reached
    stopped      the stop was hit
    expired      too many bars went by without the zone being touched
    invalidated  the stop was taken before the entry was ever filled

``valid`` means pending or active - a setup you can still act on. Everything
else is history, and the UI keeps it visible instead of deleting it, because
"this idea died and here is where" is information, not noise.

The **order block** is the last candle in the opposite direction before the
displacement that broke structure (the classic definition). Its high/low is the
zone, and the zone is drawn on the chart:

    SHORT  last bullish candle before the down break   (supply)
    LONG   last bearish candle before the up break     (demand)

That is the red box on the trader's own chart: the top of the drop, not the
close of the break candle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALID = {"pending", "active"}
DONE = {"win", "stopped", "expired", "invalidated"}


def order_block(candles: list[dict], side: str, lookback: int = 12) -> dict[str, Any] | None:
    """The last opposite-colour candle before the break candle (the zone).

    ``candles[-1]`` is the candle that broke structure. A SHORT looks for the
    last bullish candle in the bars before it, a LONG for the last bearish one.
    """
    if len(candles) < 2:
        return None
    upto = len(candles) - 1
    start = max(0, upto - lookback)
    for i in range(upto - 1, start - 1, -1):
        bar = candles[i]
        bullish = float(bar["close"]) > float(bar["open"])
        bearish = float(bar["close"]) < float(bar["open"])
        if (side == "SHORT" and bullish) or (side == "LONG" and bearish):
            high, low = float(bar["high"]), float(bar["low"])
            return {
                "high": high,
                "low": low,
                "mid": (high + low) / 2,
                "time": int(bar["close_time"]),
                "index_ago": upto - i,
                "kind": "supply" if side == "SHORT" else "demand",
            }
    # no opposite candle in sight (a straight run): use the break candle itself
    bar = candles[-1]
    high, low = float(bar["high"]), float(bar["low"])
    return {
        "high": high,
        "low": low,
        "mid": (high + low) / 2,
        "time": int(bar["close_time"]),
        "index_ago": 0,
        "kind": "displacement" if side != "SHORT" else "supply",
    }


@dataclass
class SetupRecord:
    """One confirmed setup and everything that happened to it afterwards."""

    key: str
    symbol: str
    timeframe: str
    side: str
    created_ms: int
    entry: float
    stop: float
    target: float | None
    rr: float | None
    zone: dict[str, Any] | None
    swept_level: float
    swept_side: str
    sweep_ms: int
    bos_level: float
    bos_ms: int
    break_kind: str = "SHIFT"
    hvb_ms: int | None = None
    hvb_side: str | None = None
    volume_ratio: float | None = None
    score: float | None = None
    score_parts: dict[str, Any] = field(default_factory=dict)
    depth: dict[str, Any] | None = None
    trend_dir: int | None = None
    trend_clean: bool | None = None
    reasons: list[str] = field(default_factory=list)
    state: str = "pending"
    state_since: int = 0
    touched_ms: int | None = None
    closed_ms: int | None = None
    last_price: float | None = None
    bars: int = 0
    max_bars: int = 240

    def __post_init__(self) -> None:
        self.state_since = self.created_ms
        if self.zone is None:
            self.zone = {"high": self.entry, "low": self.entry, "mid": self.entry}

    # ------------------------------------------------------------------ view
    @property
    def valid(self) -> bool:
        return self.state in VALID

    @property
    def mitigated(self) -> bool:
        """Has price traded back into the order block at all?

        This is the trader's own wording ("it hasn't been mitigated yet") and it
        is tracked separately from the state, so a setup that is old but untouched
        can still be described correctly instead of being called "expired" and
        losing the information that nothing has used it.
        """
        return self.touched_ms is not None

    @property
    def target_2r(self) -> float:
        """Where 2R sits, so every setup has a measurable target."""
        risk = abs(self.entry - self.stop)
        if self.side == "SHORT":
            return self.entry - 2 * risk
        return self.entry + 2 * risk

    def r_multiple(self, price: float | None = None) -> float | None:
        price = self.last_price if price is None else price
        risk = abs(self.entry - self.stop)
        if price is None or risk <= 0:
            return None
        move = (self.entry - price) if self.side == "SHORT" else (price - self.entry)
        return round(move / risk, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "state": self.state,
            "valid": self.valid,
            "mitigated": self.mitigated,
            "created_ms": self.created_ms,
            "state_since": self.state_since,
            "touched_ms": self.touched_ms,
            "closed_ms": self.closed_ms,
            "bars": self.bars,
            "entry": round(self.entry, 8),
            "stop": round(self.stop, 8),
            "target": None if self.target is None else round(self.target, 8),
            "target_2r": round(self.target_2r, 8),
            "rr": self.rr,
            "r_now": self.r_multiple(),
            "zone": self.zone,
            "swept_level": self.swept_level,
            "swept_side": self.swept_side,
            "sweep_ms": self.sweep_ms,
            "bos_level": self.bos_level,
            "bos_ms": self.bos_ms,
            "break_kind": self.break_kind,
            "hvb_ms": self.hvb_ms,
            "hvb_side": self.hvb_side,
            "volume_ratio": self.volume_ratio,
            "score": self.score,
            "score_parts": self.score_parts,
            "depth": self.depth,
            "trend_dir": self.trend_dir,
            "trend_clean": self.trend_clean,
            "reasons": self.reasons,
            "last_price": self.last_price,
        }

    # --------------------------------------------------------------- lifecycle
    def advance(self, bar: dict, max_bars: int | None = None) -> bool:
        """One closed bar moves the state on. Returns True if anything changed.

        Conservative where it has to be: if a single bar contains both the target
        and the stop, the **stop** is assumed to have been hit first.
        """
        if self.state in DONE:
            return False
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        self.last_price = close
        self.bars += 1
        limit = self.max_bars if max_bars is None else max_bars
        changed = False

        if self.state == "pending":
            touched = high >= self.zone["low"] and low <= self.zone["high"]
            if touched:
                self.state, self.state_since, self.touched_ms = "active", int(bar["close_time"]), int(bar["close_time"])
                return True
            beyond_stop = high >= self.stop if self.side == "SHORT" else low <= self.stop
            if beyond_stop:
                self.state, self.state_since = "invalidated", int(bar["close_time"])
                self.closed_ms = int(bar["close_time"])
                return True
            if self.bars >= limit:
                self.state, self.state_since = "expired", int(bar["close_time"])
                self.closed_ms = int(bar["close_time"])
                return True
            return False

        # active: the trade is on
        hit_target = self.target is not None and (
            low <= self.target if self.side == "SHORT" else high >= self.target
        )
        hit_stop = high >= self.stop if self.side == "SHORT" else low <= self.stop
        if hit_stop:
            self.state, self.state_since = "stopped", int(bar["close_time"])
            self.closed_ms = int(bar["close_time"])
            changed = True
        elif hit_target:
            self.state, self.state_since = "win", int(bar["close_time"])
            self.closed_ms = int(bar["close_time"])
            changed = True
        elif self.bars >= limit and hit_target is False:
            # no target in reach and it has been open far too long
            self.state, self.state_since = "expired", int(bar["close_time"])
            self.closed_ms = int(bar["close_time"])
            changed = True
        return changed
