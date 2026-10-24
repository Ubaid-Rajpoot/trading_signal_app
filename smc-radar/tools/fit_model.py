#!/usr/bin/env python3
"""Fit and validate the probability model the app ranks signals with.

The question this answers: *given everything the book says about a setup, how
often does its own plan reach the liquidity pool before the stop?* It is fitted
on one 90-day block of real 1m BTC and then checked on a **held-out** 90-day
block and on **other timeframes** - a number that only looks good on the data it
was fitted to is worth nothing.

Output:

* ``bot/signal_model.json`` - the weights, the provenance and the measured
  scores, so the app can score a zone in pure Python (no numpy at runtime);
* a printed report (AUC, calibration, top-k, net of fees);
* ``docs/conditions.md`` quotes this report.

    python tools/fit_model.py --train study-data/BTCUSDT-1m-90d.csv \\
        --holdout study-data/BTCUSDT-1m-90d-holdout.csv --timeframe 1m
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.map import CONDITION_NAMES  # noqa: E402
from tools.condition_study import collect, simulate  # noqa: E402
from tools.zone_quality import read_csv, roll  # noqa: E402

FAMILIES = {"strong_high": "strong", "strong_low": "strong",
            "very_strong_high": "strong", "very_strong_low": "strong",
            "algo_bear": "algo", "algo_bull": "algo",
            "very_strong_algo_bear": "vs_algo", "very_strong_algo_bull": "vs_algo",
            "rejection_block_bear": "rej", "rejection_block_bull": "rej"}
FAMILY_ORDER = ["strong", "algo", "vs_algo", "rej"]      # the last one is the baseline
FEATURES = (["bias", "side=high"] + list(CONDITION_NAMES) + ["log(1+rr)"]
            + ["family=" + f for f in FAMILY_ORDER[:-1]])


def rows_for(csv: str, timeframe: str, horizon: int) -> list[dict]:
    bars = roll(read_csv(Path(csv)), timeframe)
    out = []
    for zone in collect(bars, "BTCUSDT", timeframe):
        res = simulate(zone, bars, horizon)
        if res["result"] == "no fill":
            continue
        plan = zone.plan
        out.append(
            {
                "kind": zone.kind,
                "family": FAMILIES[zone.kind],
                "side": zone.side,
                "conds": dict(zone.conds),
                "rr": plan.get("rr") or 0.0,
                "risk_pct": 100.0 * abs(plan["stop"] - plan["entry"]) / plan["entry"],
                "win": 1.0 if res["result"] == "target" else 0.0,
                "r": res["r"],
                "time": zone.time,
            }
        )
    return out


def matrix(rows: list[dict]) -> list[list[float]]:
    """The exact feature vector, in the order stored in the model file."""
    out = []
    for r in rows:
        vec = [1.0, 1.0 if r["side"] == "high" else 0.0]
        vec += [1.0 if r["conds"].get(c) else 0.0 for c in CONDITION_NAMES]
        vec.append(__import__("math").log1p(max(r["rr"], 0.0)))
        vec += [1.0 if r["family"] == f else 0.0 for f in FAMILY_ORDER[:-1]]
        out.append(vec)
    return out


def fit(X: list[list[float]], y: list[float], l2: float = 1.0, steps: int = 6000, lr: float = 0.05) -> list[float]:
    """Plain logistic regression, written out so the fit can be reproduced."""
    import math

    n, d = len(X), len(X[0])
    base = sum(y) / n
    w = [0.0] * d
    w[0] = math.log(base / (1 - base))
    for _ in range(steps):
        grad = [0.0] * d
        for i in range(n):
            z = sum(w[j] * X[i][j] for j in range(d))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            err = p - y[i]
            row = X[i]
            for j in range(d):
                grad[j] += err * row[j]
        for j in range(d):
            g = grad[j] / n + (l2 * w[j] / n if j else 0.0)
            w[j] -= lr * g
    return w


def predict(w: list[float], X: list[list[float]]) -> list[float]:
    import math

    out = []
    for row in X:
        z = sum(w[j] * row[j] for j in range(len(w)))
        out.append(1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z)))))
    return out


def auc(scores: list[float], y: list[float]) -> float:
    pairs = sorted(zip(scores, y))
    pos = sum(y)
    neg = len(y) - pos
    if not pos or not neg:
        return float("nan")
    rank = 1
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (rank + (rank + (j - i))) / 2.0
        rank_sum += avg * sum(1 for k in range(i, j + 1) if pairs[k][1] == 1)
        rank += j - i + 1
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def net_r(row: dict, entry_bps: float, win_bps: float, loss_bps: float) -> float:
    """The plan net of costs, in R.

    The entry is a limit order at the block edge (maker) and the target is a
    limit at the pool (maker); the stop is a stop order that crosses the spread
    (taker). So the default model charges maker+maker on a win and maker+taker on
    a loss, plus the spread itself as one taker fee.
    """
    risk = row["risk_pct"] / 100.0
    if risk <= 0:
        return row["r"]
    if row["win"]:
        return row["r"] - (entry_bps + win_bps) / 1e4 / risk
    return row["r"] - (entry_bps + loss_bps) / 1e4 / risk


def report(name: str, rows: list[dict], p: list[float], fee: tuple[float, float, float]) -> dict:
    n = len(rows)
    wins = sum(r["win"] for r in rows) / n if n else 0
    gross = sum(r["r"] for r in rows) / n if n else 0
    net = sum(net_r(r, *fee) for r in rows) / n if n else 0
    out = {"n": n, "win": wins, "gross_r": gross, "net_r": net,
           "auc": auc(p, [r["win"] for r in rows])}
    by_score = sorted(range(n), key=lambda i: -p[i])
    line = f"{name:<26}{n:>6}{wins:>7.0%}{gross:>+9.2f}{net:>+10.2f}"
    print(line)
    for k in (0.05, 0.10, 0.20, 0.50):
        take = by_score[: max(1, int(n * k))]
        w = sum(rows[i]["win"] for i in take) / len(take)
        g = sum(rows[i]["r"] for i in take) / len(take)
        nn = sum(net_r(rows[i], *fee) for i in take) / len(take)
        print(f"  top {k:>4.0%}  n={len(take):>5}  win {w:>5.0%}  gross {g:>+6.2f} R  net {nn:>+6.2f} R")
        out[f"top{int(k*100)}"] = {"n": len(take), "win": w, "gross_r": g, "net_r": nn}
    print("  calibration (predicted -> actual):")
    bands = [(0.0, 0.4), (0.4, 0.55), (0.55, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    for lo, hi in bands:
        idx = [i for i in range(n) if lo <= p[i] < hi]
        if len(idx) < 20:
            continue
        actual = sum(rows[i]["win"] for i in idx) / len(idx)
        nn = sum(net_r(rows[i], *fee) for i in idx) / len(idx)
        print(f"    {lo:.2f}-{hi:.2f}  n={len(idx):>5}  predicted {sum(p[i] for i in idx)/len(idx):>4.0%}"
              f"  actual {actual:>4.0%}  net {nn:>+6.2f} R")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Fit and validate the signal model")
    ap.add_argument("--train", default="study-data/BTCUSDT-1m-90d.csv")
    ap.add_argument("--holdout", default="study-data/BTCUSDT-1m-90d-holdout.csv")
    ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--horizon", type=int, default=240)
    ap.add_argument("--out", default="bot/signal_model.json")
    ap.add_argument("--maker-bps", type=float, default=2.0, help="MEXC futures maker")
    ap.add_argument("--taker-bps", type=float, default=6.0, help="MEXC futures taker")
    args = ap.parse_args()

    train = rows_for(args.train, args.timeframe, args.horizon)
    print(f"train {args.train} {args.timeframe}: {len(train)} decided plans")
    w = fit(matrix(train), [r["win"] for r in train])
    print("\nweights (the model file the app ships):")
    for name, value in sorted(zip(FEATURES, w), key=lambda x: -abs(x[1])):
        print(f"  {name:<18}{value:+.3f}")

    fee = (args.maker_bps, args.maker_bps, args.taker_bps)
    print(f"\nfees: entry {args.maker_bps:g}bps (limit) + exit maker {args.maker_bps:g}bps (target)"
          f" / taker {args.taker_bps:g}bps (stop)")
    print(f"\n{'group':<26}{'n':>6}{'win':>7}{'gross R':>9}{'net R':>10}")
    results = {"train": report("train (fitted)", train, predict(w, matrix(train)), fee)}

    hold = rows_for(args.holdout, args.timeframe, args.horizon)
    results["holdout"] = report(f"holdout {args.timeframe} (unseen)", hold, predict(w, matrix(hold)), fee)

    for tf in ("5m", "15m", "1h"):
        if tf == args.timeframe:
            continue
        rows = rows_for(args.train, tf, 120)
        print()
        results[f"train_{tf}"] = report(f"same block, {tf}", rows, predict(w, matrix(rows)), fee)

    model = {
        "features": FEATURES,
        "weights": [round(x, 6) for x in w],
        "conditions": list(CONDITION_NAMES),
        "families": FAMILY_ORDER,
        "trained_on": {
            "file": Path(args.train).name, "timeframe": args.timeframe,
            "horizon": args.horizon, "rows": len(train),
        },
        "validated_on": {"file": Path(args.holdout).name, "timeframe": args.timeframe, "rows": len(hold)},
        "results": results,
        "fees_bps": {"maker": args.maker_bps, "taker": args.taker_bps},
        "command": f"python tools/fit_model.py --train {args.train} --holdout {args.holdout}"
                   f" --timeframe {args.timeframe} --horizon {args.horizon}",
        "target": "P(the plan's target is reached before its stop)",
    }
    out = ROOT / args.out
    out.write_text(json.dumps(model, indent=1) + "\n")
    print(f"\nmodel -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
