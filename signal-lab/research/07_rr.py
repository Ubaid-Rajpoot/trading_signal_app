#!/usr/bin/env python3
"""Reward:risk sweep -- the direction the brief actually points at.

Round 14 shipped a 1:2 reward:risk (target 2 ATR, stop 4 ATR). That is the wrong
end of the curve for a fee-paying trader: at 1:2 you need 67 % accuracy to break
even, and the model's ceiling was ~75 %, so almost the whole edge went to the
exchange.

At 1:4 or 1:5 the arithmetic flips. Break-even accuracy is

    p* = (1 + fee_in_R) / (1 + rr)

which is 20-25 % at 1:4 and 17-21 % at 1:5. The model does not need to be right
three times in four; it needs to be right *once in five*. That is a much easier
question for a weak-but-real edge, and it is where a small predictive edge turns
into money instead of noise.

This script measures, for a grid of (stop in ATR, reward:risk, timeframe, symbol):

  * the raw base rate and the break-even rate,
  * the model's accuracy at probability cuts, **non-overlapping** (one trade at a
    time), in three expanding-window folds,
  * expectancy after a 5 bp round trip, per fold, and the worst fold,
  * trades per day, and how often a trade neither hits the target nor the stop.

Usage
-----
    python research/07_rr.py                          # the default grid
    python research/07_rr.py --rrs 4,5 --stops 1,2
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
spec6 = importlib.util.spec_from_file_location("bands", os.path.join(os.path.dirname(__file__), "06_bands.py"))
bands_mod = importlib.util.module_from_spec(spec6)
spec6.loader.exec_module(bands_mod)

FEE_RT = hunt.FEE_RT
FOLDS = bands_mod.FOLDS
CUTS = (0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.33, 0.25)


def build(symbol, tf, stop_atr, rr, horizon):
    s = data.load(symbol, tf)
    f = hunt.features.features(s)
    names = sorted(f)
    F = np.stack([f[n] for n in names], axis=1)
    atr = outcomes.atr_series(s.h, s.l, s.c)
    res = outcomes.barrier(s, atr, rr * stop_atr, stop_atr, horizon, "long")
    out = np.zeros(len(s), dtype=np.int8)
    out[res["sig"]] = res["out"]
    atr_frac = np.r_[atr / s.c, np.nan][: len(s)]
    ok = np.isfinite(F).all(axis=1) & (out != 0)
    return dict(s=s, F=F, names=names, out=out, atr_frac=atr_frac, ok=ok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--tfs", default="1h,4h")
    ap.add_argument("--stops", default="0.5,1,2")
    ap.add_argument("--rrs", default="2,3,4,5,6")
    ap.add_argument("--horizon", type=int, default=240)
    ap.add_argument("--out", default="results/rr.json")
    args = ap.parse_args()
    stops = [float(x) for x in args.stops.split(",")]
    rrs = [float(x) for x in args.rrs.split(",")]

    rows = []
    for tf in args.tfs.split(","):
        for stop in stops:
            for rr in rrs:
                rec = dict(tf=tf, stop=stop, rr=rr, folds=[], ok=True)
                pooled = {c: dict(w=0, n=0, ev=[]) for c in CUTS}
                for sym in args.symbols.split(","):
                    d = build(sym, tf, stop, rr, args.horizon)
                    fee_r = FEE_RT / (stop * float(np.nanmedian(d["atr_frac"][d["ok"]])))
                    be = (1 + fee_r) / (1 + rr)
                    rec.setdefault("fee_r", []).append(fee_r)
                    rec.setdefault("be", []).append(be)
                    rec.setdefault("timeout", []).append(float((d["out"] == 0).mean()))
                    rec.setdefault("base", []).append(float((d["out"][d["ok"]] == 1).sum() / d["ok"].sum()))
                    for label, a, b in FOLDS:
                        tr = d["ok"] & (d["s"].t * 60000 < a)
                        te = d["ok"] & (d["s"].t * 60000 >= a) & (d["s"].t * 60000 < b)
                        if tr.sum() < 5000 or te.sum() < 300:
                            continue
                        m = bands_mod.fit(d["F"], d["out"], tr)
                        p = m.predict_proba(d["F"])[:, 1]
                        y = (d["out"] == 1)
                        for c in CUTS:
                            sel = bands_mod.thin((p >= c) & te, args.horizon)
                            if sel.sum() < 15:
                                continue
                            w = float(y[sel].mean())
                            pooled[c]["w"] += int(y[sel].sum())
                            pooled[c]["n"] += int(sel.sum())
                            pooled[c]["ev"].append(w * rr - (1 - w) - fee_r)
                if pooled[CUTS[0]]["n"] == 0 and all(v["n"] == 0 for v in pooled.values()):
                    continue
                rec["fee_r"] = float(np.mean(rec["fee_r"]))
                rec["be"] = float(np.mean(rec["be"]))
                rec["timeout"] = float(np.mean(rec["timeout"]))
                rec["base"] = float(np.mean(rec["base"]))
                rec["cuts"] = {}
                for c in CUTS:
                    v = pooled[c]
                    if v["n"] < 40:
                        continue
                    rec["cuts"][f"{c}"] = dict(n=v["n"], win=v["w"] / v["n"],
                                              folds=[float(x) for x in v["ev"]],
                                              worst=float(min(v["ev"])), mean=float(np.mean(v["ev"])))
                rows.append(rec)
                best = max(rec["cuts"].items(), key=lambda kv: kv[1]["worst"]) if rec["cuts"] else None
                if best:
                    c, v = best
                    print(f"{tf:>3} stop {stop:<4} 1:{rr:<3.0f}  base {100*rec['base']:5.1f} %  "
                          f"break-even {100*rec['be']:5.1f} %  fee {rec['fee_r']:.3f} R  "
                          f"timeout {100*rec['timeout']:4.1f} %  |  best cut {float(c):.2f}: "
                          f"{v['n']:>5} trades  win {100*v['win']:5.1f} %  "
                          f"EV/fold " + " ".join(f"{x:+.2f}" for x in v["folds"]) +
                          f"  worst {v['worst']:+.3f} R")
                else:
                    print(f"{tf:>3} stop {stop:<4} 1:{rr:<3.0f}  base {100*rec['base']:5.1f} %  "
                          f"break-even {100*rec['be']:5.1f} %  (too few signals at any cut)")

    survivors = []
    for r in rows:
        for c, v in r["cuts"].items():
            if v["worst"] > 0 and v["n"] >= 100:
                survivors.append(dict(tf=r["tf"], stop=r["stop"], rr=r["rr"], cut=float(c),
                                      n=v["n"], win=v["win"], worst=v["worst"], mean=v["mean"],
                                      folds=v["folds"], fee_r=r["fee_r"], be=r["be"]))
    survivors.sort(key=lambda x: -x["worst"])
    print(f"\nconfigurations whose WORST fold is positive after fees, with >= 100 trades: {len(survivors)}")
    for s_ in survivors[:12]:
        print(f"  {s_['tf']} stop {s_['stop']} 1:{s_['rr']:.0f} cut {s_['cut']:.2f}  "
              f"n={s_['n']:>5}  win {100*s_['win']:5.1f} %  worst fold {s_['worst']:+.3f} R  "
              f"(break-even {100*s_['be']:.1f} %)")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(dict(rows=rows, survivors=survivors, fee_bp=FEE_RT * 1e4,
                       horizon=args.horizon, folds=[f[0] for f in FOLDS]), fh, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
