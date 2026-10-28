#!/usr/bin/env python3
"""The 100 % hunt.

Question: can a search over *this much* data find a pattern that wins every
single time it fires?  Answer, measured: yes — hundreds of them, in sample.

Method
------
1. Build ~59 causal features at one timeframe and label every bar by what
   happened after it with a fixed barrier geometry (target/stop in ATR).
2. Discretise every feature into 8 quantile bins, using **in-sample rows only**.
3. Score every conjunction of two conditions exactly (a 2-D bincount per
   feature pair — the full pairwise space, no sampling, no randomness).
4. Call a cell a *perfect rule* if it has at least ``--min-n`` resolved trades
   and **zero** losses in the in-sample period.
5. Then take those perfect rules and score them on data the search never saw:
   the years after 2024, and a different asset (ETH).

Usage
-----
    python research/02_hunt.py --symbol BTCUSDT --tf 1h
    python research/02_hunt.py --symbol BTCUSDT --tf 1m --max-features 26
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lab import data, features, outcomes, search  # noqa: E402

IS_END_MS = 1704067200000  # 2024-01-01 UTC
FEE_RT = 0.0005            # 5 bp round trip: MEXC futures taker 2 bp x2 + 1 bp slippage


def fmt_cond(names, edges, j, b):
    e = edges[j]
    lo = "-inf" if b == 0 else f"{e[b-1]:.4g}"
    hi = "+inf" if b >= len(e) else f"{e[b]:.4g}"
    return f"{names[j]} in [{lo}, {hi})"


def build(symbol, tf, target_frac, stop_frac, horizon, max_features=0):
    s = data.load(symbol, tf)
    f = features.features(s)
    names = sorted(f)
    if max_features and len(names) > max_features:
        # keep a deterministic spread of features (every k-th name) so the
        # search space shrinks without cherry-picking
        step = len(names) / max_features
        names = [names[int(i * step)] for i in range(max_features)]
    F = np.stack([f[n] for n in names], axis=1)
    atr = outcomes.atr_series(s.h, s.l, s.c)
    res = outcomes.barrier(s, atr, target_frac, stop_frac, horizon, "long")
    # +1/-1 outcomes live on the signal bar's index
    out = np.zeros(len(s), dtype=np.int8)
    out[res["sig"]] = res["out"]
    atr = np.r_[atr, np.nan][: len(s)]
    is_mask = s.t < (IS_END_MS // 60000)
    is_mask = is_mask & (np.arange(len(s)) < len(res["sig"]) + 1)
    edges = search.bin_edges(F, is_mask & np.isfinite(F).all(axis=1))
    B = search.apply_bins(F, edges)
    win = (out == 1)
    loss = (out == -1)
    resolved = win | loss
    return dict(s=s, names=names, edges=edges, B=B, win=win, loss=loss, resolved=resolved,
                is_mask=is_mask, atr_frac=atr / s.c)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--target", type=float, default=0.5)
    ap.add_argument("--stop", type=float, default=1.0)
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--min-n", type=int, default=50)
    ap.add_argument("--max-features", type=int, default=0)
    ap.add_argument("--oos-symbol", default="ETHUSDT")
    ap.add_argument("--out", default="results/hunt.json")
    args = ap.parse_args()

    t0 = time.time()
    print(f"=== the 100 % hunt: {args.symbol} {args.tf}  "
          f"target {args.target} ATR / stop {args.stop} ATR / {args.horizon} bars ===")
    d = build(args.symbol, args.tf, args.target, args.stop, args.horizon, args.max_features)
    s, names, edges, B = d["s"], d["names"], d["edges"], d["B"]
    win, loss = d["win"], d["loss"]
    keep = d["is_mask"] & d["resolved"]
    print(f"bars {len(s):,} | features {len(names)} | bins 8 | in-sample resolved rows {int(keep.sum()):,}"
          f" | build {time.time()-t0:.1f}s")
    base = win[keep].sum() / max(keep.sum(), 1)
    print(f"base win rate in sample: {100*base:.2f} %")

    t1 = time.time()
    scan = search.pairwise_scan(B, win, loss, names, min_n=args.min_n, rows_mask=d["is_mask"])
    cells = len(scan["n"])
    perfect = scan["losses"] == 0
    print(f"pairwise cells with n >= {args.min_n}: {cells:,} | scored in {time.time()-t1:.1f}s")
    print(f"cells that NEVER lost in sample: {int(perfect.sum()):,} "
          f"({100*perfect.sum()/max(cells,1):.1f} % of cells)")
    # what chance alone predicts: a cell of n trades is perfect with prob (1-p)^n
    null = float(np.mean((1 - base) ** scan["n"]))
    print(f"expected fraction by chance alone: {100*null:.1f} %")

    # ---- out-of-sample ---------------------------------------------------------
    oos_rows = (~d["is_mask"]) & d["resolved"]
    idx = np.flatnonzero(perfect)
    idx = idx[np.argsort(-scan["n"][idx])][:200]  # the 200 biggest perfect rules
    rows = []
    for i in idx:
        conds = [(int(scan["fi"][i]), int(scan["bi"][i])), (int(scan["fj"][i]), int(scan["bj"][i]))]
        oos = search.evaluate(B, win, loss, conds, oos_rows)
        rows.append({"conds": [fmt_cond(names, edges, j, b) for j, b in conds],
                     "is_n": int(scan["n"][i]), "is_wr": float(scan["winrate"][i]),
                     "is_w": int(scan["wins"][i]),
                     "oos_n": oos["n"], "oos_wr": oos["winrate"], "oos_w": oos["wins"],
                     "oos_l": oos["losses"]})
    tested = [r for r in rows if r["oos_n"] >= 10]
    if tested:
        wr = np.array([r["oos_wr"] for r in tested])
        survived = int(sum(1 for r in tested if r["oos_wr"] == 1.0))
        print(f"\nof the {len(tested)} biggest perfect rules rescorable out of sample:")
        print(f"  still 100 % out of sample : {survived} ({100*survived/len(tested):.1f} %)")
        print(f"  median out-of-sample win  : {100*np.median(wr):.1f} %  (base rate {100*base:.1f} %)")
        print(f"  worst                     : {100*wr.min():.1f} %")
        for r in sorted(tested, key=lambda r: -r["oos_n"])[:8]:
            print(f"    IS {r['is_w']}/{r['is_n']:<4d}  ->  OOS {r['oos_w']}/{r['oos_n']:<4d} "
                  f"({100*r['oos_wr']:.1f} %)   {' AND '.join(r['conds'])}")

    # ---- expectancy of the perfect rules after fees ----------------------------
    atr_frac = np.nanmedian(d["atr_frac"][keep])
    fee_r = FEE_RT / (args.stop * atr_frac)
    print(f"\nfee drag at a {args.stop}-ATR stop: {fee_r:.3f} R per trade "
          f"(5 bp round trip, ATR is {100*atr_frac:.2f} % of price)")
    if tested:
        r = args.target / args.stop
        ev_pre = np.mean([(x["oos_wr"] * r - (1 - x["oos_wr"])) for x in tested])
        print(f"out-of-sample expectancy of the perfect rules: {ev_pre:+.3f} R pre-fee, "
              f"{ev_pre - fee_r:+.3f} R after fees")

    # ---- a different asset ------------------------------------------------------
    d2 = build(args.oos_symbol, args.tf, args.target, args.stop, args.horizon, args.max_features)
    # rebuild bins for ETH with BTC's edges so the rule text stays identical
    B2 = search.apply_bins(np.stack([features.features(d2["s"])[n] for n in names], axis=1), edges)
    rows2 = d2["resolved"]
    eth = []
    for i in idx[:100]:
        conds = [(int(scan["fi"][i]), int(scan["bi"][i])), (int(scan["fj"][i]), int(scan["bj"][i]))]
        e = search.evaluate(B2, d2["win"], d2["loss"], conds, rows2)
        if e["n"] >= 10:
            eth.append(e)
    if eth:
        wr = np.array([e["winrate"] for e in eth])
        print(f"\non {args.oos_symbol} (a different asset, same rules): "
              f"{len(eth)} rules scorable, {int((wr==1.0).sum())} still 100 %, "
              f"median {100*np.median(wr):.1f} %")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"symbol": args.symbol, "tf": args.tf, "target": args.target,
                   "stop": args.stop, "horizon": args.horizon, "min_n": args.min_n,
                   "bars": len(s), "features": len(names), "cells": cells,
                   "perfect_cells": int(perfect.sum()), "base_win": base,
                   "null_fraction": null, "fee_r": fee_r,
                   "rules": rows, "eth": eth}, fh, indent=1)
    print(f"\nwrote {args.out}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
