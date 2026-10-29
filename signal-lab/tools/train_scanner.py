#!/usr/bin/env python3
"""Train the scanner: one model per timeframe, plus the per-timeframe geometry.

Input : `results/timeframes.json` (the sweep in `research/08_timeframes.py`)
Output: `app/model.pkl`, `app/data/geoms.json`, `app/data/bands_tf.json`

For every timeframe the sweep kept, this trains

  * a production model on everything up to today (what live scoring uses), and
  * a validation model on 2024-01-01 and earlier (what the replay uses, so the
    replay cannot be scored by a model that has seen its own test years),

then measures the probability bands on 2024-2026 with non-overlapping trades and
picks the recording cut from them. Nothing here is hand-set: each number is
written next to the file it came from.

    python tools/train_scanner.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))
from lab import data, outcomes  # noqa: E402

SPLIT_MS = 1704067200000          # 2024-01-01
CUTS = (0.50, 0.45, 0.35, 0.25, 0.15)
BAND_FLOOR = 40                   # no band is reported below this many trades
APP = os.path.join(ROOT, "app")


def thin(fires: np.ndarray, lockout: int) -> np.ndarray:
    out = np.zeros(len(fires), dtype=bool)
    i, n = 0, len(fires)
    while i < n:
        if fires[i]:
            out[i] = True
            i += lockout
        else:
            i += 1
    return out


def model_for(train_x, train_y):
    m = HistGradientBoostingClassifier(
        max_iter=150, learning_rate=0.07, max_leaf_nodes=31, min_samples_leaf=200,
        l2_regularization=1.0, early_stopping=False, random_state=0)
    m.fit(train_x, train_y)
    return m


def _train_tf(symbols: list[str], tf: str, geom: dict, train_rows: int, test_stride: int = 3,
              verbose=True):
    from lab import features as labfeatures
    # only what a live candle feed can produce: MEXC sends no trade count and no
    # taker-buy split, so those features are NaN live and would block every signal
    keep_features = labfeatures.live_safe()
    fames = []
    for sym in symbols:
        s = data.load(sym, tf)
        atr = outcomes.atr_series(s.h, s.l, s.c)
        is_test = s.t * 60000 >= SPLIT_MS
        # a 1-minute archive has 1.5 M test bars; keep every `test_stride`-th one
        # (the non-overlap lock-out is divided by the same number) so the feature
        # matrix fits in 3 GB, and train the production model on that sample
        if test_stride > 1:
            is_test = is_test & (np.arange(len(is_test)) % test_stride == 0)
        train_idx = np.flatnonzero(~is_test)
        if train_rows and len(train_idx) > train_rows:
            rng = np.random.default_rng(0)
            train_idx = np.sort(rng.choice(train_idx, train_rows, replace=False))
        keep = is_test.copy()
        keep[train_idx] = True
        idx_parts, mat_parts, names = [], [], None
        for start, f in labfeatures.feature_segments(s, seg=250_000, keep_mask=keep):
            names = keep_features
            mat_parts.append(np.stack([f[n] for n in names], axis=1))
            idx_parts.append(np.arange(start, start + len(f[names[0]])))
        idx = np.concatenate(idx_parts)
        F = np.concatenate(mat_parts)
        good = idx + 1 < len(s)
        idx, F = idx[good], F[good]
        a = atr[idx]
        entry = s.o[idx + 1]
        out = outcomes.first_touch(s.h, s.l, idx + 1, entry + geom["target"] * a,
                                   entry - geom["stop"] * a, int(geom["horizon"]))
        ok = np.isfinite(F).all(axis=1) & (out != 0)
        t_ms = s.t[idx] * 60000
        fames.append(dict(sym=sym, idx=idx, F=F, out=out, ok=ok, t_ms=t_ms,
                          atr=a, price=s.c[idx]))
    tr = np.concatenate([fm["ok"] & (fm["t_ms"] < SPLIT_MS) for fm in fames])
    te = np.concatenate([fm["ok"] & (fm["t_ms"] >= SPLIT_MS) for fm in fames])
    F = np.concatenate([fm["F"] for fm in fames])
    y = np.concatenate([(fm["out"] == 1).astype(int) for fm in fames])
    out = np.concatenate([fm["out"] for fm in fames])
    t_ms = np.concatenate([fm["t_ms"] for fm in fames])
    atr = np.concatenate([fm["atr"] for fm in fames])
    price = np.concatenate([fm["price"] for fm in fames])
    order = np.argsort(t_ms, kind="stable")
    idx_sorted = order
    if verbose:
        print(f"# {tf}: train {int(tr.sum()):,} / test {int(te.sum()):,} rows", flush=True)

    m_oos = model_for(F[tr], y[tr])
    m_prod = model_for(F, y)
    proba = np.zeros(len(out))
    proba[te] = m_oos.predict_proba(F[te])[:, 1]
    # non-overlapping test trades, in pool order (rows are `test_stride` bars apart)
    lockout = max(1, int(geom["horizon"]) // max(1, test_stride))
    sel = thin((proba > 0) & te, lockout)
    chosen_prob = proba[sel]
    chosen_out = out[sel]
    chosen_ms = t_ms[sel]
    chosen_fee = 5e-4 * price[sel] / (geom["stop"] * atr[sel])

    bands = []
    for cut in CUTS:
        m = chosen_prob >= cut
        n = int(m.sum())
        if n < BAND_FLOOR:
            continue
        wins = int((chosen_out[m] == 1).sum())
        p = wins / n
        fee_r = float(np.nanmean(chosen_fee[m]))
        be = (1 + fee_r) / (1 + geom["ratio"])
        se = (p * (1 - p) / n) ** 0.5
        folds = {}
        for label, lo, hi in (("2024", 1704067200000, 1735689600000),
                              ("2025", 1735689600000, 1767225600000),
                              ("2026", 1767225600000, 4102444800000)):
            fm = m & (chosen_ms >= lo) & (chosen_ms < hi)
            if fm.sum():
                folds[label] = {"n": int(fm.sum()),
                                "win": float((chosen_out[fm] == 1).mean())}
        bands.append(dict(cut=float(cut), n=n, wins=wins, win=p, ci=[p - 1.96 * se, p + 1.96 * se],
                          fee_r=fee_r, break_even=be,
                          ev_post=p * geom["ratio"] - (1 - p) - fee_r,
                          rr=geom["ratio"], folds=folds))
    # the recording cut: the most selective band that still has a usable sample,
    # and whose expectancy after fees is positive. If none is positive, take the
    # band with the best measured expectancy and say so in the report.
    good = [b for b in bands if b["ev_post"] > 0 and b["n"] >= BAND_FLOOR]
    if good:
        cut = max(good, key=lambda b: b["cut"])["cut"]
    elif bands:
        cut = max(bands, key=lambda b: b["ev_post"])["cut"]
    else:
        cut = 0.30
    # honest per-timeframe verdict on the measured window
    tre = np.nonzero(sel)[0]
    m = chosen_prob >= cut
    n = int(m.sum())
    wins = int((chosen_out[m] == 1).sum())
    win = wins / n if n else None
    days = float((chosen_ms.max() - chosen_ms.min()) / 86400000) if len(chosen_ms) else 0.0
    per_day = float(n / days) if days else 0.0
    fee_r = float(np.nanmean(chosen_fee[m])) if n else None
    ev = (win * geom["ratio"] - (1 - win) - (fee_r or 0)) if win is not None else None
    meta = dict(target=geom["target"], stop=geom["stop"], horizon=int(geom["horizon"]),
                cut=float(cut), n=n, win=win, fee_r=fee_r, ev_post=ev, per_day=per_day,
                basis="2024-2026, non-overlapping, validation model",
                source="results/timeframes.json + tools/train_scanner.py")
    if verbose:
        print(f"  {tf}: cut {cut:.2f} -> {n} trades, win "
              f"{100*win if win is not None else 0:.1f} %, EV {ev if ev else 0:+.3f} R, "
              f"{per_day:.2f}/day/symbol", flush=True)
    return dict(prod=m_prod, oos=m_oos, features=names, meta=meta, bands=bands,
                rows=dict(train=int(tr.sum()), test=int(te.sum())), fames=fames)


def train_all(tfs: list[str], sweep: dict, symbols: list[str], train_rows: int,
              test_stride: int = 3):
    models, models_oos, geoms, tf_bands, trained = {}, {}, {}, {}, {}
    feats = None
    for tf in tfs:
        best = (sweep.get("best") or {}).get(tf)
        if best:
            geom = dict(target=best["stop"] * best["rr"], stop=best["stop"],
                        horizon=int(best.get("horizon") or 200), ratio=best["rr"])
            basis = "chosen by research/08_timeframes.py on 2024-2026"
        else:
            # Nothing on this clock paid after fees in the sweep. Ship its best
            # measured attempt anyway, flagged `viable: False`, so the clock is
            # available and its measured cost is visible instead of hidden.
            rows = [r for r in sweep.get("rows", []) if r["tf"] == tf]
            b = max(rows, key=lambda r: r["ev_post"]) if rows else None
            stop = b["stop"] if b else 2.0
            rr = b["rr"] if b else 4.0
            geom = dict(target=stop * rr, stop=stop, horizon=int(b.get("horizon") or 200) if b else 240,
                        ratio=rr)
            basis = ("no positive geometry after fees in the sweep - usable, but it "
                     "loses the measured fee on every trade")
        r = _train_tf(symbols, tf, geom, train_rows, test_stride=test_stride)
        models[tf], models_oos[tf] = r["prod"], r["oos"]
        feats = r["features"] or feats
        meta = dict(r["meta"])
        meta["basis"] = basis
        # viable means: measured positive after fees with this exact model and
        # feature set - not "was chosen by the screen"
        meta["viable"] = bool(best) and meta.get("ev_post") is not None and meta["ev_post"] > 0
        meta["trained"] = True
        meta["test_rows"] = r["rows"]["test"]
        geoms[tf] = meta
        tf_bands[tf] = r["bands"]
        trained[tf] = r["rows"]
    return models, models_oos, geoms, tf_bands, trained, feats


def add_untrained(geoms: dict, sweep: dict, trained: set[str]) -> dict:
    """Record the timeframes that were measured but not shipped, so the page can show
    why they are off instead of silently omitting them."""
    out = dict(geoms)
    for tf in sorted({r["tf"] for r in sweep.get("rows", [])}, key=lambda t: (len(t), t)):
        if tf in trained:
            continue
        rows = [r for r in sweep["rows"] if r["tf"] == tf]
        if not rows:
            continue
        b = max(rows, key=lambda r: r["ev_post"])
        out[tf] = dict(target=b["stop"] * b["rr"], stop=b["stop"],
                       horizon=int(b.get("horizon") or 200), cut=float(b.get("cut") or 0.3),
                       n=b["n"], win=b["win"], fee_r=b["fee_r"], ev_post=b["ev_post"],
                       per_day=b.get("per_day"), trained=False, viable=False,
                       basis="measured by research/08_timeframes.py; no timeframe at this "
                             "clock paid after fees, so it is not scanned")
    return out


def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", default="results/timeframes.json")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--train-rows", type=int, default=120_000)
    ap.add_argument("--tfs", default="")
    ap.add_argument("--test-stride", type=int, default=3,
                    help="keep every Nth 2024+ bar (memory); the lock-out is divided to match")
    ap.add_argument("--merge", action="store_true",
                    help="keep the timeframes already in app/model.pkl")
    args = ap.parse_args()

    with open(os.path.join(ROOT, args.sweep)) as fh:
        sweep = json.load(fh)
    tfs = [t for t in (args.tfs.split(",") if args.tfs else sorted(sweep.get("best") or {}))]
    if not tfs:
        raise SystemExit("the sweep found no timeframe to ship - run research/08_timeframes.py first")
    print(f"training {len(tfs)} timeframe(s): {', '.join(tfs)}", flush=True)

    t0 = time.time()
    models, models_oos, geoms, tf_bands, trained, feats = train_all(
        tfs, sweep, args.symbols.split(","), args.train_rows, args.test_stride)

    if args.merge and os.path.exists(os.path.join(APP, "model.pkl")):
        old = joblib.load(os.path.join(APP, "model.pkl"))
        for k, v in (old.get("models") or {}).items():
            models.setdefault(k, v)
        for k, v in (old.get("models_oos") or {}).items():
            models_oos.setdefault(k, v)
        for k, v in (old.get("geoms") or {}).items():
            geoms.setdefault(k, v)
        feats = feats or old["features"]

    geoms = add_untrained(geoms, sweep, set(models))
    default = geoms.get("1h") or next(iter(geoms.values()))
    pack = dict(models=models, models_oos=models_oos, features=feats,
                tfs=sorted(models), target=default["target"], stop=default["stop"],
                horizon=default["horizon"], fee_rt=5e-4, trained_rows=trained,
                geoms=geoms, oos_trained_until="2023-12-31",
                trained_at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    joblib.dump(pack, os.path.join(APP, "model.pkl"), compress=3)
    os.makedirs(os.path.join(APP, "data"), exist_ok=True)
    with open(os.path.join(APP, "data", "geoms.json"), "w") as fh:
        json.dump({"geoms": geoms, "fee_bp": 5.0,
                   "note": "per-timeframe geometry measured by research/08_timeframes.py; "
                           "cut from the 2024-2026 bands in this run"}, fh, indent=1)
    with open(os.path.join(APP, "data", "bands_tf.json"), "w") as fh:
        json.dump({"bands": tf_bands, "fee_bp": 5.0,
                   "note": "probability bands per timeframe, 2024-2026, non-overlapping"}, fh, indent=1)

    print(f"\nwrote app/model.pkl, app/data/geoms.json, app/data/bands_tf.json "
          f"in {time.time()-t0:.0f}s", flush=True)
    print("\nshipped geometry")
    print(f"{'tf':>4} {'stop':>5} {'target':>7} {'cut':>5} {'n':>6} {'win':>6} {'EV':>8} {'per day':>8}")
    for tf in sorted(geoms):
        g = geoms[tf]
        print(f"{tf:>4} {g['stop']:>5.0f} {g['target']:>7.0f} {g['cut']:>5.2f} {g['n']:>6} "
              f"{100*(g['win'] or 0):>5.1f}% {(g['ev_post'] or 0):>+8.3f} {g['per_day']:>8.2f}")


if __name__ == "__main__":
    _main()
