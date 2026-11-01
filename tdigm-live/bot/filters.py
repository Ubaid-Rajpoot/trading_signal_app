"""The gate every alert has to get through.

An alert is a raw event. A *signal* is an event that also survived the trader's
filters. Nothing here invents a strategy - each filter either switches a
condition on/off or narrows the same condition:

    structure        the sweep -> opposite-side break (the drawing). On by default.
    trend_context    the external structure must agree: higher highs + higher lows
                     for a long, lower highs + lower lows for a short (bot/trend.py).
                     OFF by default since round 19: on real data it removed a
                     third of the signals and did not improve R per trade
                     (docs/trend-rules.md). The evidence is still on every card,
                     and it is what makes a setup grade A instead of B.
    require_hh_hl    demand the clean swing sequence, not just the last break
    require_bos      a break of STRUCTURE in the trade direction, not older than
                     bos_max_age bars (a close through a swing, never a wick)
    max_internal_depth  how deep inside internal structure a setup may sit:
                     1 = external only, 2 = one level of internal (default),
                     3+ = "internal within internal" and is skipped
    volume_break     the signal candle must be a high-volume bar (volume_mult x
                     the average of the previous volume_period candles)
    confirm_mode     "all" = every switched-on condition must hold;
                     "score" = the weighted confluence score must reach
                     confirm_min_score (bos + hvb + volume + fvg + trend + depth)
    hvb_window       the HVB may print on the break candle or up to N bars after
                     it (the TDI cross and the structure break are two different
                     measurements; demanding the same candle was the single
                     biggest reason a real setup never appeared)
    hvb              the TDIGM price line crosses the CENTRE band on the signal bar
    hvb_min_points   and how far past the centre it closed, in RSI points
    hvb_min_speed    and how fast: this candle's step vs the line's average step
    regime           only trade a trending market (ADX + efficiency ratio)
    regime_direction "any" or "with" (the EMA20/EMA50 direction)
    premium_discount  p.42 - the manipulation (sweep) must happen in premium
                      for a short / in discount for a long, unless
                      premium_on_entry is set (then the entry is measured)
    structure_direction  the last MAJOR break has to agree with the trade (p.53)
    require_ms       the confirming break must be a major swing (MS, not SHIFT)
    require_fvg      the displacement must have left an FVG (p.87)
    block_liquidity_pool  no trade aimed straight into a cluster of equal highs/lows
    structure_atr_min / _max  the swept->broken structure has to be this tall (ATR)
    break            the classic 20-bar high/low close break, same bar
    sides            long only / short only / both
    min_rr           skip setups whose next-liquidity target pays less than this
    max_bars_since_sweep  skip a reversal that took too long to happen
    trend            "off", "with" (only trade in the EMA direction) or
                     "against" (only the reversal kind)
    sessions         UTC windows the bot is allowed to shout in
    cooldown         minutes to keep quiet for on that symbol+timeframe

Every rejection is counted and returned, so the page can say *why* it is quiet
instead of looking broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_FILTERS: dict[str, Any] = {
    "structure": True,
    # --- the trader's trend / nesting rules (round 17) ---
    "trend_context": False,
    "require_hh_hl": False,
    "require_bos": True,
    "bos_max_age": 240,
    "max_internal_depth": 2,
    # 0 = an untouched setup remains valid until price invalidates it.  This is
    # essential for historical discovery: age alone is not mitigation.
    "setup_max_bars": 0,
    "volume_break": False,
    "volume_mult": 1.5,
    "volume_period": 20,
    "confirm_mode": "all",
    "confirm_min_score": 60,
    "hvb": True,
    # strict strategy rule: HVB and body break belong to the same closed candle
    "hvb_window": 0,
    "hvb_min_points": 1.0,
    "hvb_min_speed": 1.5,
    "regime": True,
    "regime_adx_min": 20.0,
    "regime_er_min": 0.10,
    "regime_direction": "any",
    "premium_discount": False,
    "premium_level": 0.5,
    "premium_on_entry": False,
    "structure_direction": True,
    "require_ms": False,
    "require_fvg": False,
    # optional book confluence, not part of the trader's TDIGM checklist
    "block_liquidity_pool": False,
    "pool_tolerance": 0.25,
    "pool_count": 3,
    "structure_atr_min": 0.0,
    "structure_atr_max": 0.0,
    "break": False,
    "sides": ["long", "short"],
    "min_rr": 0.0,
    "max_bars_since_sweep": 0,
    "trend": "off",
    "trend_ema": 50,
    "sessions": [],
    "cooldown_minutes": 0,
}


# What the confluence score is made of when ``confirm_mode = "score"``. The
# weights add up to 100 and express how much each piece of evidence is worth in
# the trader's own reading of the setup: the trend it sits in and the TDIGM break
# are the two big ones, the volume and the imbalance confirm the displacement,
# and being at external rather than nested depth is a small bonus.
SCORE_WEIGHTS: dict[str, int] = {
    "trend": 25,
    "hvb": 25,
    "bos": 20,
    "volume": 15,
    "fvg": 10,
    "depth": 5,
}


def parse_sessions(items: list[str]) -> list[tuple[int, int]]:
    """``["07:00-11:00", "13:00-21:00"]`` -> minute ranges (UTC)."""
    out: list[tuple[int, int]] = []
    for item in items or []:
        try:
            start, end = str(item).split("-")
            sh, sm = (int(x) for x in start.split(":"))
            eh, em = (int(x) for x in end.split(":"))
        except Exception:
            continue
        begin, finish = sh * 60 + sm, eh * 60 + em
        if 0 <= begin < 1440 and 0 <= finish <= 1440 and begin != finish:
            out.append((begin, finish))
    return out


@dataclass
class Filters:
    config: dict[str, Any] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    passed: int = 0

    def __post_init__(self) -> None:
        cfg = {**DEFAULT_FILTERS, **(self.config or {})}
        self.config = cfg
        self.sessions = parse_sessions(cfg.get("sessions") or [])
        self.sides = {s.lower() for s in (cfg.get("sides") or ["long", "short"])}

    # ---------------------------------------------------------------- checks
    def apply(
        self,
        candidate: dict,
        *,
        trend: float | None = None,
        minute_of_day: int = 0,
        regime: dict | None = None,
    ) -> tuple[bool, list[str]]:
        """``candidate`` carries the raw event; returns (ok, failed reasons)."""
        cfg = self.config
        reasons: list[str] = []

        context = candidate.get("context") or {}
        depth = candidate.get("depth") or {}
        side = str(candidate.get("side") or "").lower()
        want = 1 if side == "long" else -1
        score_mode = str(cfg.get("confirm_mode") or "all").lower() == "score"

        if cfg.get("trend_context", True) and side:
            external = context.get("external") or {}
            direction = external.get("direction")
            if direction is None:
                reasons.append("no clear external trend (swings are not making higher highs + higher lows or the mirror)")
            elif direction * want < 0:
                which = "higher highs + higher lows" if direction > 0 else "lower highs + lower lows"
                reasons.append(f"against the external trend ({which})")
            elif cfg.get("require_hh_hl", True) and not external.get("clean"):
                reasons.append("external structure is not a clean HH+HL / LH+LL sequence")

        if cfg.get("require_bos", True) and side:
            external = context.get("external") or {}
            bos_direction = external.get("bos_direction")
            age = external.get("bos_age")
            limit = int(cfg.get("bos_max_age") or 0)
            if bos_direction is None:
                reasons.append("no break of structure yet in this trend")
            elif bos_direction * want < 0:
                reasons.append("the last break of structure is the other way")
            elif limit > 0 and age is not None and age > limit:
                reasons.append(f"the last break of structure was {age} bars ago (max {limit})")

        if side and depth.get("value") is not None:
            allowed = int(cfg.get("max_internal_depth") or 2)
            if depth["value"] > allowed:
                reasons.append(
                    f"{depth.get('label')} (depth {depth['value']}, max {allowed}): the setup is "
                    f"nested too deep inside the internal structure"
                )

        if cfg.get("volume_break") and side:
            ratio = candidate.get("volume_ratio")
            need = float(cfg.get("volume_mult") or 0)
            if ratio is None:
                reasons.append("volume not known yet")
            elif need > 0 and ratio < need:
                reasons.append(f"no high-volume bar ({ratio:.1f}x the average, needs {need:g}x)")

        if cfg.get("structure", True) and not candidate.get("structure"):
            reasons.append("no structure (needs a sweep, then the opposite side breaking)")

        if cfg.get("hvb", True) and not score_mode:
            need = float(cfg.get("hvb_min_points") or 0)
            need_speed = float(cfg.get("hvb_min_speed") or 0)
            if candidate.get("hvb"):
                # Direction is part of confluence, not just the existence of a
                # cross.  A bearish structure break with an upward TDI cross was
                # previously accepted because any non-empty ``hvb`` was truthy.
                expected = "up" if side == "long" else "down"
                actual = str(candidate.get("hvb") or "").lower()
                power = abs(float(candidate.get("hvb_points") or 0.0))
                speed = candidate.get("hvb_speed")
                if side and actual != expected:
                    reasons.append(f"HVB direction {actual} opposes the {side} setup (needs {expected})")
                elif need > 0 and power < need:
                    reasons.append(f"HVB broke the centre by only {power:.2f} points (needs {need:g})")
                elif need_speed > 0 and speed is not None and speed < need_speed:
                    reasons.append(f"HVB too slow ({speed:.1f}x its average step, needs {need_speed:g}x)")
            else:
                power = abs(float(candidate.get("hvb_points") or 0.0))
                speed = candidate.get("hvb_speed")
                if candidate.get("centre_cross") and need > 0 and power < need:
                    reasons.append(f"HVB broke the centre by only {power:.2f} points (needs {need:g})")
                elif candidate.get("centre_cross") and need_speed > 0 and speed is not None and speed < need_speed:
                    reasons.append(f"HVB too slow ({speed:.1f}x its average step, needs {need_speed:g}x)")
                else:
                    reasons.append("no HVB: the price line did not cross the centre band")

        if cfg.get("regime", True) and regime:
            adx = regime.get("adx")
            er = regime.get("er")
            state = regime.get("state")
            adx_min = float(cfg.get("regime_adx_min") or 0)
            er_min = float(cfg.get("regime_er_min") or 0)
            if state is None:
                reasons.append("market state not known yet (ADX/ER still warming up)")
            elif state != "trend":
                if adx is not None and adx < adx_min:
                    reasons.append(f"ranging market: ADX {adx:.1f} < {adx_min:g}")
                elif er is not None and er < er_min:
                    reasons.append(f"ranging market: ER {er:.2f} < {er_min:g}")
                else:
                    reasons.append("ranging market (ADX and ER below the trend thresholds)")
            elif str(cfg.get("regime_direction") or "any").lower() == "with" and side:
                want = 1 if side == "long" else -1
                if regime.get("direction") is not None and regime["direction"] * want < 0:
                    reasons.append("against the EMA trend (regime_direction = with)")

        book = candidate.get("book") or {}
        if book:
            reasons.extend(self._book_checks(side, cfg, book, candidate))

        if cfg.get("break"):
            if not candidate.get("break"):
                reasons.append("no 20-bar high/low break on this candle")

        if score_mode and side:
            score, parts = self.score_of(candidate)
            need = float(cfg.get("confirm_min_score") or 0)
            if score < need:
                have = ", ".join(f"{name} {'yes' if hit else 'no'}" for name, hit in parts.items())
                reasons.append(f"confluence score {score:.0f} < {need:g} ({have})")

        if side and side not in self.sides:
            reasons.append(f"{side}s are switched off")

        min_rr = float(cfg.get("min_rr") or 0)
        if min_rr > 0:
            rr = candidate.get("rr")
            if rr is None:
                reasons.append(f"no next liquidity level (needs RR >= {min_rr:g})")
            elif float(rr) < min_rr:
                reasons.append(f"RR {float(rr):.2f} < {min_rr:g}")

        max_wait = int(cfg.get("max_bars_since_sweep") or 0)
        if max_wait > 0:
            bars = candidate.get("bars_since_sweep")
            if bars is None or int(bars) > max_wait:
                reasons.append(f"reversal came {bars} bars after the sweep (max {max_wait})")

        mode = str(cfg.get("trend") or "off").lower()
        if mode in {"with", "against"} and trend is not None and side:
            want = 1 if (mode == "with" and side == "long") or (mode == "against" and side == "short") else -1
            if trend * want <= 0:
                label = "with" if mode == "with" else "against"
                reasons.append(f"not {label} the trend (EMA{int(cfg.get('trend_ema', 50))})")

        if self.sessions:
            minute = minute_of_day % 1440
            inside = any(
                (begin <= minute < finish) if begin < finish else (minute >= begin or minute < finish)
                for begin, finish in self.sessions
            )
            if not inside:
                reasons.append("outside the allowed session (UTC)")

        if reasons:
            for reason in reasons:
                key = _short(reason)
                self.rejections[key] = self.rejections.get(key, 0) + 1
            return False, reasons

        self.passed += 1
        return True, []

    @staticmethod
    def score_of(candidate: dict) -> tuple[float, dict[str, bool]]:
        """The weighted confluence score, and which parts of it were present.

        Used by ``confirm_mode = "score"`` and shown on every card, so the trader
        can see *why* one setup outranks another instead of comparing bands.
        """
        context = candidate.get("context") or {}
        external = context.get("external") or {}
        book = candidate.get("book") or {}
        depth = candidate.get("depth") or {}
        side = str(candidate.get("side") or "").lower()
        want = 1 if side == "long" else -1
        ratio = candidate.get("volume_ratio")
        parts = {
            "trend": bool(external.get("direction") is not None and external["direction"] * want > 0
                          and external.get("clean")),
            "hvb": bool(
                candidate.get("hvb")
                and str(candidate.get("hvb")).lower() == ("up" if side == "long" else "down")
            ),
            "bos": bool(external.get("bos_direction") is not None
                        and external["bos_direction"] * want > 0),
            "volume": bool(ratio is not None and float(ratio) >= 1.5),
            "fvg": bool(book.get("fvgs_in_leg")),
            "depth": bool(depth.get("value") is not None and depth["value"] <= 2),
        }
        score = float(sum(SCORE_WEIGHTS[name] for name, hit in parts.items() if hit))
        return score, parts

    @staticmethod
    def _book_checks(side: str, cfg: dict, book: dict, candidate: dict) -> list[str]:
        """The book's confluences, each one switchable on its own."""
        out: list[str] = []

        # p.42 premium / discount
        if cfg.get("premium_discount") and side:
            threshold = float(cfg.get("premium_level") or 0.5)
            on_entry = bool(cfg.get("premium_on_entry"))
            zone = book.get("zone") if on_entry else book.get("sweep_zone")
            position = book.get("position") if on_entry else book.get("sweep_position")
            if zone is None:
                pass  # nothing to judge against yet - do not invent a rejection
            elif side == "short" and position is not None and position < threshold:
                out.append(f"short not in premium ({position * 100:.0f}% of the dealing range)")
            elif side == "long" and position is not None and (1 - position) < threshold:
                out.append(f"long not in discount ({(1 - position) * 100:.0f}% below the top)")

        # p.53 external structure direction
        if cfg.get("structure_direction") and side:
            trend = book.get("external_trend")
            want = 1 if side == "long" else -1
            if trend is None:
                pass  # not enough major structure yet: do not invent a rejection
            elif trend * want < 0:
                out.append("against the external structure (last major break)")

        if cfg.get("require_ms") and book.get("break_kind") != "MS":
            out.append(f"only a SHIFT, not an MS (break kind: {book.get('break_kind') or 'none'})")

        if cfg.get("require_fvg") and not book.get("fvgs_in_leg"):
            out.append("the displacement left no FVG to trade from")

        if cfg.get("block_liquidity_pool") and side:
            need = int(cfg.get("pool_count") or 3)
            pools = int(book.get("pools_ahead") or 0)
            if pools >= need:
                where = "highs" if side == "long" else "lows"
                out.append(f"{pools} equal {where} in front of the trade (a liquidity pool)")

        low = float(cfg.get("structure_atr_min") or 0)
        high = float(cfg.get("structure_atr_max") or 0)
        height = book.get("structure_atr")
        if low > 0 or high > 0:
            if height is None:
                out.append("structure height unknown (ATR not ready)")
            elif low > 0 and height < low:
                out.append(f"structure only {height:.2f} ATR tall (needs {low:g})")
            elif high > 0 and height > high:
                out.append(f"structure {height:.2f} ATR tall (wider than {high:g})")
        return out

    # ------------------------------------------------------------ reporting
    def summary(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "sessions": [f"{b // 60:02d}:{b % 60:02d}-{e // 60:02d}:{e % 60:02d}" for b, e in self.sessions],
            "passed": self.passed,
            "rejected": sum(self.rejections.values()),
            "reasons": dict(sorted(self.rejections.items(), key=lambda kv: -kv[1])[:6]),
        }


def _short(reason: str) -> str:
    """Stable key for the counters - drop the numbers so they group."""
    head = reason.split("(")[0].strip()
    out = []
    for token in head.split():
        # the numbers differ every bar; "N" keeps one counter per kind of reason
        out.append("N" if any(ch.isdigit() for ch in token) and not token.startswith("20") else token)
    return " ".join(out)[:48]
