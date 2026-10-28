#!/usr/bin/env python3
"""Bands: turn the model into a signal list with *measured* accuracy per band.

For the geometry the walk-forward validated (target 2 ATR / stop 4 ATR, i.e.
0.5 R:R) this script:

  1. trains the model on expanding windows and scores the next block of years,
  2. measures, per probability band, the realised win rate with a 95 % Wilson
     interval and the expectancy after fees - on out-of-sample rows only,
  3. trains one final model on all pooled data (both symbols, both timeframes)
     and saves it for the app, together with the band table.

Usage
-----
    python research/06_bands.py --out-model app/model.pkl --out app/data/bands.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

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
CUTS = [0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.60, 0.00]


def wilson(w, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = w / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - r) / d, (c + r) / d)


def thin(fires, lockout):
    """Greedy non-overlapping selection: one open trade per stream at a time.

    Without this, 60 consecutive firing bars are 60 rows but roughly one trade:
    the win rate is still unbiased, but n and the confidence interval lie.
    """
    out = np.zeros(len(fires), dtype=bool)
    i = 0
    n = len(fires)
    while i < n:
        if fires[i]:
            out[i] = True
            i += lockout
        else:
            i += 1
    return out


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
    ok = np.isfinite(F).all(axis=1) & (out != 0)
    return dict(s=s, F=F, names=names, out=out, atr_frac=atr_frac, ok=ok)


def fit(F, out, rows):
    m = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.06, max_leaf_nodes=31,
                                       min_samples_leaf=200, l2_regularization=1.0,
                                       early_stopping=False, random_state=0)
    m.fit(F[rows], (out[rows] == 1).astype(int))
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--tfs", default="4h,1h")
    ap.add_argument("--target", type=float, default=2.0)
    ap.add_argument("--stop", type=float, default=4.0)
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--out-model", default="app/model.pkl")
    ap.add_argument("--out", default="app/data/bands.json")
    args = ap.parse_args()
    k = args.target / args.stop

    pooled = {c: dict(w=0, n=0, w_fold={}, n_fold={}) for c in CUTS}
    per_tf = {}
    final = {}
    for tf in args.tfs.split(","):
        per_tf[tf] = {}
        parts = []
        for sym in args.symbols.split(","):
            d = load(sym, tf, args.target, args.stop, args.horizon)
            fee_r = FEE_RT / (args.stop * float(np.nanmedian(d["atr_frac"][d["ok"]])))
            d["fee_r"] = fee_r
            d["symbol"] = sym
            parts.append(d)
            print(f"{sym} {tf}: {int(d['ok'].sum()):,} resolved bars, fee {fee_r:.3f} R")

            for label, a, b in FOLDS:
                tr = d["ok"] & (d["s"].t * 60000 < a)
                te = d["ok"] & (d["s"].t * 60000 >= a) & (d["s"].t * 60000 < b)
                if tr.sum() < 5000 or te.sum() < 200:
                    continue
                m = fit(d["F"], d["out"], tr)
                p = m.predict_proba(d["F"][te])[:, 1]
                y = (d["out"][te] == 1).astype(int)
                for c in CUTS:
                    sel = thin(p >= c, args.horizon)
                    if sel.sum() < 20:
                        continue
                    pooled[c]["w"] += int(y[sel].sum())
                    pooled[c]["n"] += int(sel.sum())
                    pooled[c]["w_fold"][label] = pooled[c]["w_fold"].get(label, 0) + int(y[sel].sum())
                    pooled[c]["n_fold"][label] = pooled[c]["n_fold"].get(label, 0) + int(sel.sum())
                    key = f"{sym} {tf} {label}"
                    per_tf[tf].setdefault(c, {})[key] = dict(
                        n=int(sel.sum()), win=float(y[sel].mean()))
                    pooled[c].setdefault("days", 0)
                    pooled[c]["days"] += len(d["s"]) / (24 * (60 if tf == "1h" else 240))
        F_all = np.concatenate([d["F"] for d in parts])
        out_all = np.concatenate([d["out"] for d in parts])
        ok_all = np.concatenate([d["ok"] for d in parts])
        t_all = np.concatenate([d["s"].t for d in parts])
        before_2024 = t_all < (1704067200000 // 60000)
        final[tf] = {"parts": parts, "names": parts[0]["names"],
                     "fee_r": float(np.mean([d["fee_r"] for d in parts])),
                     # production model: everything we have
                     "model": fit(F_all, out_all, ok_all),
                     # validation model: only what existed before 2024, so that a
                     # replay of 2024+ is genuinely out of sample
                     "model_oos": fit(F_all, out_all, ok_all & before_2024)}

    print(f"\n=== measured accuracy by band, pooled out-of-sample, "
          f"target {args.target} / stop {args.stop} ATR ({k:.2f} R:R) ===")
    print(f"{'cut':>6} {'signals':>9} {'win %':>7} {'95 % CI':>16} {'EV pre':>8} "
          f"{'EV post':>8}   folds (win % / n)")
    bands = []
    for c in CUTS:
        w, n = pooled[c]["w"], pooled[c]["n"]
        if n < 50:
            continue
        win = w / n
        lo, hi = wilson(w, n)
        fee_r = float(np.mean([final[tf]["fee_r"] for tf in final]))
        ev = win * k - (1 - win)
        folds = " ".join(f"{lb}:{100*pooled[c]['w_fold'][lb]/max(pooled[c]['n_fold'][lb],1):.0f}%"
                         f"/{pooled[c]['n_fold'][lb]}" for lb in pooled[c]["n_fold"])
        print(f"{c:>6.2f} {n:>9,} {100*win:>7.2f} [{100*lo:5.1f}-{100*hi:5.1f}] {ev:>+8.3f} "
              f"{ev-fee_r:>+8.3f}   {folds}")
        bands.append(dict(cut=c, n=n, wins=w, win=win, ci=[lo, hi], rr=k,
                          ev_pre=ev, ev_post=ev - fee_r,
                          folds={lb: dict(n=pooled[c]["n_fold"][lb],
                                          win=pooled[c]["w_fold"][lb] / max(pooled[c]["n_fold"][lb], 1))
                                 for lb in pooled[c]["n_fold"]}))

    os.makedirs(os.path.dirname(os.path.abspath(args.out_model)), exist_ok=True)
    import joblib
    joblib.dump({"models": {tf: final[tf]["model"] for tf in final},
                 "models_oos": {tf: final[tf]["model_oos"] for tf in final},
                 "oos_trained_until": "2023-12-31",
                 "features": final[list(final)[0]]["names"],
                 "tfs": list(final), "target": args.target, "stop": args.stop,
                 "horizon": args.horizon, "fee_rt": FEE_RT,
                 "trained_rows": {tf: int(sum(d["ok"].sum() for d in final[tf]["parts"])) for tf in final}},
                args.out_model)
    print(f"\nsaved model -> {args.out_model}")

    out = dict(target=args.target, stop=args.stop, horizon=args.horizon, rr=k,
               fee_bp=FEE_RT * 1e4, bands=bands, folds=[f[0] for f in FOLDS],
               symbols=args.symbols.split(","), tfs=list(final),
               per_tf={tf: {str(c): v for c, v in per_tf[tf].items()} for tf in per_tf})
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=1)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
