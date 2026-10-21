"""The gate every alert has to get through.

An alert is a raw event. A *signal* is an event that also survived the trader's
filters. Nothing here invents a strategy - each filter either switches a
condition on/off or narrows the same condition:

    structure        the sweep -> opposite-side break (the drawing). On by default.
    hvb              the TDIGM price line crosses the CENTRE band on the signal bar
    hvb_min_points   and how far past the centre it closed, in RSI points
    hvb_min_speed    and how fast: this candle's step vs the line's average step
    regime           only trade a trending market (ADX + efficiency ratio)
    regime_direction "any" or "with" (the EMA20/EMA50 direction)
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
    "hvb": True,
    "hvb_min_points": 1.0,
    "hvb_min_speed": 1.5,
    "regime": True,
    "regime_adx_min": 20.0,
    "regime_er_min": 0.10,
    "regime_direction": "any",
    "break": False,
    "sides": ["long", "short"],
    "min_rr": 0.0,
    "max_bars_since_sweep": 0,
    "trend": "off",
    "trend_ema": 50,
    "sessions": [],
    "cooldown_minutes": 0,
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

        if cfg.get("structure", True) and not candidate.get("structure"):
            reasons.append("no structure (needs a sweep, then the opposite side breaking)")

        if cfg.get("hvb", True):
            need = float(cfg.get("hvb_min_points") or 0)
            need_speed = float(cfg.get("hvb_min_speed") or 0)
            if candidate.get("hvb"):
                # the engine already applies both gates; a filter edited from the
                # page has to bite on the spot too, so re-check the numbers
                power = abs(float(candidate.get("hvb_points") or 0.0))
                speed = candidate.get("hvb_speed")
                if need > 0 and power < need:
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

        side = str(candidate.get("side") or "").lower()

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

        if cfg.get("break"):
            if not candidate.get("break"):
                reasons.append("no 20-bar high/low break on this candle")

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
