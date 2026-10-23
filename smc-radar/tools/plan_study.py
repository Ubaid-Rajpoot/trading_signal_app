#!/usr/bin/env python3
"""Do the plans actually work: limit at the block, stop beyond the level, target
at the next liquidity pool.

The scanner now attaches an execution plan to every zone (entry / stop /
target / R:R). A plan is only worth shipping if it can be traded as written, so
this tool does exactly that on recorded candles, candle by candle, with no
look-ahead:

    1. wait for price to come back to the entry (the block's near edge),
    2. after it fills, the first of stop / target decides the trade,
    3. if both can be touched inside the same candle, count the **stop**
       (conservative - the tool never assumes the good one came first).

Output: fill rate, win rate, expectancy in R, and the same table split by every
filter the page offers (grade, R:R, trend alignment, block thickness, kind).

    python tools/plan_study.py --csv study-data/BTCUSDT-1m-90d.csv --timeframe 1m
    python tools/plan_study.py --csv ... --timeframe 1m --min-rr 2 --aligned
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.map import DEFAULTS, MarketMap  # noqa: E402
from tools.zone_quality import read_csv, roll  # noqa: E402


def collect(bars: list[dict], symbol: str, timeframe: str) -> list:
    """Replay the candles and keep every zone that was ever created."""
    map_ = MarketMap(symbol, timeframe, DEFAULTS)
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
        return {"result": "no fill", "r": 0.0, "wait": None, "bars": None}
    for i in range(filled, min(len(bars), filled + horizon)):
        bar = bars[i]
        hit_stop = float(bar["high"]) >= stop if short else float(bar["low"]) <= stop
        hit_target = float(bar["low"]) <= target if short else float(bar["high"]) >= target
        if hit_stop:
            return {"result": "stopped", "r": -1.0, "wait": filled - start, "bars": i - filled}
        if hit_target:
            return {"result": "target", "r": float(plan["rr"]), "wait": filled - start, "bars": i - filled}
    last = bars[min(len(bars) - 1, filled + horizon - 1)]
    r = ((entry - float(last["close"])) if short else (float(last["close"]) - entry)) / max(1e-12, abs(stop - entry))
    return {"result": "open", "r": round(r, 3), "wait": filled - start, "bars": horizon}


def stats(rows: list[dict]) -> str:
    if not rows:
        return f"{'':<22}{0:>7}"
    trades = [r for r in rows if r["filled"]]
    decided = [r for r in trades if r["result"] in ("stopped", "target")]
    wins = sum(1 for r in decided if r["result"] == "target")
    exp = sum(r["r"] for r in trades) / len(trades) if trades else 0.0
    return (
        f"{len(rows):>7}{len(trades) / len(rows):>8.0%}"
        f"{(wins / len(decided) if decided else 0):>8.0%}{exp:>9.2f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Trade the scanner's plans on recorded candles")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--horizon", type=int, default=240, help="bars to wait for a fill and then for the result")
    args = parser.parse_args()

    raw = read_csv(Path(args.csv))
    bars = roll(raw, args.timeframe)
    zones = collect(bars, args.symbol, args.timeframe)
    rows = []
    for zone in zones:
        res = simulate(zone, bars, args.horizon)
        rows.append(
            {
                "kind": zone.kind,
                "grade": zone.grade,
                "rr": zone.plan["rr"],
                "side": zone.plan["side"],
                "aligned": bool((zone.trend or {}).get("aligned")),
                "block": zone.block_atr,
                "disp": zone.disp_atr,
                "confluence": bool(zone.confluence or zone.htf),
                "filled": res["result"] != "no fill",
                "result": res["result"],
                "r": res["r"],
                "wait": res["wait"],
            }
        )

    print(f"{args.csv} · {args.timeframe} · {len(bars):,} candles · {len(rows)} plans")
    print(f"horizon {args.horizon} bars to fill, then {args.horizon} bars to resolve\n")
    head = f"{'group':<22}{'plans':>7}{'filled':>8}{'win':>8}{'exp R':>9}"
    print(head)
    print(f"{'all plans':<22}" + stats(rows))

    def table(title: str, key: str) -> None:
        buckets: dict[str, list[dict]] = collections.defaultdict(list)
        for row in rows:
            buckets[str(row[key])].append(row)
        print(f"\n--- {title}")
        print(head)
        for name in sorted(buckets, key=str):
            print(f"{name:<22}" + stats(buckets[name]))

    for row in rows:
        rr = row["rr"] or 0
        row["rr_bucket"] = "<1.5" if rr < 1.5 else "1.5-2" if rr < 2 else "2-3" if rr < 3 else ">=3"
        block = row["block"]
        row["block_bucket"] = "n/a" if block is None else (
            "<0.5" if block < 0.5 else "0.5-1" if block < 1 else ">=1"
        )

    table("by R:R", "rr_bucket")
    table("by grade", "grade")
    table("by block thickness (ATR)", "block_bucket")
    table("by trend alignment at formation", "aligned")
    table("by pattern kind", "kind")

    print("\n--- filters the page offers, measured")
    print(head)
    for label, predicate in (
        ("sniper (R:R >= 1.5)", lambda r: (r["rr"] or 0) >= 1.5),
        ("sniper + algo candle", lambda r: (r["rr"] or 0) >= 1.5 and "algo" in (r["kind"] or "")),
        ("sniper + A+ / A", lambda r: (r["rr"] or 0) >= 1.5 and r["grade"] in ("A+", "A")),
        ("A+ / A only", lambda r: r["grade"] in ("A+", "A")),
        ("A+ / A and R:R >= 2", lambda r: r["grade"] in ("A+", "A") and (r["rr"] or 0) >= 2),
        ("A+ / A, R:R >= 2, block >= 1", lambda r: r["grade"] in ("A+", "A") and (r["rr"] or 0) >= 2 and (r["block"] or 0) >= 1),
        ("... and trend aligned", lambda r: r["grade"] in ("A+", "A") and (r["rr"] or 0) >= 2 and (r["block"] or 0) >= 1 and r["aligned"]),
        ("... and confluence", lambda r: r["grade"] in ("A+", "A") and (r["rr"] or 0) >= 2 and (r["block"] or 0) >= 1 and r["aligned"] and r["confluence"]),
    ):
        print(f"{label:<22}" + stats([r for r in rows if predicate(r)]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
