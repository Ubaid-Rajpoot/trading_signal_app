"""Signal engine: turns raw candles + a strategy into an actionable snapshot.

Produces, for the most recent **closed** bar:
  * the current strategy state (flat / long / short) and where it came from
  * a recommended action with confidence and a full rule breakdown
  * an entry / stop / take-profit plan from the strategy's own risk rules
  * a compact chart payload the frontend can render directly
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np
import pandas as pd

from ..config import TIMEFRAMES
from . import market_structure as ms
from .data.providers import MarketDataError, get_candles
from .strategies import Strategy, get_strategy
from .strategies.base import Targets


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _state_at_last_bar(data: pd.DataFrame, triggers) -> dict[str, Any]:
    """Replay triggers to find the currently open strategy position."""
    state = "FLAT"
    entry_index: int | None = None
    le, lx = triggers.long_entry.to_numpy(), triggers.long_exit.to_numpy()
    se, sx = triggers.short_entry.to_numpy(), triggers.short_exit.to_numpy()
    for i in range(len(data)):
        if state == "FLAT":
            if le[i] and not se[i]:
                state, entry_index = "LONG", i
            elif se[i] and not le[i]:
                state, entry_index = "SHORT", i
        elif state == "LONG" and lx[i]:
            state, entry_index = "FLAT", None
        elif state == "SHORT" and sx[i]:
            state, entry_index = "FLAT", None
    out: dict[str, Any] = {"state": state, "since": None, "entry_price": None, "bars_in_state": None}
    if entry_index is not None:
        out["since"] = int(data["time"].iloc[entry_index])
        out["entry_price"] = _f(data["close"].iloc[entry_index])
        out["bars_in_state"] = int(len(data) - 1 - entry_index)
    return out


def _confidence(data: pd.DataFrame, side: str) -> tuple[int, list[str]]:
    """Heuristic 0-100 conviction score plus the drivers behind it."""
    row = data.iloc[-1]
    price = _f(row["close"], 0.0) or 0.0
    notes: list[str] = []
    score = 50.0

    adx = _f(row.get("adx"))
    if adx is not None:
        if adx >= 30:
            score += 18
            notes.append(f"Strong trend (ADX {adx:.1f})")
        elif adx >= 22:
            score += 10
            notes.append(f"Developing trend (ADX {adx:.1f})")
        elif adx < 15:
            score -= 12
            notes.append(f"Choppy conditions (ADX {adx:.1f})")

    ema = _f(row.get("trend_ema")) or _f(row.get("slow_ema"))
    if ema and price:
        stretch = (price / ema - 1) * 100
        aligned = stretch > 0 if side in ("LONG", "BUY") else stretch < 0
        score += max(-12, min(14, abs(stretch) * 2.5)) * (1 if aligned else -0.7)
        notes.append(f"Price {stretch:+.2f}% vs trend EMA")

    rsi = _f(row.get("rsi"))
    if rsi is not None:
        if side == "LONG" and 45 <= rsi <= 68:
            score += 8
            notes.append(f"RSI supportive ({rsi:.0f})")
        elif side == "SHORT" and 32 <= rsi <= 55:
            score += 8
            notes.append(f"RSI supportive ({rsi:.0f})")
        elif side == "LONG" and rsi > 78:
            score -= 10
            notes.append(f"RSI overbought ({rsi:.0f})")
        elif side == "SHORT" and rsi < 22:
            score -= 10
            notes.append(f"RSI oversold ({rsi:.0f})")

    vol_ratio = _f(row.get("volume_ratio"))
    if vol_ratio is not None:
        if vol_ratio >= 1.5:
            score += 6
            notes.append(f"Volume expansion ({vol_ratio:.1f}x avg)")
        elif vol_ratio < 0.7:
            score -= 4
            notes.append(f"Thin volume ({vol_ratio:.1f}x avg)")

    return int(max(5, min(95, round(score)))), notes


def _action_for(state: str, entry_fired: bool, exit_fired: bool, bars_in_state: int | None) -> tuple[str, str]:
    if entry_fired and (bars_in_state or 0) == 0:
        side = "BUY" if state == "LONG" else "SELL"
        return side, "Fresh entry trigger on the last closed bar"
    if state == "LONG":
        return "HOLD LONG", f"Long open for {bars_in_state} bar(s) - manage with the plan below"
    if state == "SHORT":
        return "HOLD SHORT", f"Short open for {bars_in_state} bar(s) - manage with the plan below"
    if exit_fired:
        return "EXIT", "Exit trigger fired - the setup is over"
    return "WAIT", "No setup right now - the engine is watching"


ARMED_WINDOW = 3  # bars an HVB stays "armed" while waiting for the structure break


def _setup_block(
    prepared: pd.DataFrame,
    triggers,
    strategy: Strategy,
    params: dict[str, Any],
    entry_fired: bool,
    state: str,
    action: str,
    price: float,
) -> dict[str, Any]:
    """What the setup is doing right now - state only, no stop, no targets.

    TRIGGERED : every condition completed on the last closed candle
    ARMED     : the TDI momentum break (HVB) printed, price still has to break
                a swing level
    FORMING   : price is closing in on a swing level and/or the purple line is
                heading for the base line
    IDLE      : nothing worth watching on this instrument
    """
    has_tdi = "hvb_up" in prepared.columns and "tdi_mbl" in prepared.columns
    has_swings = "sw_hi_minor" in prepared.columns

    def val(col: str) -> float | None:
        try:
            v = float(prepared[col].iloc[-1])
            return v if np.isfinite(v) else None
        except Exception:
            return None

    atr_now = val("atr")
    purple, base = val("tdi_fast"), val("tdi_mbl")
    upper, lower = val("tdi_upper"), val("tdi_lower")

    # Nearest swing levels that price still has to break, on each side.
    highs = [v for v in (val("sw_hi_minor"), val("sw_hi_major")) if v and price and v > price]
    lows = [v for v in (val("sw_lo_minor"), val("sw_lo_major")) if v and price and v < price]
    level_up = min(highs) if highs else None
    level_down = max(lows) if lows else None

    # When did an HVB last print, whichever direction? A FORMING/BLOCKED state
    # means "before the HVB" - showing the distance makes that obvious.
    hvb_last_bars: int | None = None
    if has_tdi:
        both = (prepared["hvb_up"] | prepared["hvb_down"]).to_numpy()
        hits = both.nonzero()[0]
        if hits.size:
            hvb_last_bars = int(len(both) - 1 - hits[-1])
        else:
            hvb_last_bars = -1  # never in this window

    status = "IDLE"
    direction: str | None = None
    level: float | None = None
    bars_ago: int | None = None
    text = "No setup on this instrument right now."

    if has_tdi:
        up_hits = prepared["hvb_up"].tail(ARMED_WINDOW).to_numpy().nonzero()[0]
        down_hits = prepared["hvb_down"].tail(ARMED_WINDOW).to_numpy().nonzero()[0]
        last_up = int(up_hits[-1]) if up_hits.size else None
        last_down = int(down_hits[-1]) if down_hits.size else None

        if entry_fired:
            status = "TRIGGERED"
            direction = "LONG" if bool(triggers.long_entry.iloc[-1]) else "SHORT"
            bars_ago = 0
            text = "HVB crossed the base line and the swing broke on the same candle."
        elif last_up is not None or last_down is not None:
            if last_up is not None and (last_down is None or last_up >= last_down):
                direction, bars_ago = "LONG", ARMED_WINDOW - 1 - last_up
            else:
                direction, bars_ago = "SHORT", ARMED_WINDOW - 1 - last_down
            status = "ARMED"
            wait_for = "high" if direction == "LONG" else "low"
            text = f"HVB fired {bars_ago} bar(s) ago - waiting for price to break a {wait_for}."
        elif purple is not None and base is not None:
            # How close is the purple line to the base line, and price to structure?
            half_band = max(((upper - lower) / 2.0) if (upper and lower) else 5.0, 0.5)
            tdi_gap = abs(purple - base) / half_band
            bias = "LONG" if purple > base else "SHORT"
            next_level = level_up if bias == "LONG" else level_down
            gap_atr = abs(next_level - price) / atr_now if (next_level and atr_now) else None
            if (gap_atr is not None and gap_atr <= 1.0) or tdi_gap <= 0.2:
                status = "FORMING"
                direction = bias
                text = (
                    f"Purple line {abs(purple - base):.2f} pts from the base line - "
                    f"setup building for a {bias.lower()}."
                )

    if direction == "LONG":
        level = level_up
    elif direction == "SHORT":
        level = level_down

    # v2-style strategies publish a per-bar gate; a setup that fails it is shown
    # as BLOCKED so the radar never advertises a trade the rules forbid.
    blocked_reason = None
    if direction and status in ("FORMING", "ARMED") and "setup_ok_long" in prepared.columns:
        column = "setup_ok_long" if direction == "LONG" else "setup_ok_short"
        if not bool(prepared[column].iloc[-1]):
            row = prepared.iloc[-1]
            if row.get("regime") != ms.TRENDING:
                blocked_reason = f"market is {str(row.get('regime') or 'unknown').lower()} - {row.get('regime_reason')}"
            elif (row.get("liq_above") if direction == "LONG" else row.get("liq_below")):
                blocked_reason = "liquidity build-up in front of the setup"
            elif (row.get("trend_dir") or 0) * (1 if direction == "LONG" else -1) <= 0:
                blocked_reason = "setup is against the external trend"
            else:
                blocked_reason = "no internal structure to break"
            status = "BLOCKED"
            text = f"Setup forming, but blocked: {blocked_reason}."

    distance_pct = round((level - price) / price * 100, 3) if (level and price) else None

    return {
        "status": status,
        "direction": direction,
        "level": round(float(level), 8) if level else None,
        "distance_pct": distance_pct,
        "hvb_bars_ago": bars_ago,
        "hvb_last_bars": hvb_last_bars,
        "text": text,
        "blocked_reason": blocked_reason,
        "tdi": {
            "purple": round(purple, 3) if purple is not None else None,
            "base": round(base, 3) if base is not None else None,
            "slow": (round(val("tdi_slow"), 3) if val("tdi_slow") is not None else None),
            "side": ("above" if (purple or 0) > (base or 0) else "below") if purple is not None else None,
        }
        if has_tdi
        else None,
    }


def build_signal(
    symbol: str,
    timeframe: str,
    strategy_id: str,
    params: dict[str, Any] | None = None,
    limit: int = 400,
    candles: pd.DataFrame | None = None,
    source: str | None = None,
    replay_bars: int = 0,
) -> dict[str, Any]:
    strategy: Strategy = get_strategy(strategy_id)
    resolved = strategy.resolve_params(params)

    if candles is None:
        candles, source = get_candles(symbol, timeframe, limit, replay_bars=replay_bars)
    source = source or "live"

    # Volume context is useful in every strategy's confidence score.
    candles = candles.copy()
    candles["volume_ratio"] = candles["volume"] / candles["volume"].rolling(20, min_periods=5).mean()

    prepared = strategy.prepare(candles, resolved)
    triggers = strategy.rules(prepared, resolved)

    state_info = _state_at_last_bar(prepared, triggers)

    last = prepared.iloc[-1]
    prev_time = int(prepared["time"].iloc[-1])
    entry_fired = bool(triggers.long_entry.iloc[-1] or triggers.short_entry.iloc[-1])
    exit_fired = bool(triggers.long_exit.iloc[-1] or triggers.short_exit.iloc[-1])

    state = state_info["state"]
    action, action_reason = _action_for(
        state, entry_fired, exit_fired, state_info.get("bars_in_state")
    )

    if entry_fired:
        side = "LONG" if bool(triggers.long_entry.iloc[-1]) else "SHORT"
    elif state in ("LONG", "SHORT"):
        side = state
    else:
        side = ""

    bias = side if side in ("LONG", "SHORT") else "NEUTRAL"

    # Direction used for the illustrative plan when there is no live position
    # or trigger: fall back to the trend the strategy itself is tracking.
    trend_ref = _f(last.get("trend_ema")) or _f(last.get("slow_ema"))
    price = _f(last["close"], 0.0) or 0.0
    plan_side = side or ("LONG" if (trend_ref is None or price >= trend_ref) else "SHORT")
    # Stop-less systems only report the setup; no entry/stop/target ladder.
    targets = (
        strategy.targets(prepared, resolved, plan_side)
        if getattr(strategy, "uses_stops", True)
        else Targets()
    )
    confidence, confidence_notes = _confidence(prepared, bias if bias != "NEUTRAL" else plan_side)
    unrealized = None
    if state_info["entry_price"] and state != "FLAT":
        raw = (price / state_info["entry_price"] - 1) * 100
        unrealized = round(raw if state == "LONG" else -raw, 2)

    checks = [c.as_dict() for c in strategy.checks(prepared, resolved)]

    # Chart payload: last N bars + only the indicator lines the strategy plots.
    window = prepared.tail(240)
    series: dict[str, list[float | None]] = {}
    for col in ("open", "high", "low", "close", "volume", "volume_ratio"):
        if col in window:
            series[col] = [None if pd.isna(v) else round(float(v), 8) for v in window[col]]
    lines = {}
    for line in strategy.plot_lines:
        col = line["column"]
        if col in window:
            lines[col] = {
                "label": line["label"],
                "colour": line["colour"],
                "values": [None if pd.isna(v) else round(float(v), 8) for v in window[col]],
            }
    # Optional oscillator sub-pane (TDI for the shift strategy).
    pane_payload = None
    if strategy.pane:
        spec = strategy.pane
        pane_lines = {}
        for line in spec.get("lines", []):
            col = line["column"]
            if col in window:
                pane_lines[col] = {
                    "label": line["label"],
                    "colour": line["colour"],
                    "width": line.get("width", 1.4),
                    "values": [None if pd.isna(v) else round(float(v), 3) for v in window[col]],
                }
        pane_bands = []
        for band in spec.get("bands", []):
            if band["upper"] in window and band["lower"] in window:
                pane_bands.append(
                    {
                        "colour": band.get("colour", "#3b82f6"),
                        "upper": [None if pd.isna(v) else round(float(v), 3) for v in window[band["upper"]]],
                        "lower": [None if pd.isna(v) else round(float(v), 3) for v in window[band["lower"]]],
                    }
                )
        pane_markers = []
        for marker in spec.get("markers", []):
            col = marker["column"]
            if col in window:
                for pos in window[col].to_numpy().nonzero()[0]:
                    pane_markers.append({"index": int(pos), "side": marker["side"]})
        pane_payload = {
            "title": spec.get("title", ""),
            "levels": spec.get("levels", []),
            "lines": pane_lines,
            "bands": pane_bands,
            "markers": pane_markers,
        }

    markers = []
    for i in range(len(window)):
        idx = len(prepared) - len(window) + i
        if bool(triggers.long_entry.iloc[idx]):
            markers.append({"index": i, "side": "LONG", "price": float(window["low"].iloc[i])})
        elif bool(triggers.short_entry.iloc[idx]):
            markers.append({"index": i, "side": "SHORT", "price": float(window["high"].iloc[i])})

    # How close is price to a fresh trigger? Useful while flat.
    readiness = None
    channel_high, channel_low = _f(last.get("channel_high")), _f(last.get("channel_low"))
    if price and channel_high and channel_low:
        up_dist = (channel_high - price) / price * 100
        down_dist = (price - channel_low) / price * 100
        nearest = min(up_dist, down_dist)
        readiness = int(max(0, min(100, round(100 - (nearest / 3.0) * 100))))
    elif price:
        # Strategies built on swing levels (e.g. TDI + shift): how close is price
        # to the nearest structure that still has to break?
        candidates = [
            _f(last.get(col))
            for col in ("sw_hi_minor", "sw_lo_minor", "sw_hi_major", "sw_lo_major")
        ]
        distances = [abs(price - lvl) for lvl in candidates if lvl]
        atr_now = _f(last.get("atr"))
        structure_ready = None
        if distances:
            nearest = min(distances)
            # Normalise by ATR so "close" means the same thing on every asset.
            span = (atr_now * 2.0) if atr_now else (price * 0.02)
            structure_ready = max(0.0, min(100.0, 100 - (nearest / span) * 100))

        # Strategies with an oscillator pane (TDI): the first condition is the
        # purple line approaching the base line, so show that too.
        pane_ready = None
        upper, lower = _f(last.get("tdi_upper")), _f(last.get("tdi_lower"))
        purple, base = _f(last.get("tdi_fast")), _f(last.get("tdi_mbl"))
        if upper is not None and lower is not None and purple is not None and base is not None:
            half_band = max((upper - lower) / 2.0, 0.5)
            pane_ready = max(0.0, min(100.0, 100 - (abs(purple - base) / half_band) * 100))

        parts = [v for v in (structure_ready, pane_ready) if v is not None]
        if parts:
            readiness = int(round(sum(parts) / len(parts)))

    last_trigger = None
    for name, label in (("long_entry", "LONG"), ("short_entry", "SHORT")):
        trigger_series = getattr(triggers, name)
        hits = trigger_series.to_numpy().nonzero()[0]
        if hits.size:
            idx = int(hits[-1])
            candidate = {
                "side": label,
                "time": int(prepared["time"].iloc[idx]),
                "price": round(float(prepared["close"].iloc[idx]), 8),
                "bars_ago": int(len(prepared) - 1 - idx),
            }
            if last_trigger is None or candidate["time"] > last_trigger["time"]:
                last_trigger = candidate

    if bias == "NEUTRAL":
        confidence = readiness if readiness is not None else 30
        confidence_label = "Setup readiness"
    else:
        confidence_label = "Conviction"

    setup = _setup_block(prepared, triggers, strategy, resolved, entry_fired, state, action, price)
    regime = ms.describe(prepared.iloc[-1]) if "regime" in prepared.columns else None

    return {
        "setup": setup,
        "regime": regime,
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "timeframe_label": TIMEFRAMES.get(timeframe, {}).get("label", timeframe),
        "strategy": {"id": strategy.id, "name": strategy.name, "uses_stops": strategy.uses_stops},
        "params": resolved,
        "source": source,
        "replay_bars": int(replay_bars or 0),
        "generated_at": int(time.time() * 1000),
        "bar_time": prev_time,
        "price": round(price, 8),
        "change_pct": round(float(prepared["close"].pct_change().iloc[-1] * 100), 3)
        if len(prepared) > 1 and pd.notna(prepared["close"].pct_change().iloc[-1]) else 0.0,
        "action": action,
        "action_reason": action_reason,
        "bias": bias,
        "confidence": confidence,
        "confidence_label": confidence_label,
        "confidence_notes": confidence_notes,
        "readiness": readiness,
        "last_trigger": last_trigger,
        "state": {
            "position": state,
            "since": state_info["since"],
            "entry_price": round(state_info["entry_price"], 8) if state_info["entry_price"] else None,
            "bars_in_state": state_info["bars_in_state"],
            "unrealized_pct": unrealized,
        },
        "checks": checks,
        "plan": {
            **targets.as_dict(plan_side, price_decimals=6),
            "is_live": bool(bias != "NEUTRAL"),
            "note": (
                "No stop loss or take profit - this system reports the setup only"
                if not getattr(strategy, "uses_stops", True)
                else ("Live setup" if bias != "NEUTRAL" else "Illustrative levels from the current bias")
            ),
        },
        "indicators": {
            k: round(float(v), 4)
            for k, v in last.items()
            if k not in ("time", "open", "high", "low", "close", "volume") and isinstance(v, (int, float, np.floating))
            and pd.notna(v)
        },
        "chart": {
            "time": [int(v) for v in window["time"]],
            "series": series,
            "lines": lines,
            "markers": markers,
            "pane": pane_payload,
        },
    }


def scan(
    symbols: list[str],
    timeframe: str,
    strategy_id: str,
    params: dict[str, Any] | None = None,
    limit: int = 300,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            signal = build_signal(symbol, timeframe, strategy_id, params, limit=limit)
            rows.append(
                {
                    "symbol": symbol,
                    "price": signal["price"],
                    "change_pct": signal["change_pct"],
                    "action": signal["action"],
                    "bias": signal["bias"],
                    "confidence": signal["confidence"],
                    "state": signal["state"]["position"],
                    "unrealized_pct": signal["state"]["unrealized_pct"],
                    "distance_to_stop_pct": _distance_to_stop(signal),
                    "setup_status": signal["setup"]["status"],
                    "setup_direction": signal["setup"]["direction"],
                    "setup_level": signal["setup"]["level"],
                    "setup_distance_pct": signal["setup"]["distance_pct"],
                    "source": signal["source"],
                    "error": None,
                }
            )
        except (MarketDataError, KeyError) as exc:
            rows.append({"symbol": symbol, "error": str(exc)})

    rank = {"BUY": 0, "SELL": 0, "HOLD LONG": 1, "HOLD SHORT": 1, "EXIT": 2, "WAIT": 3}
    rows.sort(key=lambda r: (rank.get(r.get("action", "WAIT"), 9), -(r.get("confidence") or 0)))
    return {"timeframe": timeframe, "strategy": strategy_id, "rows": rows, "symbols": symbols}


def _distance_to_stop(signal: dict[str, Any]) -> float | None:
    plan = signal.get("plan") or {}
    price, stop = plan.get("entry"), plan.get("stop")
    if not price or not stop:
        return None
    return round(abs(price - stop) / price * 100, 2)
