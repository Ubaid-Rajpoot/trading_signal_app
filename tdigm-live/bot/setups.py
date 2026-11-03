"""A setup is not a moment - it is a thing with a life.

The engine used to shout once, on the candle that confirmed the setup, and then
forget about it. That is why a setup the trader could see on their own chart
("it is still valid, it has not been mitigated yet") showed up in the app as
nothing at all: it was confirmed before the app was running, or the alert
scrolled away, and there was no such thing as "the state of a setup".

Here every published setup becomes a record with a state machine:

    pending      neither the order block nor eligible breaker was mitigated
    active       a mitigation fixed the entry and armed the 1:4 target
    win          the post-mitigation 4R target was reached
    stopped      the post-mitigation stop was reached
    expired      optional positive max-bars limit elapsed (disabled by default)
    invalidated  the stop was taken before any mitigation (no trade was filled)

``valid`` means pending or active - a setup you can still act on. Everything
else is history, and the UI keeps it visible instead of deleting it, because
"this idea died and here is where" is information, not noise.

The **order block** is the last candle in the opposite direction before the
displacement that broke structure. An aggressive displacement also promotes the
paired endpoint candle on its left to an ICT-style **breaker block** (book
pp.88–89). Either zone may mitigate the setup. Its high/low is drawn on chart:

    SHORT  last bullish candle before the down break   (supply)
    LONG   last bearish candle before the up break     (demand)

That is the red box on the trader's own chart: the top of the drop, not the
close of the break candle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALID = {"pending", "active"}
DONE = {"win", "stopped", "breakeven", "expired", "invalidated"}
LIFECYCLE_VERSION = 3
DEFAULT_TARGET_RR = 4.0


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
                "type": "order_block",
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
        "type": "order_block",
    }


def breaker_block(
    candles: list[dict],
    side: str,
    broke_level_time: int,
    stop: float,
    *,
    min_body_atr: float = 1.0,
    stop_buffer_atr: float = 0.10,
    atr_period: int = 14,
) -> dict[str, Any] | None:
    """Return the paired endpoint candle as an aggressive-shift breaker block.

    The book's breaker is on the *left* of the displacement: it is the candle
    that formed the structural endpoint the aggressive MS later closes through.
    It is an alternative mitigation zone, not a replacement for the order block.
    A normal-sized break does not promote that candle to a breaker.
    """
    if len(candles) < 3 or not broke_level_time:
        return None
    shift = candles[-1]
    opened, closed = float(shift["open"]), float(shift["close"])
    directed = closed < opened if side == "SHORT" else closed > opened
    if not directed:
        return None

    prior = candles[max(0, len(candles) - 1 - max(2, atr_period)):len(candles) - 1]
    ranges: list[float] = []
    previous_close: float | None = None
    for bar in prior:
        high, low = float(bar["high"]), float(bar["low"])
        true_range = high - low
        if previous_close is not None:
            true_range = max(true_range, abs(high - previous_close), abs(low - previous_close))
        if true_range > 0:
            ranges.append(true_range)
        previous_close = float(bar["close"])
    atr = sum(ranges) / len(ranges) if ranges else 0.0
    body = abs(closed - opened)
    body_atr = body / atr if atr > 0 else 0.0
    if atr <= 0 or body_atr < max(0.0, min_body_atr):
        return None

    source = next(
        (bar for bar in candles[:-1] if int(bar.get("close_time") or 0) == int(broke_level_time)),
        None,
    )
    if source is None:
        return None
    high, low = float(source["high"]), float(source["low"])
    if high <= low:
        return None
    # The complete zone—not merely its proximal edge—must sit between the
    # displacement close and invalidation stop.
    if side == "SHORT" and not (closed < low < high < float(stop)):
        return None
    if side == "LONG" and not (float(stop) < low < high < closed):
        return None
    buffer = max(0.0, float(stop_buffer_atr)) * atr
    breaker_stop = high + buffer if side == "SHORT" else low - buffer
    # The breaker stop must improve risk versus the structural sweep stop.
    if side == "SHORT" and breaker_stop >= float(stop):
        return None
    if side == "LONG" and breaker_stop <= float(stop):
        return None
    return {
        "high": high,
        "low": low,
        "mid": (high + low) / 2,
        "time": int(source["close_time"]),
        "kind": "bearish_breaker" if side == "SHORT" else "bullish_breaker",
        "type": "breaker_block",
        "body_atr": round(body_atr, 3),
        "atr": atr,
        "stop_buffer": buffer,
        "breaker_stop": breaker_stop,
        "break_time": int(shift["close_time"]),
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
    zones: list[dict[str, Any]] = field(default_factory=list)
    target_rr: float = DEFAULT_TARGET_RR
    lifecycle_version: int = LIFECYCLE_VERSION
    mitigation_kind: str | None = None
    mitigation_zone: dict[str, Any] | None = None
    state_reason: str | None = None
    break_kind: str = "SHIFT"
    hvb_ms: int | None = None
    hvb_side: str | None = None
    volume_ratio: float | None = None
    score: float | None = None
    grade: str | None = None
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
    breakeven_at_r: float = 0.0      # p.109: stop to break-even at +nR (0 = never)
    be_ms: int | None = None         # when the stop was moved
    risk0: float = 0.0               # the risk the setup was born with, for R maths
    stop0: float = 0.0               # the stop as published (the live one can move)

    def __post_init__(self) -> None:
        self.state_since = self.created_ms
        self.target_rr = max(0.1, float(self.target_rr or DEFAULT_TARGET_RR))
        if not self.stop0:
            self.stop0 = self.stop
        if self.zone is None:
            self.zone = {"high": self.entry, "low": self.entry, "mid": self.entry,
                         "kind": "entry", "type": "order_block"}
        if not self.zones:
            self.zones = [self.zone]
        # Risk/reward does not exist until mitigation creates an actual fill.
        # Book plans are born active; sweep plans are born pending.
        if self.state == "active" or self.touched_ms is not None:
            if not self.risk0:
                self.risk0 = abs(self.entry - self.stop)
            if self.risk0 > 0:
                if self.rr is None:
                    self.rr = self.target_rr
                if self.target is None:
                    self.target = (
                        self.entry - self.target_rr * self.risk0 if self.side == "SHORT"
                        else self.entry + self.target_rr * self.risk0
                    )
        elif self.state == "pending":
            self.target = None
            self.rr = None
            self.risk0 = 0.0

    # ------------------------------------------------------------------ view
    @property
    def valid(self) -> bool:
        return self.state in VALID

    @property
    def mitigated(self) -> bool:
        """Has price traded into an order block or eligible breaker block?

        This is the trader's own wording ("it hasn't been mitigated yet") and it
        is tracked separately from the state, so a setup that is old but untouched
        can still be described correctly instead of being called "expired" and
        losing the information that nothing has used it.
        """
        return self.touched_ms is not None

    @property
    def target_rr_price(self) -> float | None:
        """The fixed-R target, available only after a mitigation fill."""
        if not self.mitigated:
            return None
        risk = self.risk0 or abs(self.entry - self.stop0)
        if risk <= 0:
            return None
        if self.side == "SHORT":
            return self.entry - self.target_rr * risk
        return self.entry + self.target_rr * risk

    @property
    def target_2r(self) -> float | None:
        """Legacy 2R reference; never used by the current lifecycle target."""
        if not self.mitigated:
            return None
        risk = self.risk0 or abs(self.entry - self.stop0)
        if risk <= 0:
            return None
        return self.entry - 2 * risk if self.side == "SHORT" else self.entry + 2 * risk

    @property
    def realized_r(self) -> float | None:
        """The R the trade actually produced, once it is over.

        A closed trade has no live mark: quoting one is how a stopped short from
        April ended up on the page at "-28.9R" three months later, because price
        had walked on and the number kept following it. This is the number the
        record is measured on, so it has to be the realised one.
        """
        risk = self.risk0 or abs(self.entry - self.stop0)
        if risk <= 0:
            return None
        if not self.mitigated:
            return None
        if self.state == "win":
            target = self.target if self.target is not None else self.target_rr_price
            if target is None:
                return None
            move = (self.entry - target) if self.side == "SHORT" else (target - self.entry)
            return round(move / risk, 2)
        if self.state == "stopped":
            move = (self.stop0 - self.entry) if self.side == "SHORT" else (self.entry - self.stop0)
            return round(-abs(move) / risk, 2)
        if self.state == "breakeven":
            return 0.0
        return None            # expired: the trade never resolved, it is not an R

    def r_multiple(self, price: float | None = None) -> float | None:
        if not self.mitigated:
            return None
        price = self.last_price if price is None else price
        risk = self.risk0 or abs(self.entry - self.stop)
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
            "stop0": round(self.stop0, 8),
            "risk0": round(self.risk0, 8),
            "target": None if self.target is None else round(self.target, 8),
            "target_2r": None,  # retained only so old CSV consumers do not break
            "target_rr": self.target_rr,
            "target_rr_price": None if self.target_rr_price is None else round(self.target_rr_price, 8),
            "rr": self.rr,
            "r_now": self.realized_r if self.state in DONE else self.r_multiple(),
            "zone": self.zone,
            "zones": self.zones,
            "mitigation_kind": self.mitigation_kind,
            "mitigation_zone": self.mitigation_zone,
            "lifecycle_version": self.lifecycle_version,
            "state_reason": self.state_reason,
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
            "grade": self.grade,
            "score_parts": self.score_parts,
            "depth": self.depth,
            "trend_dir": self.trend_dir,
            "trend_clean": self.trend_clean,
            "reasons": self.reasons,
            "last_price": self.last_price,
            "breakeven_at_r": self.breakeven_at_r or None,
            "be_ms": self.be_ms,
        }

    # --------------------------------------------------------------- lifecycle
    def advance(self, bar: dict, max_bars: int | None = None) -> bool:
        """Advance one closed candle through mitigation, 4R target, and stop.

        No P/L is measured while pending. The first order-block or eligible
        breaker-block touch creates the fill and therefore creates risk and the
        fixed-R target. If one candle contains both stop and target, stop wins.
        """
        if self.state in DONE:
            return False
        high, low = float(bar["high"]), float(bar["low"])
        opened, close = float(bar.get("open", bar["close"])), float(bar["close"])
        at = int(bar["close_time"])
        self.last_price = close
        self.bars += 1
        limit = self.max_bars if max_bars is None else max_bars

        if self.state == "pending":
            touched = [
                zone for zone in self.zones
                if high >= float(zone.get("low", 0)) and low <= float(zone.get("high", 0))
            ]
            beyond_stop = high >= self.stop if self.side == "SHORT" else low <= self.stop
            gapped_stop = opened >= self.stop if self.side == "SHORT" else opened <= self.stop

            if gapped_stop:
                self.state, self.state_since, self.closed_ms = "invalidated", at, at
                self.state_reason = "price opened beyond the stop before mitigation; no trade was filled"
                return True

            if touched:
                # Price approaches a short zone from below and a long zone from
                # above. The proximal edge is the first deterministic fill.
                if self.side == "SHORT":
                    selected = min(touched, key=lambda z: float(z["low"]))
                    fill = float(selected["low"])
                else:
                    selected = max(touched, key=lambda z: float(z["high"]))
                    fill = float(selected["high"])
                if selected.get("type") == "breaker_block" and selected.get("breaker_stop") is not None:
                    self.stop = float(selected["breaker_stop"])
                risk = (self.stop - fill) if self.side == "SHORT" else (fill - self.stop)
                if risk <= 0:
                    self.state, self.state_since, self.closed_ms = "invalidated", at, at
                    self.state_reason = "mitigation zone produced no correctly sided risk to the stop"
                    return True

                self.entry = fill
                self.stop0 = self.stop
                self.risk0 = risk
                self.rr = self.target_rr
                self.target = (
                    fill - self.target_rr * risk if self.side == "SHORT"
                    else fill + self.target_rr * risk
                )
                self.touched_ms = at
                self.mitigation_zone = dict(selected)
                self.mitigation_kind = str(selected.get("type") or selected.get("kind") or "zone")
                self.state, self.state_since = "active", at
                stop_note = " with buffered breaker stop" if self.mitigation_kind == "breaker_block" else ""
                self.state_reason = (
                    f"mitigated at the {self.mitigation_kind}{stop_note}; "
                    f"fixed 1:{self.target_rr:g} target armed"
                )

                hit_stop = high >= self.stop if self.side == "SHORT" else low <= self.stop
                hit_target = low <= self.target if self.side == "SHORT" else high >= self.target
                if hit_stop:
                    self.state, self.closed_ms = "stopped", at
                    self.state_reason = "mitigated and stopped on the same candle (conservative ordering)"
                elif hit_target:
                    self.state, self.closed_ms = "win", at
                    self.state_reason = f"mitigated and reached the 1:{self.target_rr:g} target"
                return True

            if beyond_stop:
                self.state, self.state_since, self.closed_ms = "invalidated", at, at
                kinds = "/".join(sorted({str(z.get("type") or "zone") for z in self.zones}))
                self.state_reason = f"stop was reached before {kinds or 'entry-zone'} mitigation; no trade was filled"
                return True
            if limit > 0 and self.bars >= limit:
                self.state, self.state_since, self.closed_ms = "expired", at, at
                self.state_reason = "the unmitigated setup exceeded its configured bar limit"
                return True
            return False

        # Active means mitigation already happened, so stop/target now count.
        stop_level = self.stop
        hit_target = self.target is not None and (
            low <= self.target if self.side == "SHORT" else high >= self.target
        )
        hit_stop = high >= stop_level if self.side == "SHORT" else low <= stop_level
        if hit_stop:
            self.state = "breakeven" if self.be_ms is not None else "stopped"
            self.state_since = self.closed_ms = at
            self.state_reason = (
                "break-even stop reached after mitigation" if self.be_ms is not None
                else "stop reached after mitigation"
            )
            return True
        if hit_target:
            self.state, self.state_since, self.closed_ms = "win", at, at
            self.state_reason = f"fixed 1:{self.target_rr:g} target reached after mitigation"
            return True

        changed = False
        if self.breakeven_at_r and self.be_ms is None:
            risk = self.risk0 or abs(self.entry - self.stop)
            reached = (low <= self.entry - self.breakeven_at_r * risk if self.side == "SHORT"
                       else high >= self.entry + self.breakeven_at_r * risk)
            if reached:
                self.be_ms = at
                self.stop = self.entry
                self.state_reason = f"stop moved to break-even at {self.breakeven_at_r:g}R"
                changed = True
        if limit > 0 and self.bars >= limit:
            self.state, self.state_since, self.closed_ms = "expired", at, at
            self.state_reason = "the active setup exceeded its configured bar limit"
            changed = True
        return changed
