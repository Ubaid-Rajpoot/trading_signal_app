#!/usr/bin/env python3
"""Fit and validate the probability model behind every signal.

The question this answers: *given everything the book says about a setup, and the
geometry of its own plan, how often does the plan reach the liquidity pool before
its stop?* It is fitted on one 90-day block of real 1m BTC (rolled to every
timeframe the app runs) and then checked on a **held-out** 90-day block.

Two things this tool is allowed to change and nothing else:

* the **target minimum distance** (``--target-atr-min``): a pool half a tick away
  is not "the next liquidity level", it is noise inside the block, and it pays a
  fraction of the risk - less than the fee. Skipping pools closer than this is
  the single change that turns the plan from break-even into profitable;
* the **stop** (``--stop-mode``): beyond the exact high/low (``level``, the
  shipped rule) or beyond the far side of the red box (``block``).

Everything else is the book's own detection and the trader's own exit rule.

Output:

* ``bot/signal_model.json`` - weights, provenance and the measured scores, so the
  app can score a zone in pure Python (no numpy at runtime);
* a printed report: calibration bands and the operating points (prob >= x) with
  the accuracy and the R per trade, net of the exchange fee;
* ``docs/signals.md`` quotes this report.

    python tools/fit_model.py --train study-data/BTCUSDT-1m-90d.csv \\
        --holdout study-data/BTCUSDT-1m-90d-holdout.csv --target-atr-min 1.0
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.map import CONDITION_NAMES, DEFAULTS, MarketMap  # noqa: E402
from tools.condition_study import simulate  # noqa: E402
from tools.zone_quality import read_csv, roll  # noqa: E402

TIMEFRAMES = ("1m", "5m", "15m", "30m")
HORIZON = {"1m": 240, "5m": 144, "15m": 96, "30m": 96}
FAMILY = {
    "strong_high": "strong", "strong_low": "strong",
    "very_strong_high": "strong", "very_strong_low": "strong",
    "algo_bear": "algo", "algo_bull": "algo",
    "very_strong_algo_bear": "vs_algo", "very_strong_algo_bull": "vs_algo",
    "rejection_block_bear": "rej", "rejection_block_bull": "rej",
}
FAMILY_ORDER = ["strong", "algo", "vs_algo"]          # "rej" is the baseline
FEATURES = (
    ["bias", "side=high"]
    + list(CONDITION_NAMES)
    + ["log(1+rr)", "log(risk_pct)", "block_atr"]
    + ["family=" + f for f in FAMILY_ORDER]
    + ["tf=" + tf for tf in TIMEFRAMES[1:]]
)


def collect(bars: list[dict], symbol: str, timeframe: str, settings: dict) -> list:
    """Every zone the map produced, in formation order (no look-ahead: the map
    only ever sees the candles up to that point)."""
    map_ = MarketMap(symbol, timeframe, settings)
    seen: dict[str, object] = {}
    for bar in bars:
        map_.add(bar)
        for zone in map_.zones:
            seen.setdefault(zone.key, zone)
    return list(seen.values())


def rows_for(csv: str, settings: dict, symbol: str = "BTCUSDT",
             timeframes: tuple[str, ...] = TIMEFRAMES) -> list[dict]:
    out: list[dict] = []
    maker, taker = float(settings["fee_maker_bps"]), float(settings["fee_taker_bps"])
    for tf in timeframes:
        bars = roll(read_csv(Path(csv)), tf)
        for zone in collect(bars, symbol, tf, settings):
            plan = zone.plan or {}
            if plan.get("target") is None:
                continue
            res = simulate(zone, bars, HORIZON[tf])
            if res["result"] == "no fill":
                continue
            entry = float(plan["entry"])
            risk_pct = 100.0 * abs(float(plan["stop"]) - entry) / entry
            if risk_pct <= 0:
                continue
            # the fee depends on *how* the trade ends: a limit target is maker on
            # both sides, a stop crosses the spread and pays taker on the way out
            bps = (maker + maker) if res["result"] == "target" else (maker + taker)
            fee_r = bps / 1e4 / (risk_pct / 100.0)
            out.append(
                {
                    "kind": zone.kind, "side": zone.side, "tf": tf, "time": int(zone.time),
                    "conds": dict(zone.conds or {}), "rr": float(plan.get("rr") or 0.0),
                    "risk_pct": risk_pct, "block_atr": float(zone.block_atr or 0.0),
                    "win": 1.0 if res["result"] == "target" else 0.0,
                    "r": float(res["r"]), "fee_r": fee_r,
                    "net": float(res["r"]) - fee_r,
                }
            )
    return out


def vector(row: dict) -> list[float]:
    """The feature vector, in the order stored in the model file."""
    vec = [1.0, 1.0 if row["side"] == "high" else 0.0]
    vec += [1.0 if row["conds"].get(c) else 0.0 for c in CONDITION_NAMES]
    vec += [math.log1p(max(row["rr"], 0.0)), math.log(max(row["risk_pct"], 1e-6)),
            min(row["block_atr"], 3.0)]
    vec += [1.0 if FAMILY[row["kind"]] == f else 0.0 for f in FAMILY_ORDER]
    vec += [1.0 if row["tf"] == tf else 0.0 for tf in TIMEFRAMES[1:]]
    return vec


def fit(X: list[list[float]], y: list[float], l2: float = 1.0, steps: int = 1500, lr: float = 0.5) -> list[float]:
    """Plain logistic regression, written out so the fit can be reproduced."""
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
            w[j] -= lr * (grad[j] / n + (l2 * w[j] / n if j else 0.0))
    return w


def predict(w: list[float], X: list[list[float]]) -> list[float]:
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
    rank, rank_sum, i = 1, 0.0, 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (rank + (rank + (j - i))) / 2.0
        rank_sum += avg * sum(1 for k in range(i, j + 1) if pairs[k][1] == 1)
        rank += j - i + 1
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The range the true hit rate is likely to fall in (95% Wilson interval).

    A "100%" measured on 124 trades is not the same statement as a "100%" on
    3,000 - the interval is how the report keeps that distinction honest.
    """
    if n <= 0:
        return (0.0, 1.0)
    phat = wins / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    spread = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def gate(rows: list[dict], p: list[float], cut: float, fee_max: float) -> dict:
    """The signals a cut of ``cut`` produces, net of the fee."""
    sel = [i for i in range(len(rows)) if p[i] >= cut and rows[i]["fee_r"] <= fee_max]
    if not sel:
        return {"n": 0, "win": None, "net": None, "rr": None, "fee_r": None}
    wins = int(sum(rows[i]["win"] for i in sel))
    lo, hi = wilson(wins, len(sel))
    return {
        "n": len(sel),
        "wins": wins,
        "win": wins / len(sel),
        "ci95": [round(lo, 4), round(hi, 4)],
        "net": sum(rows[i]["net"] for i in sel) / len(sel),
        "rr": statistics.median([rows[i]["rr"] for i in sel]),
        "fee_r": statistics.median([rows[i]["fee_r"] for i in sel]),
    }


def report(name: str, rows: list[dict], p: list[float], fee_max: float, days: float) -> dict:
    table: dict[str, dict] = {}
    win = sum(r["win"] for r in rows) / len(rows) if rows else 0.0
    net = sum(r["net"] for r in rows) / len(rows) if rows else 0.0
    print(f"{name:<22}{len(rows):>7}{win:>7.0%}{net:>+8.2f}{auc(p, [r['win'] for r in rows]):>7.3f}   "
          f"{len(rows)/days:>6.1f} plans/day")
    table["all"] = {"n": len(rows), "win": win, "net": net,
                    "auc": auc(p, [r["win"] for r in rows]), "days": days}
    for cut in (0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
        g = gate(rows, p, cut, fee_max)
        table[f"p>={cut}"] = g
        if not g["n"]:
            continue
        print(f"  signals p>={cut:<5} n={g['n']:>5} ({g['n']/days:>5.2f}/day)  "
              f"win {g['win']:>4.0%} (95% CI {g['ci95'][0]:.0%}-{g['ci95'][1]:.0%})  "
              f"net {g['net']:>+5.2f} R  rr {g['rr']:>+5.2f}  fee {g['fee_r']:>4.2f} R")
    return table


def calibration(rows: list[dict], p: list[float]) -> list[dict]:
    bands = [(0.0, 0.4), (0.4, 0.55), (0.55, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    out = []
    print("  calibration (predicted -> actual, out of sample):")
    for lo, hi in bands:
        idx = [i for i in range(len(rows)) if lo <= p[i] < hi]
        if len(idx) < 20:
            continue
        actual = sum(rows[i]["win"] for i in idx) / len(idx)
        net = sum(rows[i]["net"] for i in idx) / len(idx)
        out.append({"band": [lo, hi], "n": len(idx), "predicted": sum(p[i] for i in idx) / len(idx),
                    "actual": actual, "net": net})
        print(f"    {lo:.2f}-{hi:.2f}  n={len(idx):>5}  predicted {sum(p[i] for i in idx)/len(idx):>4.0%}"
              f"  actual {actual:>4.0%}  net {net:>+6.2f} R")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Fit and validate the signal model")
    ap.add_argument("--train", default="study-data/BTCUSDT-1m-90d.csv")
    ap.add_argument("--holdout", default="study-data/BTCUSDT-1m-90d-holdout.csv")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframes", default=",".join(TIMEFRAMES))
    ap.add_argument("--target-atr-min", type=float, default=1.0)
    ap.add_argument("--stop-mode", default="level", choices=("level", "block"))
    ap.add_argument("--out", default="bot/signal_model.json", help="where to write the model")
    args = ap.parse_args()
    tfs = tuple(t.strip() for t in args.timeframes.split(",") if t.strip())

    settings = {**DEFAULTS, "plan_target_atr_min": args.target_atr_min, "plan_stop_mode": args.stop_mode}
    print(f"target minimum {args.target_atr_min} ATR · stop mode {args.stop_mode} · "
          f"timeframes {', '.join(tfs)}")

    train = rows_for(args.train, settings, args.symbol, tfs)
    hold = rows_for(args.holdout, settings, args.symbol, tfs)
    days_train = 90.0
    days_hold = 90.0
    w = fit([vector(r) for r in train], [r["win"] for r in train])

    print("\nweights (what the app ships):")
    for name, value in sorted(zip(FEATURES, w), key=lambda x: -abs(x[1])):
        print(f"  {name:<18}{value:+.3f}")

    print(f"\n{'group':<22}{'plans':>7}{'win':>7}{'net R':>8}{'AUC':>7}")
    results = {"train": report("train (fitted)", train, predict(w, [vector(r) for r in train]), 0.30, days_train)}
    p_hold = predict(w, [vector(r) for r in hold])
    results["holdout"] = report("holdout (unseen 90d)", hold, p_hold, 0.30, days_hold)
    print()
    results["calibration"] = calibration(hold, p_hold)

    print("\nper timeframe, holdout, signals at p>=0.85:")
    per_tf: dict[str, dict] = {}
    for tf in tfs:
        idx = [i for i, r in enumerate(hold) if r["tf"] == tf]
        rows = [hold[i] for i in idx]
        p = [p_hold[i] for i in idx]
        g = gate(rows, p, 0.85, 0.30)
        per_tf[tf] = g
        print(f"  {tf:<4} plans {len(rows):>6}  all: win {sum(r['win'] for r in rows)/max(1,len(rows)):>4.0%} "
              f"net {sum(r['net'] for r in rows)/max(1,len(rows)):>+5.2f}   "
              f"p>=0.85: n={g['n']:>4} ({g['n']/days_hold:>4.2f}/day) "
              f"win {g['win'] if g['win'] is not None else 0:>4.0%} net {g['net'] if g['net'] is not None else 0:>+5.2f}")
    results["per_timeframe"] = per_tf

    # the letters the page prints, each one backed by its out-of-sample record
    cuts = {"A+": 0.90, "A": 0.85, "B": 0.75, "C": 0.60}
    bands = []
    for letter, cut in cuts.items():
        g = gate(hold, p_hold, cut, 0.30)
        bands.append({"letter": letter, "cut": cut, "win": round(g["win"] or 0.0, 4),
                      "n": g["n"], "net": round(g["net"] or 0.0, 3),
                      "ci95": g["ci95"] if g["n"] else [0.0, 1.0]})
    print("\nbands shipped to the page (holdout, fee-filtered):")
    for b in bands:
        print(f"  {b['letter']:<3} p>={b['cut']:<5} {b['win']:>4.0%} on {b['n']:>4} signals"
              f" (95% CI {b['ci95'][0]:.0%}-{b['ci95'][1]:.0%})  net {b['net']:>+5.2f} R")

    model = {
        "bands": bands,
        "features": FEATURES,
        "weights": [round(x, 6) for x in w],
        "conditions": list(CONDITION_NAMES),
        "families": FAMILY_ORDER + ["rej"],
        "trained_on": {
            "file": Path(args.train).name, "timeframes": list(tfs), "rows": len(train),
            "symbol": args.symbol,
            "plan": {"target_atr_min": args.target_atr_min, "stop_mode": args.stop_mode},
        },
        "validated_on": {"file": Path(args.holdout).name, "rows": len(hold)},
        "results": results,
        "fees_bps": {"maker": DEFAULTS["fee_maker_bps"], "taker": DEFAULTS["fee_taker_bps"]},
        "command": (f"python tools/fit_model.py --train {args.train} --holdout {args.holdout}"
                    f" --target-atr-min {args.target_atr_min} --stop-mode {args.stop_mode}"),
        "target": "P(the plan's target is reached before its stop), out of sample",
    }
    out = ROOT / args.out
    out.write_text(json.dumps(model, indent=1) + "\n")
    print(f"\nmodel -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
