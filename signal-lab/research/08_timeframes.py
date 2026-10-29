#!/usr/bin/env python3
"""Which timeframes can actually be traded, and at what stop?

The user wants 1m, 3m, 5m, 10m, 15m, 30m, 45m, 1H, 2H, 3H and 4H. Before any of
them goes in the app, each one has to answer a single question:

    after the exchange takes its cut, does a trade on this clock still pay?

The fee is a fixed 5 bp per round trip. In R (the risk of one trade) that is

    fee_R = 5 bp / (stop in ATR x ATR as a fraction of price)

On 1m, ATR is ~0.05 % of price, so a 2 ATR stop risks 0.1 % and the fee is *0.5 R*
— five times what a 1:10 win would pay. So the stop has to be chosen per
timeframe: wide enough that the fee is a small share of R, narrow enough that the
target is reachable.

This sweep runs every timeframe on the real 9.6 M-bar archive, trains a model on
the years before 2024, and scores 2024-2026 with non-overlapping trades. The
per-timeframe winners are what `tools/train_scanner.py` ships.

Memory: a 1-minute archive is 4.8 M bars, so features are built in overlapping
segments and only the rows that are needed (all of 2024+, a sample of the past)
are kept.

Usage
-----
    python research/08_timeframes.py
    python research/08_timeframes.py --tfs 1m,5m --stops 3,10 --symbols BTCUSDT
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lab import data, features, outcomes  # noqa: E402
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("hunt", os.path.join(os.path.dirname(__file__), "02_hunt.py"))
hunt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hunt)

FEE_RT = hunt.FEE_RT
SPLIT_MS = 1704067200000          # 2024-01-01
ALL_TFS = ("1m", "3m", "5m", "10m", "15m", "30m", "45m", "1h", "2h", "3h", "4h")


def tf_minutes(tf: str) -> int:
    return data.TF_MINUTES[tf]


def thin(fires: np.ndarray, lockout: int) -> np.ndarray:
    """One entry per `lockout` bars - greedy, in time order."""
    out = np.zeros(len(fires), dtype=bool)
    i, n = 0, len(fires)
    while i < n:
        if fires[i]:
            out[i] = True
            i += lockout
        else:
            i += 1
    return out


def collect(symbol: str, tf: str, train_rows: int, stride: int = 3, seed: int = 0):
    """Features for a sample of the bars from 2024 on, plus a sample of the past.

    A 1-minute archive has 1.5 M bars in 2024-2026 and 59 features: kept dense
    that is 700 MB per symbol, and two symbols do not fit in 3 GB. The screen
    keeps every `stride`-th test bar (the non-overlap lock-out is divided by the
    same number), which is what `thin()` would have selected anyway.
    """
    s = data.load(symbol, tf)
    atr = outcomes.atr_series(s.h, s.l, s.c)
    is_test = s.t * 60000 >= SPLIT_MS
    if stride > 1:
        is_test = is_test & (np.arange(len(is_test)) % stride == 0)
    train_idx = np.flatnonzero(~is_test)
    if train_rows and len(train_idx) > train_rows:
        rng = np.random.default_rng(seed)
        train_idx = np.sort(rng.choice(train_idx, train_rows, replace=False))
    keep = is_test.copy()
    keep[train_idx] = True

    idx_parts, F_parts, names = [], [], None
    keep_features = features.live_safe()      # what a live candle feed can produce
    for start, f in features.feature_segments(s, seg=250_000, keep_mask=keep):
        names = keep_features
        F_parts.append(np.stack([f[n] for n in names], axis=1))
        idx_parts.append(np.arange(start, start + len(f[names[0]])))
    idx = np.concatenate(idx_parts)
    F = np.concatenate(F_parts)
    return dict(s=s, atr=atr, idx=idx, F=F, names=names)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT")
    ap.add_argument("--tfs", default=",".join(ALL_TFS))
    ap.add_argument("--stops", default="2,4,7,12,20")
    ap.add_argument("--rrs", default="4")
    ap.add_argument("--horizon", type=int, default=200, help="bars to wait for the target")
    ap.add_argument("--stride", type=int, default=3,
                    help="score every Nth test bar (the sweep is a screen, not the record)")
    ap.add_argument("--train-rows", type=int, default=120_000)
    ap.add_argument("--cut", type=float, default=0.30)
    ap.add_argument("--min-trades", type=int, default=40)
    ap.add_argument("--out", default="results/timeframes.json")
    args = ap.parse_args()
    stops = [float(x) for x in args.stops.split(",")]
    rrs = [float(x) for x in args.rrs.split(",")]

    rows = []
    print(f"{'tf':>4} {'stop':>5} {'rr':>4} {'fee R':>7} {'break-even':>11} "
          f"{'cut':>5} {'n':>6} {'win':>6} {'EV 2024-26':>11} {'/day/sym':>9}", flush=True)
    for tf in args.tfs.split(","):
        horizon = args.horizon
        t0 = time.time()
        parts = [collect(sym, tf, args.train_rows, args.stride)
                 for sym in args.symbols.split(",")]
        print(f"# {tf}: {len(parts[0]['s']):,} bars, {len(parts[0]['idx']):,} rows kept, "
              f"{time.time()-t0:.0f}s", flush=True)
        for stop_atr in stops:
            for rr in rrs:
                wins = ns = 0
                days = 0.0
                cut_used = None
                for p in parts:
                    s, atr, idx, F = p["s"], p["atr"], p["idx"], p["F"]
                    good = idx + 1 < len(s)              # the last bar has no entry
                    idx, F = idx[good], F[good]
                    a = atr[idx]
                    entry = s.o[idx + 1]
                    out = outcomes.first_touch(s.h, s.l, idx + 1, entry + rr * stop_atr * a,
                                               entry - stop_atr * a, horizon)
                    ok = np.isfinite(F).all(axis=1) & (out != 0)
                    t_ms = s.t[idx] * 60000
                    tr = ok & (t_ms < SPLIT_MS)
                    te = ok & (t_ms >= SPLIT_MS)
                    if tr.sum() < 2000 or te.sum() < 200:
                        continue
                    m = HistGradientBoostingClassifier(
                        max_iter=150, learning_rate=0.07, max_leaf_nodes=31,
                        min_samples_leaf=200, l2_regularization=1.0,
                        early_stopping=False, random_state=0)
                    m.fit(F[tr], (out[tr] == 1).astype(int))
                    proba = np.zeros(len(out))
                    proba[te] = m.predict_proba(F[te])[:, 1]
                    # one fixed rule for every timeframe: the model must put at
                    # least 30 % on reaching +4R before -1R (the base rate is ~20 %).
                    # Tuning the cut per timeframe would be a second helping of
                    # hindsight; the page shows the whole cut curve anyway.
                    cut = args.cut
                    sel = thin((proba >= cut) & te, max(1, horizon // max(1, args.stride)))
                    if sel.sum() < args.min_trades:
                        continue
                    wins += int((out[sel] == 1).sum())
                    ns += int(sel.sum())
                    cut_used = cut
                    te_idx = np.flatnonzero(te)
                    days += (s.t[idx[te_idx[-1]]] - s.t[idx[te_idx[0]]]) / 1440
                if ns == 0:
                    continue
                fee_r = float(np.mean([FEE_RT / (stop_atr * float(np.nanmedian(
                    (p["atr"][p["idx"]] / p["s"].c[p["idx"]])[np.isfinite(p["atr"][p["idx"]])])))
                    for p in parts]))
                be = (1 + fee_r) / (1 + rr)
                win = wins / ns
                ev = win * rr - (1 - win) - fee_r
                rows.append(dict(tf=tf, stop=stop_atr, rr=rr, horizon=horizon, fee_r=fee_r,
                                 break_even=be, n=ns, win=win, ev_post=ev, cut=cut_used,
                                 per_day=float(ns / days) if days else None))
                flag = "  <==" if ev > 0.02 else ("  *" if ev > 0 else "")
                print(f"{tf:>4} {stop_atr:>5.0f} {rr:>4.0f} {fee_r:>7.3f} {100*be:>10.1f}% "
                      f"{cut_used:>5.2f} {ns:>6} {100*win:>5.1f}% {ev:>+11.3f} "
                      f"{(ns/days if days else 0):>9.2f}{flag}", flush=True)
        del parts

    ok = [r for r in rows if r["ev_post"] > 0 and r["n"] >= args.min_trades]
    ok.sort(key=lambda r: -r["ev_post"])
    best: dict[str, dict] = {}
    for r in ok:
        if r["tf"] not in best:
            best[r["tf"]] = r
    print(f"\ncandidates with positive expectancy after fees and >= 40 trades: {len(ok)}")
    for tf, r in sorted(best.items(), key=lambda kv: ALL_TFS.index(kv[0])):
        print(f"  {tf:>4}: stop {r['stop']:.0f} ATR, 1:{r['rr']:.0f}, fee {r['fee_r']:.3f} R, "
              f"n={r['n']}, win {100*r['win']:.1f} %, EV {r['ev_post']:+.3f} R, "
              f"{r['per_day']:.2f} signals/day/symbol")
    dead = [tf for tf in args.tfs.split(",") if tf not in best]
    if dead:
        print(f"  no positive geometry found for: {', '.join(dead)}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(dict(rows=rows, best=best, fee_bp=FEE_RT * 1e4), fh, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
