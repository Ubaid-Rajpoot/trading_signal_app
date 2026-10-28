#!/usr/bin/env python3
"""Does *any* of this predict the future, once fees are paid?

Trains a gradient-boosted model on 2017-2023 (BTC), then scores 2024-2026
(BTC) and the same rules on ETH - a different asset, never trained on. Reports
AUC, calibration, and the only table that decides whether a signal system is
worth running: **expectancy after fees, by how selective you are**.

Usage
-----
    python research/03_model.py --tf 1h --target 1 --stop 1
    python research/03_model.py --tf 15m --target 0.5 --stop 1 --save app/model.pkl
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

IS_END_MS = hunt.IS_END_MS
FEE_RT = hunt.FEE_RT


def dataset(symbol, tf, target, stop, horizon):
    s = data.load(symbol, tf)
    f = hunt.features.features(s)
    names = sorted(f)
    F = np.stack([f[n] for n in names], axis=1)
    atr = outcomes.atr_series(s.h, s.l, s.c)
    res = outcomes.barrier(s, atr, target, stop, horizon, "long")
    out = np.zeros(len(s), dtype=np.int8)
    out[res["sig"]] = res["out"]
    atr_frac = np.r_[atr / s.c, np.nan][: len(s)]
    return dict(s=s, F=F, names=names, out=out, atr_frac=atr_frac,
                is_mask=s.t < (IS_END_MS // 60000))


def table(y, p, k, fee_r, label, cuts=(0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 1.0)):
    """Win rate / expectancy for the most confident (100*cut) % of signals."""
    print(f"\n  {label}: n={len(y):,}  base win {100*y.mean():.2f} %")
    order = np.argsort(-p)
    out = []
    for c in cuts:
        m = int(len(y) * c)
        if m < 30:
            continue
        sel = order[:m]
        w = float(y[sel].mean())
        ev = w * k - (1 - w)
        row = dict(select=c, n=m, win=w, ev_pre=ev, ev_post=ev - fee_r)
        out.append(row)
        print(f"    top {100*c:6.1f}%  n={m:>7,}  win {100*w:6.2f} %  "
              f"EV {ev:+.3f} R pre-fee -> {ev-fee_r:+.3f} R after fees")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--x-symbol", default="ETHUSDT")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--target", type=float, default=1.0)
    ap.add_argument("--stop", type=float, default=1.0)
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--save", default="")
    ap.add_argument("--out", default="results/model.json")
    args = ap.parse_args()
    k = args.target / args.stop

    t0 = time.time()
    d = dataset(args.symbol, args.tf, args.target, args.stop, args.horizon)
    F, y_all = d["F"], d["out"]
    is_mask = d["is_mask"]
    ok = np.isfinite(F).all(axis=1) & (y_all != 0)
    tr = ok & is_mask
    te = ok & ~is_mask
    print(f"=== {args.symbol} {args.tf}: target {args.target} / stop {args.stop} ATR "
          f"({k:.2f} R:R), {args.horizon} bars ===")
    print(f"train {int(tr.sum()):,} rows (<=2023)   test {int(te.sum()):,} rows (2024-)   "
          f"({time.time()-t0:.0f}s to build)")

    fee_r = FEE_RT / (args.stop * float(np.nanmedian(d["atr_frac"][ok])))
    model = HistGradientBoostingClassifier(
        max_iter=400, learning_rate=0.06, max_leaf_nodes=63, min_samples_leaf=200,
        l2_regularization=1.0, early_stopping=False, random_state=0)
    model.fit(F[tr], (y_all[tr] == 1).astype(int))
    p_tr = model.predict_proba(F[tr])[:, 1]
    p_te = model.predict_proba(F[te])[:, 1]
    y_tr = (y_all[tr] == 1).astype(int)
    y_te = (y_all[te] == 1).astype(int)
    auc_tr = roc_auc_score(y_tr, p_tr)
    auc_te = roc_auc_score(y_te, p_te)
    print(f"AUC in sample {auc_tr:.4f} | out of sample {auc_te:.4f}")

    # calibration on the test years
    print("\n  calibration out of sample (predicted -> actual):")
    bins = [0, .4, .5, .6, .7, .8, .9, 1.01]
    cal = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (p_te >= lo) & (p_te < hi)
        if m.sum() < 100:
            continue
        cal.append(dict(lo=lo, hi=hi, n=int(m.sum()), pred=float(p_te[m].mean()),
                        act=float(y_te[m].mean())))
        print(f"    {lo:.2f}-{hi:.2f}  n={int(m.sum()):>7,}  predicted {100*p_te[m].mean():5.1f} %  "
              f"actual {100*y_te[m].mean():5.1f} %  "
              f"net {100*(y_te[m].mean()*k-(1-y_te[m].mean())):+.1f} R per 100")

    rows = table(y_te, p_te, k, fee_r, "out of sample, by confidence")

    # same model, different asset -- and only the years the training never saw:
    # scoring ETH over 2018-2023 would leak *era*, not asset
    d2 = dataset(args.x_symbol, args.tf, args.target, args.stop, args.horizon)
    ok2 = np.isfinite(d2["F"]).all(axis=1) & (d2["out"] != 0) & (~d2["is_mask"])
    fee_r2 = FEE_RT / (args.stop * float(np.nanmedian(d2["atr_frac"][ok2])))
    p2 = model.predict_proba(d2["F"][ok2])[:, 1]
    y2 = (d2["out"][ok2] == 1).astype(int)
    print(f"\nAUC on {args.x_symbol} (never trained on): "
          f"{roc_auc_score(y2, p2):.4f}")
    x_rows = table(y2, p2, k, fee_r2, f"{args.x_symbol}, same model")

    best = max(rows, key=lambda r: r["ev_post"]) if rows else None
    print(f"\nbest out-of-sample expectancy here: "
          f"{best['ev_post']:+.3f} R at top {100*best['select']:.1f} % of signals "
          f"({best['n']:,} trades, win {100*best['win']:.1f} %)" if best else "no rows")

    if args.save:
        import joblib
        joblib.dump({"model": model, "features": d["names"], "tf": args.tf,
                     "target": args.target, "stop": args.stop, "horizon": args.horizon,
                     "fee_rt": FEE_RT, "auc": auc_te,
                     "trained_rows": int(tr.sum())}, args.save)
        print(f"saved model -> {args.save}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(dict(symbol=args.symbol, tf=args.tf, target=args.target, stop=args.stop,
                       horizon=args.horizon, auc_in=auc_tr, auc_out=auc_te,
                       calibration=cal, by_confidence=rows, cross=x_rows, fee_r=fee_r), fh, indent=1)


if __name__ == "__main__":
    main()
