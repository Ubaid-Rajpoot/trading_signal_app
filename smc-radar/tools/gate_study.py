#!/usr/bin/env python3
"""Does the structure gate earn its place? Measured, on data the model never saw.

The user's report: *"even when the market is ranging you still give signals, and
you claim 96 % ... I followed one and my account was wiped out."* Two claims are
being tested here, separately:

1. **the gate** - of the setups the shipped model calls A (p >= 0.85) and the fee
   leaves tradable, how do the ones with a trending structure on this timeframe
   *and* the one above (and a premium/discount location) compare with the ones
   without it?
2. **the arithmetic** - a 97 % hit rate is not the same as a safe trade: with the
   book's exit rule the target is the next liquidity pool and the stop is beyond
   the block, so a single stop can be worth several wins. This prints the payoff
   as well as the hit rate, because that is what emptied the account.

Everything is on the held-out 90-day block (``--file``) and every number here can
be reproduced:

    python tools/gate_study.py --file study-data/BTCUSDT-1m-90d-holdout.csv
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from types import SimpleNamespace  # noqa: E402

from bot.map import DEFAULTS  # noqa: E402
from bot.model import SignalModel  # noqa: E402
from tools.fit_model import TIMEFRAMES, rows_for, wilson  # noqa: E402


def band(rows: list[dict], probs: list[float], cut: float, gate_on: bool) -> dict:
    """A cut, the fee gate (0.30 R) and - optionally - the structure gate."""
    picks = [
        i for i, row in enumerate(rows)
        if probs[i] >= cut and row["fee_r"] <= 0.30 and (row["gate"] if gate_on else True)
    ]
    if not picks:
        return {"n": 0}
    wins = int(sum(rows[i]["win"] for i in picks))
    lo, hi = wilson(wins, len(picks))
    rr = statistics.median([rows[i]["rr"] for i in picks])
    net = sum(rows[i]["net"] for i in picks) / len(picks)
    return {
        "n": len(picks),
        "wins": wins,
        "win": wins / len(picks),
        "ci95": (lo, hi),
        "rr": rr,
        "fee_r": statistics.median([rows[i]["fee_r"] for i in picks]),
        "net": net,
    }


def line(label: str, s: dict, days: float) -> str:
    if not s["n"]:
        return f"{label:<34}{'0':>7}"
    return (
        f"{label:<34}{s['n']:>7}{s['n'] / days:>8.2f}{s['win']:>8.0%}"
        f"  ({s['ci95'][0]:>4.0%}-{s['ci95'][1]:>4.0%}){s['rr']:>7.2f}{s['net']:>+8.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="study-data/BTCUSDT-1m-90d-holdout.csv",
                    help="a block of real 1m candles the model was NOT fitted on")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframes", default=",".join(TIMEFRAMES))
    ap.add_argument("--days", type=float, default=90.0)
    ap.add_argument("--cut", type=float, default=0.85)
    args = ap.parse_args()
    tfs = tuple(t.strip() for t in args.timeframes.split(",") if t.strip())

    model = SignalModel()
    if not model.available:
        print(f"the model is not available: {model.problem}")
        return 1
    settings = dict(DEFAULTS)

    rows = rows_for(args.file, settings, args.symbol, tfs)
    # the model scores zone-like objects; a study row carries exactly the fields it
    # reads, so a namespace is enough (no fake attributes, no second code path)
    points = [
        model.probability(SimpleNamespace(
            conds=row["conds"], plan={"rr": row["rr"], "risk_pct": row["risk_pct"]},
            side=row["side"], kind=row["kind"], timeframe=row["tf"],
            block_atr=row["block_atr"],
        )) or 0.0
        for row in rows
    ]
    passed = sum(1 for row in rows if row["gate"])
    print(f"{Path(args.file).name}: {len(rows)} plans the map formed (no look-ahead), "
          f"{passed} of them ({passed / max(1, len(rows)):.1%}) pass the structure gate\n")

    print(f"{'group':<34}{'plans':>7}{'/day':>8}{'win':>8}   {'95% CI':>10}"
          f"{'R:R':>7}{'net R':>8}")
    for cut in (0.60, 0.75, 0.85, 0.90):
        print(line(f"p>={cut:.2f}, gate OFF", band(rows, points, cut, False), args.days))
        print(line(f"p>={cut:.2f}, gate ON", band(rows, points, cut, True), args.days))
    print()

    on = band(rows, points, args.cut, True)
    off = band(rows, points, args.cut, False)
    if on["n"] and off["n"]:
        print(f"at p>={args.cut}: the gate removes {off['n'] - on['n']} plans "
              f"({off['n']} -> {on['n']}), and moves the hit rate "
              f"{off['win']:.1%} -> {on['win']:.1%}, net {off['net']:+.2f} R -> {on['net']:+.2f} R per plan")
        print(f"  one stop costs {1 / on['rr']:.1f} wins at R:R {on['rr']:.2f}: "
              f"a {on['win']:.0%} hit rate is +{on['net']:.2f} R expectation, not a guarantee.\n")

    print("per timeframe, gate ON, at the cut:")
    for tf in tfs:
        idx = [i for i, row in enumerate(rows) if row["tf"] == tf]
        sub = [rows[i] for i in idx]
        probs = [points[i] for i in idx]
        s = band(sub, probs, args.cut, True)
        print(line(f"  {tf}", s, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
