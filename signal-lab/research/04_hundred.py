#!/usr/bin/env python3
"""The 100 % club: mine perfect rules, then try to break them.

This is the experiment the brief asks for, run as literally as possible:

  * search a large space of conjunctions of causal features,
  * keep the ones that won **every** time in sample,
  * then score exactly those rules on data the search never touched
    (the years after 2024) and on a second asset (ETH),
  * and finally price them: a rule that trades a 0.05-ATR target for a 4-ATR
    stop is risking 80 R to make 1 R, so a single loss erases 80 wins - and the
    exchange fee is charged per trade regardless of which barrier hits.

Usage
-----
    python research/04_hundred.py --tf 1h --target 0.05 --stop 4
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lab import data, search  # noqa: E402
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("hunt", os.path.join(os.path.dirname(__file__), "02_hunt.py"))
hunt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hunt)

MIN_NS = (10, 50, 200, 500)


def describe(conds, names, edges):
    return " AND ".join(hunt.fmt_cond(names, edges, j, b) for j, b in conds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--x-symbol", default="ETHUSDT")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--target", type=float, default=0.05)
    ap.add_argument("--stop", type=float, default=4.0)
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--width", type=int, default=120)
    ap.add_argument("--depth", type=int, default=5)
    ap.add_argument("--fee-bp", type=float, default=5.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    t0 = time.time()
    d = hunt.build(args.symbol, args.tf, args.target, args.stop, args.horizon)
    names, edges, B = d["names"], d["edges"], d["B"]
    win, loss = d["win"], d["loss"]
    is_rows = d["is_mask"] & d["resolved"]
    oos_rows = (~d["is_mask"]) & d["resolved"]
    base_is = float(win[is_rows].sum() / is_rows.sum())
    base_oos = float(win[oos_rows].sum() / oos_rows.sum())
    k = args.target / args.stop
    atr_frac = float(np.nanmedian(d["atr_frac"][is_rows]))
    fee_r = (args.fee_bp / 1e4) / (args.stop * atr_frac)

    print(f"=== 100 % club: {args.symbol} {args.tf}  target {args.target} ATR  stop {args.stop} ATR "
          f"({k:.4f} R:R)  horizon {args.horizon} bars ===")
    print(f"in-sample {int(is_rows.sum()):,} resolved | out-of-sample {int(oos_rows.sum()):,} resolved")
    print(f"base win rate: in sample {100*base_is:.2f} %  out of sample {100*base_oos:.2f} %")
    print(f"fee: {args.fee_bp:.0f} bp round trip = {fee_r:.4f} R at this stop "
          f"(a winning trade is worth {k:.4f} R) -> fee is {100*fee_r/k:.0f} % of the prize")

    levels = {}
    for mn in MIN_NS:
        t = time.time()
        rules = search.beam_search(B, win, loss, is_rows, min_n=mn, depth=args.depth, width=args.width)
        perfect = [r for r in rules if r["losses"] == 0]
        best = rules[0] if rules else None
        levels[mn] = {
            "rules_found": len(rules),
            "perfect": len(perfect),
            "best_winrate": float(best["winrate"]) if best else None,
            "best_n": int(best["n"]) if best else None,
            "biggest_perfect_n": int(max((r["n"] for r in perfect), default=0)),
        }
        print(f"  min {mn:>4} trades: {len(perfect):>4} rules never lost in sample "
              f"(biggest {levels[mn]['biggest_perfect_n']} trades) | "
              f"best win rate overall {100*levels[mn]['best_winrate']:.2f} % on n={levels[mn]['best_n']} "
              f"[{time.time()-t:.0f}s]"
              if best else f"  min {mn}: no rules")

    # ---- take the perfect rules and score them where it counts ------------------
    rules = search.beam_search(B, win, loss, is_rows, min_n=50, depth=args.depth, width=args.width)
    perfect = [r for r in rules if r["losses"] == 0]

    # ETH, same bin edges so the rule text is identical
    s2 = data.load(args.x_symbol, args.tf)
    f2 = hunt.features.features(s2)
    F2 = np.stack([f2[n] for n in names], axis=1)
    B2 = search.apply_bins(F2, edges)
    from lab import outcomes as O
    atr2 = O.atr_series(s2.h, s2.l, s2.c)
    res2 = O.barrier(s2, atr2, args.target, args.stop, args.horizon, "long")
    out2 = np.zeros(len(s2), dtype=np.int8)
    out2[res2["sig"]] = res2["out"]
    win2, loss2 = out2 == 1, out2 == -1
    rows2 = win2 | loss2

    records = []
    for r in perfect:
        e_is = search.evaluate(B, win, loss, r["conds"], is_rows)
        e_oos = search.evaluate(B, win, loss, r["conds"], oos_rows)
        e_eth = search.evaluate(B2, win2, loss2, r["conds"], rows2)
        records.append({
            "rule": describe(r["conds"], names, edges),
            "conds": [[int(j), int(b)] for j, b in r["conds"]],
            "is_n": e_is["n"], "is_wr": e_is["winrate"],
            "oos_n": e_oos["n"], "oos_wr": e_oos["winrate"], "oos_w": e_oos["wins"],
            "x_n": e_eth["n"], "x_wr": e_eth["winrate"], "x_w": e_eth["wins"],
        })
    records.sort(key=lambda r: -r["is_n"])
    scored = [r for r in records if r["oos_n"] >= 1]
    if scored:
        wr = np.array([r["oos_wr"] for r in scored])
        still = int((wr == 1.0).sum())
        total_losses = int(sum(r["oos_n"] - r["oos_w"] for r in scored))
        print(f"\n{len(perfect)} rules never lost in sample (n>=50). Scored after 2024:")
        print(f"  still perfect        : {still} of {len(scored)} ({100*still/len(scored):.1f} %)")
        print(f"  their losses add up  : {total_losses} trades lost after 2024")
        print(f"  median out-of-sample : {100*np.median(wr):.2f} %  (vs {100*base_oos:.2f} % base)")
        # pooled expectancy if you traded every firing of every one of them
        n_all = sum(r["oos_n"] for r in scored)
        w_all = sum(r["oos_w"] for r in scored)
        p_all = w_all / max(n_all, 1)
        ev = p_all * k - (1 - p_all)
        print(f"  pooled out of sample : {w_all:,}/{n_all:,} = {100*p_all:.2f} %  "
              f"-> {ev:+.4f} R pre-fee, {ev-fee_r:+.4f} R after fees")
        print(f"  and at a perfect 100 % win rate it would still be {k-fee_r:+.4f} R per trade")

    out = args.out or os.path.join("results", f"hundred_{args.tf}_{args.target}_{args.stop}.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"symbol": args.symbol, "x_symbol": args.x_symbol, "tf": args.tf,
                   "target": args.target, "stop": args.stop, "horizon": args.horizon,
                   "rr": k, "fee_r": fee_r, "fee_bp": args.fee_bp,
                   "base_is": base_is, "base_oos": base_oos,
                   "is_rows": int(is_rows.sum()), "oos_rows": int(oos_rows.sum()),
                   "levels": {str(k2): v for k2, v in levels.items()},
                   "rules": records[:400],
                   "features": names,
                   "edges": [list(map(float, e)) for e in edges]}, fh, indent=1)
    print(f"\nwrote {out}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
