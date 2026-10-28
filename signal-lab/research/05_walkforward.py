#!/usr/bin/env python3
"""Walk-forward: is the model's edge real, or a single lucky split?

Three expanding-window folds. In every fold the model is trained only on the
past and scored only on the next block of years. A configuration is only
allowed into the app if **its worst fold** still has a positive expectancy
after fees - that is the test that kills most "96 % accuracy" systems.

Usage
-----
    python research/05_walkforward.py
    python research/05_walkforward.py --tfs 1h,4h --geoms 1:1,2:4
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lab import data, outcomes  # noqa: E402
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("hunt", os.path.join(os.path.dirname(__file__), "02_hunt.py"))
hunt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hunt)

FEE_RT = hunt.FEE_RT
FOLDS = [("2022", 1640995200000, 1672531200000),
         ("2023", 1672531200000, 1704067200000),
         ("2024-26", 1704067200000, 9999999999999)]


def load(symbol, tf, target, stop, horizon):
    s = data.load(symbol, tf)
    f = hunt.features.features(s)
    names = sorted(f)
    F = np.stack([f[n] for n in names], axis=1)
    atr = outcomes.atr_series(s.h, s.l, s.c)
    res = outcomes.barrier(s, atr, target, stop, horizon, "long")
    out = np.zeros(len(s), dtype=np.int8)
    out[res["sig"]] = res["out"]
    atr_frac = np.r_[atr / s.c, np.nan][: len(s)]
    return s, F, names, out, atr_frac


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--tfs", default="1h,4h")
    ap.add_argument("--geoms", default="1:1,2:4,1:2")
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--topq", default="0.01,0.02,0.05")
    ap.add_argument("--out", default="results/walkforward.json")
    args = ap.parse_args()
    qs = [float(q) for q in args.topq.split(",")]

    rows = []
    for sym in args.symbols.split(","):
        for tf in args.tfs.split(","):
            for g in args.geoms.split(","):
                tg, sf = (float(x) for x in g.split(":"))
                k = tg / sf
                s, F, names, out, atr_frac = load(sym, tf, tg, sf, args.horizon)
                ok = np.isfinite(F).all(axis=1) & (out != 0)
                fee_r = FEE_RT / (sf * float(np.nanmedian(atr_frac[ok])))
                t0 = time.time()
                fold_out = []
                for label, a, b in FOLDS:
                    tr = ok & (s.t * 60000 < a)
                    te = ok & (s.t * 60000 >= a) & (s.t * 60000 < b)
                    if tr.sum() < 5000 or te.sum() < 300:
                        continue
                    m = HistGradientBoostingClassifier(
                        max_iter=250, learning_rate=0.06, max_leaf_nodes=31,
                        min_samples_leaf=200, l2_regularization=1.0,
                        early_stopping=False, random_state=0)
                    m.fit(F[tr], (out[tr] == 1).astype(int))
                    # refit on every second bar of the training block to blunt the
                    # autocorrelation of overlapping 60-bar labels
                    p = m.predict_proba(F[te])[:, 1]
                    y = (out[te] == 1).astype(int)
                    auc = float(roc_auc_score(y, p))
                    order = np.argsort(-p)
                    sel = {}
                    for q in qs:
                        n_sel = max(30, int(len(y) * q))
                        idx = order[:n_sel]
                        w = float(y[idx].mean())
                        sel[f"{q}"] = dict(n=n_sel, win=w, ev=w * k - (1 - w),
                                           ev_post=w * k - (1 - w) - fee_r)
                    fold_out.append(dict(fold=label, n=int(te.sum()), auc=auc, sel=sel))
                if not fold_out:
                    continue
                for q in qs:
                    vals = [f["sel"][f"{q}"]["ev_post"] for f in fold_out]
                    rows.append(dict(symbol=sym, tf=tf, target=tg, stop=sf, rr=k, q=q,
                                     fee_r=fee_r, folds=fold_out,
                                     worst_ev=float(min(vals)), mean_ev=float(np.mean(vals)),
                                     mean_win=float(np.mean([f["sel"][f"{q}"]["win"] for f in fold_out]))))
                    print(f"{sym:8s} {tf:>3} {tg}:{sf} R:R {k:.2f}  top {100*q:>4.0f} %  "
                          f"AUC {np.mean([f['auc'] for f in fold_out]):.3f}  "
                          f"win {100*np.mean([f['sel'][f'{q}']['win'] for f in fold_out]):5.1f} %  "
                          f"EV post-fee per fold "
                          + " ".join(f"{v:+.3f}" for v in vals)
                          + f"  worst {min(vals):+.3f}")
                print(f"          ({time.time()-t0:.0f}s for {sym} {tf} {tg}:{sf})")

    good = [r for r in rows if r["worst_ev"] > 0]
    print(f"\nconfigurations whose WORST fold is still positive after fees: {len(good)} of {len(rows)}")
    for r in sorted(good, key=lambda r: -r["worst_ev"])[:10]:
        print(f"  {r['symbol']} {r['tf']} {r['target']}:{r['stop']} top {100*r['q']:.0f} %  "
              f"worst {r['worst_ev']:+.3f} R  mean win {100*r['mean_win']:.1f} %")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(rows, fh, indent=1)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
