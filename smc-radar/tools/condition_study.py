#!/usr/bin/env python3
"""Does "more of the book's conditions" actually mean a better trade?

Round 7 asks the question the user asked: a high or low made by an algo candle
is only *more reliable* when it satisfies the most conditions, so why did a
setup with a handful of conditions print an A+? Because the old letter came from
a weighted score, not from a count of conditions.

This tool trades every zone's plan on recorded candles (limit at the block edge,
stop beyond the level, target at the next liquidity pool - the same rules the app
ships) and reports the outcome **per number of conditions met**, plus the
frequency/accuracy frontier: what you get if you demand k conditions.

    python tools/condition_study.py --csv study-data/BTCUSDT-1m-90d.csv --timeframe 1m
    python tools/condition_study.py --csv study-data/BTCUSDT-1m-90d-holdout.csv --timeframe 1m

Every number in docs/conditions.md comes from this command, and nothing is
looked ahead: a plan only fills on a candle that came after the zone formed, and
when one candle can hit both the stop and the target the stop wins.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.map import CONDITION_NAMES, DEFAULTS, MarketMap  # noqa: E402
from tools.zone_quality import read_csv, roll  # noqa: E402


def collect(bars: list[dict], symbol: str, timeframe: str, overrides: dict | None = None) -> list:
    """Replay the candles and keep every zone that ever got a plan with a target."""
    settings = {**DEFAULTS, **(overrides or {})}
    map_ = MarketMap(symbol, timeframe, settings)
    seen: dict[str, object] = {}
    for bar in bars:
        map_.add(bar)
        for zone in map_.zones:
            seen.setdefault(zone.key, zone)
    return [z for z in seen.values() if z.plan and z.plan.get("target") is not None]


def simulate(zone, bars: list[dict], horizon: int) -> dict:
    plan = zone.plan
    entry, stop, target = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    short = plan["side"] == "short"
    start = int(zone.index) + 1
    end = min(len(bars), start + horizon)
    filled = None
    for i in range(start, end):
        bar = bars[i]
        if (short and float(bar["high"]) >= entry) or ((not short) and float(bar["low"]) <= entry):
            filled = i
            break
    if filled is None:
        return {"result": "no fill", "r": 0.0, "bars": None}
    for i in range(filled, min(len(bars), filled + horizon)):
        bar = bars[i]
        hit_stop = float(bar["high"]) >= stop if short else float(bar["low"]) <= stop
        hit_target = float(bar["low"]) <= target if short else float(bar["high"]) >= target
        if hit_stop:
            return {"result": "stopped", "r": -1.0, "bars": i - filled}
        if hit_target:
            return {"result": "target", "r": float(plan["rr"]), "bars": i - filled}
    last = bars[min(len(bars) - 1, filled + horizon - 1)]
    r = ((entry - float(last["close"])) if short else (float(last["close"]) - entry)) / max(1e-12, abs(stop - entry))
    return {"result": "open", "r": round(r, 3), "bars": horizon}


def stats(rows: list[dict], days: float) -> str:
    if not rows:
        return f"{0:>7}{0:>8}{0:>7}{0:>8}{0:>9}"
    trades = [r for r in rows if r["filled"]]
    decided = [r for r in trades if r["result"] in ("stopped", "target")]
    wins = sum(1 for r in decided if r["result"] == "target")
    exp = sum(r["r"] for r in trades) / len(trades) if trades else 0.0
    per_day = len(trades) / days if days else 0.0
    return (
        f"{len(rows):>7}{len(trades) / len(rows):>8.0%}{per_day:>7.1f}"
        f"{(wins / len(decided) if decided else 0):>8.0%}{exp:>9.2f}"
    )


HEAD = f"{'group':<24}{'plans':>7}{'filled':>8}{'trades/d':>7}{'win':>8}{'exp R':>9}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Outcome per number of conditions met")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--horizon", type=int, default=240, help="bars to fill / to resolve")
    parser.add_argument("--json", default="", help="write the raw rows here")
    parser.add_argument("--fee-bps", type=float, default=6.0,
                        help="taker fee in basis points, charged on entry AND exit (MEXC futures 6)")
    parser.add_argument("--stop-atr", type=float, default=None, help="override plan_stop_atr")
    parser.add_argument("--min-risk-pct", type=float, default=None, help="override plan_min_risk_pct")
    parser.add_argument("--rr-min", type=float, default=0.0, help="only count plans at or above this R:R")
    parser.add_argument("--net", action="store_true", help="only print the fee-adjusted summary")
    args = parser.parse_args()

    bars = roll(read_csv(Path(args.csv)), args.timeframe)
    if not bars:
        print(f"no candles in {args.csv}", file=sys.stderr)
        return 2
    days = max(1e-9, (bars[-1]["close_time"] - bars[0]["open_time"]) / 86_400_000)
    overrides = {}
    if args.stop_atr is not None:
        overrides["plan_stop_atr"] = args.stop_atr
    if args.min_risk_pct is not None:
        overrides["plan_min_risk_pct"] = args.min_risk_pct
    zones = collect(bars, args.symbol, args.timeframe, overrides)
    if args.rr_min:
        zones = [z for z in zones if (z.plan.get("rr") or 0) >= args.rr_min]

    rows: list[dict] = []
    for zone in zones:
        res = simulate(zone, bars, args.horizon)
        rows.append(
            {
                "kind": zone.kind,
                "side": zone.side,
                "grade": zone.grade,
                "met": zone.cond_met,
                "total": zone.cond_total,
                "conds": dict(zone.conds),
                "rr": zone.plan.get("rr"),
                # the stop distance as a percentage of price (the fee is charged
                # on the notional, so this is what turns a fee into R)
                "risk_pct": 100.0 * abs(float(zone.plan["stop"]) - float(zone.plan["entry"])) / float(zone.plan["entry"]),
                "time": zone.time,
                **res,
                "filled": res["result"] != "no fill",
            }
        )

    print(f"{args.csv} · {args.timeframe} · {len(bars):,} candles · {days:.0f} days · {len(rows)} plans")
    print(f"horizon {args.horizon} bars to fill, then {args.horizon} bars to resolve")
    fee = args.fee_bps / 10_000.0
    def net_of_fee(group: list[dict]) -> tuple[float, float]:
        trades = [r for r in group if r["filled"] and r.get("risk_pct")]
        if not trades:
            return 0.0, 0.0
        nets = [r["r"] - (2 * fee / (r["risk_pct"] / 100.0)) for r in trades if r["risk_pct"] > 0]
        return sum(nets) / len(nets), sum(1 for n in nets if n > 0) / len(nets)

    gross_exp = sum(r["r"] for r in rows if r["filled"]) / max(1, len([r for r in rows if r["filled"]]))
    net_exp, _ = net_of_fee(rows)
    print(f"{'all plans':<24}{stats(rows, days)}"
          f"   gross {gross_exp:+.2f} R  net of {args.fee_bps:g}bps x2 {net_exp:+.2f} R")
    print()
    if args.net:
        return 0

    print("--- by number of conditions met (out of %d)" % len(CONDITION_NAMES))
    print(HEAD)
    for met in sorted({r["met"] for r in rows}):
        group = [r for r in rows if r["met"] == met]
        print(f"{('%d conditions' % met):<24}{stats(group, days)}")
    print()

    print("--- the frontier: demand at least k conditions")
    print(HEAD)
    for k in range(0, len(CONDITION_NAMES) + 1):
        group = [r for r in rows if r["met"] >= k]
        if group:
            print(f"{('>= %d conditions' % k):<24}{stats(group, days)}")
    print()

    print("--- each condition on its own (all plans with it vs without it)")
    print(f"{'condition':<24}{'plans':>7}{'with':>9}{'without':>9}{'exp with':>10}{'exp w/o':>9}")
    for name in CONDITION_NAMES:
        with_it = [r for r in rows if r["conds"].get(name)]
        without = [r for r in rows if not r["conds"].get(name)]
        if not with_it or not without:
            continue
        def exp_win(group):
            trades = [r for r in group if r["filled"]]
            decided = [r for r in trades if r["result"] in ("stopped", "target")]
            wins = sum(1 for r in decided if r["result"] == "target")
            exp = sum(r["r"] for r in trades) / len(trades) if trades else 0.0
            return (wins / len(decided) if decided else 0), exp
        w_win, w_exp = exp_win(with_it)
        o_win, o_exp = exp_win(without)
        print(f"{name:<24}{len(rows):>7}{w_win:>9.0%}{o_win:>9.0%}{w_exp:>10.2f}{o_exp:>9.2f}")
    print()

    print("--- by kind, with the condition count")
    print(HEAD)
    for kind in sorted({r["kind"] for r in rows}):
        group = [r for r in rows if r["kind"] == kind]
        print(f"{kind:<24}{stats(group, days)}")
    print()

    print("--- the grade the page prints, measured")
    print(HEAD)
    for grade in ("A+", "A", "B", "C"):
        group = [r for r in rows if r["grade"] == grade]
        if group:
            print(f"{grade:<24}{stats(group, days)}")

    if args.json:
        import json

        Path(args.json).write_text(json.dumps(rows, indent=1))
        print(f"\nraw rows -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
