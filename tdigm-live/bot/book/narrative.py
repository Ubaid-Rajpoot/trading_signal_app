"""The narrative: one symbol, one timeframe, told in the book's order.

    liquidity pool raided and rejected       (trap)          p.11
        -> displacement that leaves an imbalance (AC)         p.67
        -> the imbalance is mitigated                         p.87
        -> structure turns in the trade's direction (-MS)     p.87
        -> stop behind the high-volume imbalance              p.106
        -> draw on liquidity at least min_rr away             p.5

Every stage is a state, every rejection is counted with a reason, and the plan
that comes out carries the whole chain so the page can explain itself. A stage
that does not arrive in time expires the idea instead of leaving it hanging -
"pending forever" signals are the thing this rewrite exists to remove.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .imbalance import Imbalance, algo_candle, candle_atr, premium_discount
from .liquidity import MAJOR, MEDIUM, MINOR, LiquidityMap, Pool, session_of
from .swings import Structure

_GRADE_POINTS = {"major": 3, "medium": 1, "minor": 0}


@dataclass
class BookSettings:
    """Everything the narrative is allowed to change its mind about."""

    entry_tf: str = "15m"
    htf: str = ""                     # "" = pick from the entry timeframe
    widths: dict[str, int] = field(default_factory=lambda: {"minor": 3, "major": 9, "macro": 21})
    atr_period: int = 14

    # the trap
    min_trap_grade: str = MEDIUM      # p.5: minor liquidity is never a trade
    require_rejection: bool = True    # a close *through* the pool is acceptance

    # the displacement
    min_body_atr: float = 1.0         # p.67: the AC has to be a displacement
    vol_mult: float = 1.3             # high-volume imbalance (p.81)
    need_vector: bool = False         # p.71 vector candle (body engulfs the last)
    require_hvi: bool = False         # strict: only high-volume imbalances
    arm_bars: int = 30                # the AC must print this soon after the trap

    # the entry
    entry_mode: str = "mitigation"    # "mitigation" | "shift" | "immediate"
    fill_frac: float = 0.5            # how much of the FVG must be mitigated
    entry_max_bars: int = 40
    min_rr: float = 4.0               # the trader's floor (round 16: 1:4)
    max_rr: float = 100.0             # a 1:500 draw is a broken series
    stop_buffer_atr: float = 0.10
    min_stop_atr: float = 0.4

    # context
    htf_required: bool = True
    premium_discount: bool = False     # p.49: buy discount, sell premium
    sessions: tuple[str, ...] = ()     # empty = all sessions
    target_min_grade: str = "auto"     # auto = medium intraday, major for 1h+
    target_fallback: str = MEDIUM      # second choice when no major pool is left
    max_setups_per_day: int = 0
    fee_pct: float = 0.06              # round trip, % of notional (maker in, taker out)
    max_fee_r: float = 0.20            # refuse a plan the rent would eat

    @classmethod
    def from_config(cls, data: dict[str, Any] | None, **defaults: Any) -> "BookSettings":
        """Build settings from the ``book`` block of ``config.json``."""
        data = dict(data or {})
        fields = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in data.items() if k in fields}
        if isinstance(clean.get("sessions"), list):
            clean["sessions"] = tuple(clean["sessions"])
        clean.update({k: v for k, v in defaults.items() if k in fields and k not in clean})
        return cls(**clean)

    def replace(self, **changes: Any) -> "BookSettings":
        data = {**self.__dict__, **changes}
        return BookSettings(**data)


@dataclass
class Trap:
    """A pool that was raided and rejected: manipulation, not acceptance."""

    side: str            # "high" or "low" - which pool was taken
    level: float
    name: str
    grade: str
    time: int
    index: int           # bar index in the narrative series
    extreme: float       # how far the wick went past the level
    bias: int = 0        # the HTF bias as it stood when the trap printed

    @property
    def direction(self) -> int:
        return -1 if self.side == "high" else 1


@dataclass
class Armed:
    """The trap has a displacement; we are waiting for the market to come back."""

    trap: Trap
    imbalance: Imbalance
    index: int           # bar index of the displacement candle
    ac_high: float
    ac_low: float
    ac_close: float = 0.0
    fill: float = 0.0
    mitigated_index: int | None = None


@dataclass
class Plan:
    """What the page shows and what the trader executes."""

    symbol: str
    timeframe: str
    side: str
    created_ms: int
    entry: float
    stop: float
    target: float | None
    rr: float | None
    trap: dict[str, Any]
    imbalance: dict[str, Any]
    shift: dict[str, Any] | None
    confluences: list[str]
    score: int
    grade: str
    session: str | None
    htf_bias: int
    premium: float | None
    fee_pct: float
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        risk = abs(self.entry - self.stop)
        reward = abs((self.target or self.entry) - self.entry)
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side,
            "time": self.created_ms,
            "created_ms": self.created_ms,
            "price": self.entry,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "rr": None if self.rr is None else round(self.rr, 2),
            "risk": round(risk, 8),
            "reward": round(reward, 8),
            "fee_pct": self.fee_pct,
            "fee_r": round(self.fee_pct / 100 * self.entry / risk, 3) if risk > 0 else None,
            "score": self.score,
            "grade": self.grade,
            "session": self.session,
            "htf_bias": self.htf_bias,
            "premium": self.premium,
            "trap": self.trap,
            "ac": self.imbalance,
            "imbalance": self.imbalance,
            "zone": {"high": self.imbalance.get("top"), "low": self.imbalance.get("bottom"),
                     "mid": self.imbalance.get("mid"), "kind": "fvg"},
            "shift": self.shift,
            "swept_level": self.trap.get("level"),
            "swept_side": self.trap.get("side"),
            "break_kind": (self.shift or {}).get("kind", "MS"),
            "bos_level": (self.shift or {}).get("level"),
            "reasons": list(self.confluences),
            "confluences": list(self.confluences),
            "strategy": "book",
            **self.meta,
        }


class Narrative:
    """One instrument, one entry timeframe."""

    def __init__(self, symbol: str, settings: BookSettings,
                 liquidity: LiquidityMap, structure: Structure | None = None) -> None:
        self.symbol = symbol.upper()
        self.s = settings
        self.liquidity = liquidity
        self.structure = structure or Structure(settings.widths)
        self.bars: list[dict] = []
        self.trap: Trap | None = None
        self.armed: Armed | None = None
        self.setups: list[Plan] = []
        self.drops: Counter[str] = Counter()
        self.events: list[dict[str, Any]] = []   # small ring for the page
        self._sweeps: list[tuple[int, str]] = []  # (index, side) of recent sweeps
        self._day: int | None = None
        self._day_count = 0
        self._htf_bias = 0
        self._premium: float | None = None
        self.warm = False

    # ------------------------------------------------------------------ feed
    def on_bar(self, bar: dict, htf: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Feed one closed entry-timeframe candle. Returns what it changed."""
        htf = htf or {}
        self.set_context(htf)
        self.bars.append(bar)
        if len(self.bars) > 400:
            # indices stay absolute: only the candle ring is trimmed
            self.bars = self.bars[-400:]
        atr = candle_atr(self.bars, self.s.atr_period)
        out: list[dict[str, Any]] = []
        if atr <= 0:
            return out

        events = self.structure.add(bar)
        for pivot in events["sweeps"]:
            self._sweeps.append((self.structure.index, pivot.side))
        if len(self._sweeps) > 64:
            self._sweeps = self._sweeps[-64:]

        self._maybe_trap(bar, events, htf, out)
        if self.trap is not None and self.armed is None:
            self._maybe_arm(bar, atr, out)
        if self.armed is not None:
            self._manage(bar, atr, events, out)
        self._expire(bar, out)

        if out:
            self.events.extend(out)
            if len(self.events) > 200:
                self.events = self.events[-200:]
        return out

    # ------------------------------------------------------------------ trap
    def _maybe_trap(self, bar: dict, events: dict[str, list], htf: dict[str, Any],
                    out: list[dict[str, Any]]) -> None:
        if self.trap is not None:
            return
        opened = int(bar.get("open_time") or 0)
        closed = int(bar.get("close_time") or opened)
        close = float(bar["close"])
        high, low = float(bar["high"]), float(bar["low"])
        time = int(bar.get("close_time") or opened)
        index = self.structure.index

        candidates: list[tuple[str, float, str, str, float]] = []
        rank = {MAJOR: 3, MEDIUM: 2, MINOR: 1}
        floor = rank.get(self.s.min_trap_grade, 2)
        for pool in self.liquidity.pools:
            if pool.swept_ms is None or not (opened < pool.swept_ms <= closed):
                continue
            if rank.get(pool.grade, 1) < floor:
                continue
            if self.s.require_rejection:
                if pool.side == "high" and close > pool.price:
                    self._count("the pool was accepted, not raided")
                    continue
                if pool.side == "low" and close < pool.price:
                    self._count("the pool was accepted, not raided")
                    continue
            extreme = high if pool.side == "high" else low
            candidates.append((pool.side, pool.price, pool.name, pool.grade, extreme))

        # p.11: a major swing is liquidity too, even if no PDH/PWH sits there
        for sweep in events["sweeps"]:
            if sweep.degree not in ("major", "macro"):
                continue
            if self.s.require_rejection:
                if sweep.side == "high" and close > sweep.price:
                    continue
                if sweep.side == "low" and close < sweep.price:
                    continue
            extreme = high if sweep.side == "high" else low
            candidates.append((sweep.side, sweep.price, f"{sweep.degree} swing",
                               MEDIUM if sweep.degree == "major" else MAJOR, extreme))

        if not candidates:
            return
        side, level, name, grade, extreme = max(candidates, key=lambda c: rank.get(c[3], 0))
        direction = -1 if side == "high" else 1

        bias = int(htf.get("bias") or 0)
        if self.s.htf_required and bias and direction != bias:
            self._count("against the higher-timeframe direction")
            return
        session = session_of(time)
        if self.s.sessions and session not in self.s.sessions:
            self._count("outside the allowed sessions")
            return

        self.trap = Trap(side=side, level=level, name=name, grade=grade,
                         time=time, index=index, extreme=extreme, bias=bias)
        self.armed = None
        out.append({"type": "trap", "trap": self.trap, "bar": bar})

    # ------------------------------------------------------------------- arm
    def _maybe_arm(self, bar: dict, atr: float, out: list[dict[str, Any]]) -> None:
        trap = self.trap
        assert trap is not None
        index = self.structure.index
        if index - trap.index > self.s.arm_bars:
            return
        # the candidate displacement is the bar before this one; this bar confirms it
        candidate = len(self.bars) - 2
        if candidate < 1:
            return
        found = algo_candle(self.bars, candidate, atr=atr,
                            min_body_atr=self.s.min_body_atr,
                            vol_mult=self.s.vol_mult,
                            need_vector=self.s.need_vector)
        if found is None:
            return
        want = "bear" if trap.direction < 0 else "bull"
        if found.kind != want:
            return
        if self.s.require_hvi and not found.high_volume:
            self._count("the displacement carried no volume")
            return
        ac = self.bars[candidate]
        self.armed = Armed(trap=trap, imbalance=found, index=index - 1,
                           ac_high=float(ac["high"]), ac_low=float(ac["low"]),
                           ac_close=float(ac["close"]))
        out.append({"type": "armed", "armed": self.armed, "bar": ac})

    # --------------------------------------------------------------- manage
    def _manage(self, bar: dict, atr: float, events: dict[str, list],
                out: list[dict[str, Any]]) -> None:
        armed = self.armed
        assert armed is not None
        index = self.structure.index
        if index <= armed.index + 1:
            return            # the confirming candle cannot be the entry candle

        gap = armed.imbalance
        high, low = float(bar["high"]), float(bar["low"])
        if gap.kind == "bull":
            armed.fill = max(armed.fill, min(1.0, max(0.0, (gap.top - low) / gap.height)))
        else:
            armed.fill = max(armed.fill, min(1.0, max(0.0, (high - gap.bottom) / gap.height)))

        if armed.fill >= self.s.fill_frac and armed.mitigated_index is None:
            armed.mitigated_index = index
            out.append({"type": "mitigated", "armed": armed, "bar": bar})
            if self.s.entry_mode == "mitigation":
                price = gap.bottom + self.s.fill_frac * gap.height
                self._publish(price, bar, atr, events, out, shift=None)
                return

        if self.s.entry_mode == "immediate" and index == armed.index + 1:
            self._publish(float(armed.ac_close), bar, atr, events, out, shift=None)
            return

        if self.s.entry_mode == "shift":
            if armed.mitigated_index is None:
                return
            found = self._shift_since(armed.mitigated_index, armed.trap.direction)
            if found is None:
                return
            self._publish(float(bar["close"]), bar, atr, events, out, shift=found)

    def _shift_since(self, since: int, direction: int) -> dict[str, Any] | None:
        """A break of structure in the trade's direction, after the mitigation."""
        for event in reversed(self.structure.breaks):
            if event.index <= since:
                return None
            if event.direction == direction and event.degree in ("minor", "major", "macro"):
                return event.as_dict()
        return None

    # ------------------------------------------------------------------ plan
    def _publish(self, entry: float, bar: dict, atr: float, events: dict[str, list],
                 out: list[dict[str, Any]], shift: dict[str, Any] | None) -> None:
        armed = self.armed
        assert armed is not None
        trap, gap = armed.trap, armed.imbalance
        direction = trap.direction

        # ---- stop: behind the high-volume imbalance (p.106)
        buffer = atr * self.s.stop_buffer_atr
        if direction < 0:
            stop = max(gap.top, armed.ac_high) + buffer
        else:
            stop = min(gap.bottom, armed.ac_low) - buffer
        if (direction < 0 and stop <= entry) or (direction > 0 and stop >= entry):
            self._drop("the stop would sit on the wrong side of the entry", gap)
            return
        risk = abs(entry - stop)
        if risk < self.s.min_stop_atr * atr:
            self._drop("the stop is closer than any sane noise", gap)
            return

        # ---- target: the draw on liquidity, major first (p.5)
        pool = self.liquidity.draw(direction, entry, self.s.target_min_grade)
        if pool is None and self.s.target_fallback:
            pool = self.liquidity.draw(direction, entry, self.s.target_fallback)
        if pool is None:
            self._drop("no untouched liquidity left in front of the trade", gap)
            return
        target = float(pool.price)
        reward = (target - entry) * direction
        if reward <= 0:
            self._drop("the draw sits behind the entry", gap)
            return
        fee_r = self.s.fee_pct / 100 * entry / risk
        if self.s.max_fee_r and fee_r > self.s.max_fee_r:
            # a 0.03% stop pays 2R of fees: the trade is lost before it starts
            self._drop("the fees would eat the edge", gap)
            return
        rr = reward / risk
        if rr < self.s.min_rr:
            self._drop("the draw is closer than the minimum reward:risk", gap)
            return
        if rr > self.s.max_rr:
            self._drop("the draw is absurdly far - the series looks wrong", gap)
            return

        # ---- context, recorded rather than silently enforced
        bias = int(trap.bias)      # the bias the idea was born with, not today's
        premium = self.bar_premium
        if self.s.premium_discount and premium is not None:
            want = "premium" if direction < 0 else "discount"
            side = "premium" if float(premium) >= 0.5 else "discount"
            if side != want:
                self._drop("entry on the wrong side of the dealing range", gap)
                return
        session = session_of(int(bar.get("close_time") or 0))
        if self.s.sessions and session not in self.s.sessions:
            self._drop("outside the allowed sessions", gap)
            return
        day = int(bar.get("close_time") or 0) // 86_400_000
        if day != self._day:
            self._day, self._day_count = day, 0
        if self.s.max_setups_per_day and self._day_count >= self.s.max_setups_per_day:
            self._drop("daily setup cap", gap)
            return

        confluences, score = self._story(armed, entry, stop, target, pool, shift,
                                         session, bias, premium)
        grade = "A" if score >= 6 else "B" if score >= 4 else "C"
        plan = Plan(
            symbol=self.symbol,
            timeframe=self.s.entry_tf,
            side="LONG" if direction > 0 else "SHORT",
            created_ms=int(bar.get("close_time") or 0),
            entry=entry,
            stop=stop,
            target=target,
            rr=rr,
            trap={"side": trap.side, "level": trap.level, "name": trap.name,
                  "grade": trap.grade, "time": trap.time, "extreme": trap.extreme,
                  "bias": trap.bias},
            imbalance=gap.as_dict(),
            shift=shift,
            confluences=confluences,
            score=score,
            grade=grade,
            session=session,
            htf_bias=bias,
            premium=premium,
            fee_pct=self.s.fee_pct,
        )
        self._day_count += 1
        self.setups.append(plan)
        out.append({"type": "setup", "setup": plan, "bar": bar})
        self.trap, self.armed = None, None

    def _story(self, armed: Armed, entry: float, stop: float, target: float, pool: Pool,
               shift: dict[str, Any] | None, session: str | None, bias: int,
               premium: float | None) -> tuple[list[str], int]:
        trap, gap = armed.trap, armed.imbalance
        direction = trap.direction
        lines: list[str] = []
        score = 0

        lines.append(f"swept {trap.name} {trap.level:,.0f} ({trap.grade})")
        score += _GRADE_POINTS.get(trap.grade, 0)
        if bias:
            word = "bullish" if bias > 0 else "bearish"
            agrees = bias == direction
            lines.append(f"{self.s.htf} bias {word}" + ("" if agrees else " (taken against it)"))
            if agrees:
                score += 2
        else:
            lines.append(f"no {self.s.htf} bias yet")
        body_atr = gap.body_atr
        words = f"{gap.kind}ish displacement, body {body_atr:.1f}xATR"
        if gap.vector:
            words += " and engulfing"
        lines.append(f"{words}, FVG {gap.bottom:,.1f}-{gap.top:,.1f}")
        score += 1
        if gap.high_volume:
            lines.append(f"high-volume imbalance {gap.volume_ratio:.1f}x -> stop behind it")
            score += 2
        if armed.fill >= 1.0:
            lines.append("FVG fully mitigated")
        elif armed.mitigated_index is not None:
            lines.append(f"FVG {armed.fill * 100:.0f}% mitigated")
        score += 1
        inducement = self._inducement(armed)
        if inducement:
            lines.append(f"inducement: the {trap.side} at {inducement:,.1f} was taken first")
            score += 1
        if shift:
            lines.append(f"{shift.get('kind', 'MS')} {'up' if shift['direction'] > 0 else 'down'} "
                         f"through {float(shift['level']):,.1f}")
            score += 1
        if premium is not None:
            where = "premium" if premium >= 0.5 else "discount"
            lines.append(f"entry in the {where} of the dealing range ({premium:.2f})")
            if (direction < 0 and premium >= 0.5) or (direction > 0 and premium < 0.5):
                score += 1
        failure = self.structure.failure_swing("major")
        if failure == direction:
            lines.append("failure swing: the last move could not extend")
            score += 1
        if session:
            lines.append(f"{session} session")
            if session in ("london", "ny"):
                score += 1
        risk = abs(entry - stop)
        if risk > 0:
            lines.append(f"fee {self.s.fee_pct / 100 * entry / risk:.2f}R of the risk")
        lines.append(f"draw {pool.name} {target:,.1f} ({pool.grade}), 1:{abs(target - entry) / abs(entry - stop):.1f}")
        return lines, score

    def _inducement(self, armed: Armed) -> float | None:
        """p.15: liquidity close to the order block, taken just before the move."""
        trap = armed.trap
        for index, side in reversed(self._sweeps):
            if index <= trap.index:
                return None
            if index > armed.index:
                continue
            if side == trap.side:
                pivot = self.structure.degrees["minor"].latest(side)
                if pivot is not None and pivot.price < trap.extreme:
                    return pivot.price
        return None

    # --------------------------------------------------------------- expiry
    def _expire(self, bar: dict, out: list[dict[str, Any]]) -> None:
        index = self.structure.index
        if self.trap is not None and self.armed is None and index - self.trap.index > self.s.arm_bars:
            self._count("no displacement while the trap was fresh")
            out.append({"type": "dropped", "reason": "no displacement", "trap": self.trap})
            self.trap = None
        if self.armed is not None and index - self.armed.index > self.s.entry_max_bars:
            self._count("the setup expired without an entry")
            out.append({"type": "dropped", "reason": "expired", "armed": self.armed})
            self.trap, self.armed = None, None

    # ---------------------------------------------------------------- misc
    def _count(self, reason: str) -> None:
        self.drops[reason] += 1

    def _drop(self, reason: str, gap: Imbalance | None = None) -> None:
        self._count(reason)

    @property
    def bar_htf_bias(self) -> int:
        return int(getattr(self, "_htf_bias", 0))

    @property
    def bar_premium(self) -> float | None:
        return getattr(self, "_premium", None)

    def set_context(self, htf: dict[str, Any]) -> None:
        self._htf_bias = int(htf.get("bias") or 0)
        self._premium = htf.get("premium")

    def state(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.s.entry_tf,
            "trap": None if self.trap is None else {
                "side": self.trap.side, "level": self.trap.level, "name": self.trap.name,
                "grade": self.trap.grade, "time": self.trap.time,
            },
            "armed": None if self.armed is None else {
                "kind": self.armed.imbalance.kind,
                "bottom": self.armed.imbalance.bottom,
                "top": self.armed.imbalance.top,
                "fill": round(self.armed.fill, 2),
                "time": self.armed.imbalance.time,
            },
            "setups": len(self.setups),
            "drops": dict(self.drops),
        }
