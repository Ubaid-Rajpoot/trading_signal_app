#!/usr/bin/env python3
"""Replay recorded exchange candles through the real engine and count signals.

This is how the defaults in ``config.json`` were chosen - it is the same engine,
the same structure code and the same filters the live bot runs, only the feed is
a folder of real CSV files. Nothing here can produce a signal the live bot would
not produce.

    python tools/scan_history.py data/btc --symbol BTCUSDT --timeframes 5m,15m,30m,1h
    python tools/scan_history.py data/btc --symbol BTCUSDT --hvb-points 1.5
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.buckets import Rollup  # noqa: E402
from bot.engine import Engine  # noqa: E402

TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}


def read_candles(folder: Path, symbol: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(folder.rglob(f"{symbol}-*.csv")):
        with path.open(newline="") as handle:
            for row in csv.reader(handle):
                if len(row) < 6:
                    continue
                raw = int(row[0])
                open_time = raw // 1000 if raw > 10**14 else raw
                if open_time < 10**12:
                    open_time *= 1000
                rows.append(
                    {
                        "symbol": symbol,
                        "open_time": open_time,
                        "close_time": open_time + 59_999,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                        "trades": 0,
                        "closed": True,
                    }
                )
    rows.sort(key=lambda r: r["open_time"])
    return rows


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default="data/btc")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframes", default="5m,15m,30m,1h")
    ap.add_argument("--pivot-len", type=int, default=5)
    ap.add_argument("--sweep-max-wait", type=int, default=60)
    ap.add_argument("--min-rr", type=float, default=0.0)
    ap.add_argument("--max-bars", type=int, default=0)
    ap.add_argument("--trend", default="off")
    ap.add_argument("--target", default="next", choices=["next", "range"])
    ap.add_argument("--hvb-points", type=float, default=1.0, help="how far the price line must break the moving centre band, in RSI points")
    ap.add_argument("--hvb-speed", type=float, default=1.5,
                    help="this candle's step must be this many times the price line's average step")
    ap.add_argument("--no-regime", action="store_true", help="switch the ADX/ER trend gate off")
    # ---- round 17: the swing-structure trend rules
    ap.add_argument("--no-trend-context", dest="trend_context", action="store_false",
                    help="do NOT require the external structure to agree (HH+HL / LH+LL)")
    ap.add_argument("--no-hh-hl", dest="require_hh_hl", action="store_false",
                    help="require only the direction, not the clean swing sequence")
    ap.add_argument("--no-bos", dest="require_bos", action="store_false",
                    help="do not require a recent break of structure")
    ap.add_argument("--bos-age", type=int, default=240,
                    help="how many bars back a break of structure still counts (0 = forever)")
    ap.add_argument("--max-depth", type=int, default=2,
                    help="1 = external only, 2 = one level of internal (default), 3+ allows nesting")
    ap.add_argument("--no-clear-structure", dest="clear_structure", action="store_false",
                    help="research only: allow one recent HH/HL pair without wider left context")
    ap.add_argument("--trend-steps", type=int, default=2,
                    help="consecutive high and low swing steps required at major/macro degree")
    ap.add_argument("--directional-ratio", type=float, default=1.0,
                    help="fraction of recent structural steps that must agree")
    ap.add_argument("--fresh-shift-bars", type=int, default=20,
                    help="bars for a major close beyond the remembered range edge to establish direction")
    ap.add_argument("--hvb-window", type=int, default=0,
                    help="0 = shipped same-candle HVB; larger windows are research only")
    ap.add_argument("--volume-break", action="store_true",
                    help="the signal candle must be a high-volume bar")
    ap.add_argument("--volume-mult", type=float, default=1.5, help="how many x the average volume")
    ap.add_argument("--volume-period", type=int, default=20)
    ap.add_argument("--confirm", default="all", choices=["all", "score"],
                    help="all = every condition must hold; score = weighted confluence >= --confirm-min")
    ap.add_argument("--confirm-min", type=float, default=60.0, help="the confluence score needed")
    ap.add_argument("--macro-len", type=int, default=80, help="bars each side of the macro swing")
    ap.add_argument("--adx-min", type=float, default=22.0, help="ADX(14) needed for a trending market")
    ap.add_argument("--er-min", type=float, default=0.30, help="efficiency ratio (50) needed for a trending market")
    ap.add_argument("--regime-dir", default="any", choices=["any", "with"], help="also demand the EMA direction")
    ap.add_argument("--horizon", type=int, default=300, help="bars to look ahead when scoring a signal")
    ap.add_argument("--score-target", default="structure", choices=["structure", "fixed"],
                    help="score against the setup's own target, or a fixed multiple of the risk "
                         "(the structure target is often absent, which leaves trades unscored)")
    ap.add_argument("--score-r", type=float, default=2.0, help="the fixed target, in R (risk units)")
    ap.add_argument("--no-score", action="store_true", help="skip the win/loss walk-forward")
    ap.add_argument("--major-len", type=int, default=20, help="bars each side of a MAJOR swing (MS)")
    ap.add_argument("--premium", action="store_true",
                    help="require the sweep to happen in premium/discount (p.42) - off by default, measured harmful")
    ap.add_argument("--premium-level", type=float, default=0.5, help="how deep into premium/discount")
    ap.add_argument("--premium-on-entry", action="store_true", help="measure the ENTRY instead of the sweep")
    ap.add_argument("--no-structure-direction", dest="structure_direction", action="store_false",
                    help="do NOT require the last major break to agree (it is required by default, like the app)")
    ap.add_argument("--require-ms", action="store_true", help="the confirming break must be an MS")
    ap.add_argument("--require-fvg", action="store_true", help="the displacement must leave an FVG (p.87)")
    ap.add_argument("--no-pool", action="store_true", help="do not block trades aimed into equal highs/lows")
    ap.add_argument("--pool-count", type=int, default=3, help="equal highs/lows that count as a pool")
    ap.add_argument("--structure-atr-min", type=float, default=0.0)
    ap.add_argument("--structure-atr-max", type=float, default=0.0)
    ap.add_argument("--json", default="", help="write the signals as JSON (for offline analysis)")
    ap.add_argument("--no-structure", action="store_true")
    ap.add_argument("--require-break", dest="require_break", action="store_true",
                    help="also require the 20-bar high/low break")
    args = ap.parse_args()

    folder = Path(args.folder)
    if not folder.is_absolute():
        folder = ROOT / folder
    candles = read_candles(folder, args.symbol)
    if not candles:
        print(f"no CSV files for {args.symbol} in {folder}")
        return 1

    captured: list[dict] = []

    async def collect(message: dict) -> None:
        if message.get("type") == "alert" and message["alert"]["kind"] == "signal":
            captured.append(message["alert"])

    engine = Engine(
        broadcast=collect,
        symbols=[args.symbol],
        timeframes=args.timeframes.split(","),
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9,
                      "band_mult": 1.6185, "hvb_reference": "mbl"},
        filters={
            "structure": not args.no_structure,
            "hvb": True,
            "hvb_min_points": args.hvb_points,
            "hvb_window": args.hvb_window,
            "break": bool(args.require_break),
            "min_rr": args.min_rr,
            "max_bars_since_sweep": args.max_bars,
            "trend": args.trend,
            "hvb_min_speed": args.hvb_speed,
            "regime": not args.no_regime,
            "regime_adx_min": args.adx_min,
            "regime_er_min": args.er_min,
            "regime_direction": args.regime_dir,
            "premium_discount": bool(args.premium),
            "premium_level": args.premium_level,
            "premium_on_entry": args.premium_on_entry,
            "structure_direction": args.structure_direction,
            "require_ms": args.require_ms,
            "require_fvg": args.require_fvg,
            "block_liquidity_pool": not args.no_pool,
            "pool_count": args.pool_count,
            "structure_atr_min": args.structure_atr_min,
            "structure_atr_max": args.structure_atr_max,
            "trend_context": args.trend_context,
            "require_hh_hl": args.require_hh_hl,
            "clear_structure": args.clear_structure,
            "min_trend_steps": args.trend_steps,
            "min_directional_ratio": args.directional_ratio,
            "fresh_shift_max_bars": args.fresh_shift_bars,
            "require_bos": args.require_bos,
            "bos_max_age": args.bos_age,
            "max_internal_depth": args.max_depth,
            "volume_break": args.volume_break,
            "volume_mult": args.volume_mult,
            "volume_period": args.volume_period,
            "confirm_mode": args.confirm,
            "confirm_min_score": args.confirm_min,
        },
        structure={"pivot_len": args.pivot_len, "major_len": args.major_len,
                   "macro_len": args.macro_len,
                   "sweep_max_wait": args.sweep_max_wait, "target_mode": args.target},
    )

    signals: dict[str, list[dict]] = defaultdict(list)
    await engine.warmup(type("Feed", (), {"history": _empty_history, "exchange_interval": lambda s, tf: tf})())
    for candle in candles:
        await engine.on_candle(candle)

    for alert in captured:
        signals[alert["timeframe"]].append(alert)

    days = (candles[-1]["close_time"] - candles[0]["open_time"]) / 86_400_000
    print(f"{args.symbol}: {len(candles)} 1m bars, {days:.0f} days, timeframes {list(signals)}")
    print(f"filters: {engine.filters.config}")
    print(f"structure: pivot_len={args.pivot_len} sweep_max_wait={args.sweep_max_wait}")
    print()
    total = 0
    for tf in engine.timeframes:
        got = signals.get(tf, [])
        total += len(got)
        sides = Counter(a["side"] for a in got)
        rrs = [a["setup"]["rr"] for a in got if a.get("setup") and a["setup"].get("rr")]
        hvb = sum(1 for a in got if a.get("hvb"))
        kinds = Counter((a.get("break_kind") or "?") for a in got)
        zones = Counter(((a.get("book") or {}).get("zone") or "?") for a in got)
        print(f"       break kinds {dict(kinds)} · zones {dict(zones)}")
        depths = Counter(str((a.get("depth") or {}).get("value", "?")) for a in got)
        ext = Counter((str(((a.get("context") or {}).get("external") or {}).get("direction"))
                       + "/" + str(((a.get("context") or {}).get("external") or {}).get("source")))
                      for a in got)
        vols = [a.get("volume_ratio") for a in got if a.get("volume_ratio")]
        scores = [a.get("score") for a in got if a.get("score") is not None]
        print(f"       depth {dict(sorted(depths.items()))} · external trend {dict(ext)}"
              + (f" · median volume {sorted(vols)[len(vols)//2]:.2f}x" if vols else "")
              + (f" · median score {sorted(scores)[len(scores)//2]:.0f}" if scores else ""))
        print(f"{tf:>4}: {len(got):>4} signals  ({len(got) / max(days, 1):.2f}/day)  "
              f"long {sides.get('LONG', 0)} short {sides.get('SHORT', 0)}  "
              f"hvb {hvb}  median RR {sorted(rrs)[len(rrs) // 2] if rrs else '-'}")
    print(f"\ntotal {total} signals over {days:.0f} days = {total / max(days, 1):.2f}/day")
    summary = engine.filters.summary()
    print(f"filtered out: {summary['rejected']}  ({summary['reasons']})")
    print(f"other events: {engine.kinds}")

    if args.json:
        Path(args.json).write_text(json.dumps(captured))
        print(f"signals written to {args.json}")

    if not args.no_score:
        report = score(captured, candles, engine.timeframes, args.horizon,
                       target_mode=args.score_target, fixed_r=args.score_r)
        print()
        print("does it work? every signal walked forward on its own timeframe")
        print(f"{'group':<26}{'n':>5}{'win':>6}{'loss':>6}{'open':>6}{'win%':>7}{'totalR':>8}{'R/trade':>9}")
        for label, rows in report:
            line = f"{label:<26}"
            line += f"{rows['n']:>5}{rows['wins']:>6}{rows['losses']:>6}{rows['open']:>6}"
            line += f"{rows['win_rate'] * 100 if rows['scored'] else 0:>6.1f}%"
            line += f"{rows['total_r']:>8.2f}{rows['expectancy']:>9.2f}"
            print(line)
    return 0


def _bars_by_timeframe(candles: list[dict], timeframes: list[str]) -> dict[str, list[dict]]:
    """Rebuild the same buckets the engine traded, for the forward walk."""
    out: dict[str, list[dict]] = {}
    for tf in timeframes:
        if tf == "1m":
            out[tf] = [dict(c, timeframe="1m", closed=True) for c in candles]
            continue
        rollup = Rollup(tf)
        bars: list[dict] = []
        for candle in candles:
            bars.extend(rollup.add(candle))
        bars.extend(rollup.flush())
        out[tf] = bars
    return out


def _outcome(alert: dict, bars: list[dict], horizon: int, target_mode: str = "structure",
             fixed_r: float = 2.0) -> tuple[str, float]:
    """Walk forward: did the target or the stop get hit first?

    Stop wins ties (the conservative reading of a candle that touched both).
    Returns (result, R) with result in {"win", "loss", "open"}.
    """
    setup = alert.get("setup") or {}
    entry, stop, target = setup.get("entry"), setup.get("stop"), setup.get("target")
    if not entry or not stop:
        return "open", 0.0
    risk = abs(entry - stop)
    if target_mode == "fixed" and risk > 0:
        target = entry + fixed_r * risk if str(alert.get("side")).upper() == "LONG" else entry - fixed_r * risk
    if not target:
        return "open", 0.0
    end = alert["time"]
    risk = abs(entry - stop)
    if risk <= 0:
        return "open", 0.0
    reward = abs(target - entry)
    long_side = str(alert.get("side")).upper() == "LONG"
    start = next((i for i, bar in enumerate(bars) if bar["close_time"] > end), None)
    if start is None:
        return "open", 0.0
    for bar in bars[start : start + horizon]:
        if long_side:
            if bar["low"] <= stop:
                return "loss", -1.0
            if bar["high"] >= target:
                return "win", reward / risk
        else:
            if bar["high"] >= stop:
                return "loss", -1.0
            if bar["low"] <= target:
                return "win", reward / risk
    return "open", 0.0


def _bucket(rows: list[tuple[str, float]]) -> dict:
    wins = sum(1 for r, _ in rows if r == "win")
    losses = sum(1 for r, _ in rows if r == "loss")
    opened = sum(1 for r, _ in rows if r == "open")
    scored = wins + losses
    total_r = sum(r for result, r in rows if result in {"win", "loss"})
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "open": opened,
        "scored": scored,
        "win_rate": (wins / scored) if scored else 0.0,
        "total_r": total_r,
        "expectancy": (total_r / scored) if scored else 0.0,
    }


def score(alerts: list[dict], candles: list[dict], timeframes: list[str], horizon: int,
          target_mode: str = "structure", fixed_r: float = 2.0) -> list[tuple[str, dict]]:
    """Break the results down by timeframe and by the regime the setup formed in."""
    bars = _bars_by_timeframe(candles, timeframes)
    by_tf: dict[str, list[tuple[str, float]]] = defaultdict(list)
    by_regime: dict[str, list[tuple[str, float]]] = defaultdict(list)
    by_depth: dict[str, list[tuple[str, float]]] = defaultdict(list)
    by_volume: dict[str, list[tuple[str, float]]] = defaultdict(list)
    by_grade: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for alert in alerts:
        result, r = _outcome(alert, bars[alert["timeframe"]], horizon, target_mode, fixed_r)
        by_tf[alert["timeframe"]].append((result, r))
        regime = (alert.get("regime") or {}).get("state") or "unknown"
        by_regime[regime].append((result, r))
        depth = (alert.get("depth") or {}).get("label") or "unknown"
        by_depth[str((alert.get("depth") or {}).get("value") or depth)].append((result, r))
        ratio = alert.get("volume_ratio")
        bucket = "?" if ratio is None else ("<1.0x" if ratio < 1 else "1-1.5x" if ratio < 1.5
                                           else "1.5-2.5x" if ratio < 2.5 else "2.5x+")
        by_volume[bucket].append((result, r))
        by_grade[str(alert.get("grade") or "older")].append((result, r))

    out: list[tuple[str, dict]] = []
    for tf in timeframes:
        if by_tf.get(tf):
            out.append((f"{tf}", _bucket(by_tf[tf])))
    out.append(("all timeframes", _bucket([row for rows in by_tf.values() for row in rows])))
    for state, rows in sorted(by_regime.items()):
        out.append((f"formed in {state}", _bucket(rows)))
    for depth, rows in sorted(by_depth.items()):
        out.append((f"depth {depth}", _bucket(rows)))
    for bucket, rows in sorted(by_volume.items()):
        out.append((f"volume {bucket}", _bucket(rows)))
    for grade, rows in sorted(by_grade.items()):
        out.append((f"grade {grade}", _bucket(rows)))
    return out


async def _empty_history(*_a, **_k):
    return []


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
