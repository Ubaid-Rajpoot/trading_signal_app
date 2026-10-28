#!/usr/bin/env python3
"""The accuracy frontier, and where the fee line cuts it.

For every timeframe and a grid of target/stop geometries this measures, on the
whole archive:

    win rate               = P(target touched before stop)
    expectancy pre-fee     = p * (target/stop) - (1-p)
    expectancy after fees  = the same, minus the round-trip cost expressed in R

The output answers the only question that matters for a "98-100 % accuracy"
system: at what accuracy does the trade stop paying for itself?

Usage
-----
    python research/01_frontier.py                      # every timeframe
    python research/01_frontier.py --tf 1h --symbols BTCUSDT,ETHUSDT
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lab import data, outcomes  # noqa: E402

TARGETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0)
STOPS = (0.5, 1.0, 2.0, 4.0)
TFS = ("1m", "5m", "15m", "1h", "4h")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--tf", default=",".join(TFS))
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--fee-bp", type=float, default=5.0)
    ap.add_argument("--out", default="results/frontier.json")
    args = ap.parse_args()

    fee = args.fee_bp / 1e4
    rows = []
    for sym in args.symbols.split(","):
        for tf in args.tf.split(","):
            t0 = time.time()
            s = data.load(sym, tf)
            atr = outcomes.atr_series(s.h, s.l, s.c)
            atr_frac = np.nanmedian(atr[len(s) // 2:] / s.c[len(s) // 2:])
            for tg in TARGETS:
                for sf in STOPS:
                    r = outcomes.barrier(s, atr, tg, sf, args.horizon, "long")["out"]
                    n = int((r != 0).sum())
                    if n < 100:
                        continue
                    p = float((r == 1).sum() / n)
                    k = tg / sf
                    ev = p * k - (1 - p)
                    fee_r = fee / (sf * atr_frac)
                    rows.append(dict(symbol=sym, tf=tf, target=tg, stop=sf, rr=k, n=n,
                                     win=p, ev_pre=ev, ev_post=ev - fee_r, fee_r=fee_r,
                                     atr_frac=atr_frac, timeout=float((r == 0).mean())))
            print(f"{sym} {tf}: {len(s):>8,} bars, ATR {100*atr_frac:.2f} % of price  "
                  f"({time.time()-t0:.0f}s)")

    print(f"\n=== accuracy frontier, {args.horizon}-bar horizon, {args.fee_bp:.0f} bp round trip ===")
    print(f"{'tf':>4} {'target':>7} {'stop':>5} {'R:R':>7} {'n':>8} {'win %':>7} "
          f"{'EV pre':>8} {'fee(R)':>7} {'EV post':>8}")
    for r in rows:
        if r["symbol"] != "BTCUSDT":
            continue
        print(f"{r['tf']:>4} {r['target']:>7.2f} {r['stop']:>5.2f} {r['rr']:>7.3f} {r['n']:>8,} "
              f"{100*r['win']:>7.2f} {r['ev_pre']:>+8.3f} {r['fee_r']:>7.3f} {r['ev_post']:>+8.3f}")

    hi = [r for r in rows if r["win"] >= 0.98]
    pos = [r for r in rows if r["ev_post"] > 0]
    print(f"\nrows with >= 98 % accuracy: {len(hi)}")
    if hi:
        best = min(hi, key=lambda r: -r["ev_post"])
        print(f"  the most profitable of them still yields {best['ev_post']:+.4f} R/trade "
              f"after fees ({best['symbol']} {best['tf']} target {best['target']} stop {best['stop']})")
        print(f"  accuracy >= 98 %% rows with positive net expectancy: "
              f"{sum(1 for r in hi if r['ev_post'] > 0)}")
    print(f"rows with positive expectancy after fees: {len(pos)} of {len(rows)}"
          f"  (accuracy range {100*min(r['win'] for r in pos):.1f}-{100*max(r['win'] for r in pos):.1f} % "
          f"if any)" if pos else f"rows with positive expectancy after fees: 0 of {len(rows)}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(rows, fh, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
