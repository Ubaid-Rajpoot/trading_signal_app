#!/usr/bin/env python3
"""Audit the book engine's own plans: are the signals *right*, or just frequent?

`book_study.py` answers "what did this rule set return". This answers the
question before it: **what are these signals made of, and which of them are
not worth taking?** It reads the JSON that study writes, so the numbers here
are computed on exactly the plans the bot would have published.

    python tools/book_study.py data/btc --json /tmp/study/base.json
    python tools/book_audit.py /tmp/study/base.json --train 2022-07-01:2023-12-31 \
        --holdout 2024-01-01:2025-06-30

Sections

1. **Funnel** - how many minutes became traps, displacements and plans, and
   every reason the engine refused (so "silence" is always explainable).
2. **Integrity** - the pathologies that make a signal *wrong* even when it
   wins: an entry far from the story, a candle-sized stop, a draw so far it is
   never reached, two contradictory plans at once.
3. **Outcome by slice** - train vs holdout, and every bucket (timeframe, side,
   session, grade, R:R, lag, risk) with the R/trade in it.
4. **Exit policies** - recomputed from each plan's own MFE/MAE, so a policy can
   be compared without re-running the engine. Pessimistic by construction: if a
   bar reached both the stop and the target, the stop is assumed first.

Nothing here writes to the engine; it exists to be argued with.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Binance spot taker fee is 0.10% per side (0.075% with BNB). A book trade
# pays it twice: in and out. Every R figure on the page and in these reports is
# net of it, and the fee is converted to R with the plan's own risk.
FEE_ROUND_TRIP_PCT = 0.20

TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
         "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000}


def ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def year_of(stamp: int) -> str:
    return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def stats(rows: list[dict]) -> dict:
    wins = sum(1 for r in rows if r.get("result") == "win")
    losses = sum(1 for r in rows if r.get("result") == "loss")
    bes = sum(1 for r in rows if r.get("result") == "be")
    opens = sum(1 for r in rows if r.get("result") == "open")
    decided = wins + losses + bes
    total = sum(float(r.get("r") or 0) for r in rows)
    rpt = total / len(rows) if rows else 0.0
    # the standard error of R/trade, so "it works" has a bar to clear
    r_values = [float(r.get("r") or 0) for r in rows]
    if len(r_values) > 1:
        mean = sum(r_values) / len(r_values)
        var = sum((x - mean) ** 2 for x in r_values) / (len(r_values) - 1)
        se = (var / len(r_values)) ** 0.5
    else:
        se = 0.0
    return {"n": len(rows), "win": wins, "loss": losses, "be": bes, "open": opens,
            "decided": decided, "win_pct": (wins / decided) if decided else 0.0,
            "total_r": total, "r": rpt, "se": se,
            "t": (rpt / se) if se else 0.0}


def line(name: str, st: dict) -> str:
    return (f"{name:<26}{st['n']:>5}{st['win']:>5}{st['loss']:>5}{st['be']:>4}"
            f"{st['open']:>5}{st['win_pct'] * 100:>7.1f}%{st['total_r']:>+8.2f}"
            f"{st['r']:>+8.3f}{st['se']:>7.3f}{st['t']:>+7.2f}")


HEAD = (f"{'group':<26}{'n':>5}{'win':>5}{'loss':>5}{'be':>4}{'open':>5}{'win%':>8}"
        f"{'totalR':>8}{'R/trade':>8}{'se':>7}{'t':>7}")


# ------------------------------------------------------------------ policies
def policy_r(row: dict, *, target_r: float | None = None, stop_r: float = 1.0,
             be_at_r: float | None = None, trail_at_r: float | None = None,
             scale_at_r: float | None = None, scale_frac: float = 0.5) -> tuple[str, float]:
    """Score one plan under an exit policy, from its own MFE/MAE.

    Order of touch is unknown inside one bar, so every ambiguity is resolved
    against the trade: if the bar saw both the stop and the target, the stop
    came first.
    """
    mfe = float(row.get("mfe") or 0.0)
    mae = float(row.get("mae") or 0.0)
    # the fee is paid on the notional, in R it is 0.20% * entry / risk
    fee_r = FEE_ROUND_TRIP_PCT / 100 * float(row["entry"]) / max(1e-12, abs(float(row["entry"]) - float(row["stop"])))
    if be_at_r is not None and mfe >= be_at_r:
        # the stop moved to entry: only the *adverse* move after that matters,
        # and it can no longer be worse than a scratch
        if mae <= -stop_r and mfe < (target_r or 1e9):
            return "be", 0.0
        stop_r = 0.0
    if scale_at_r is not None and mfe >= scale_at_r:
        banked = scale_frac * scale_at_r
        if mae <= -stop_r and mfe < (target_r or 1e9):
            return "loss", banked - (1 - scale_frac)
        if target_r is not None and mfe >= target_r:
            return "win", banked + (1 - scale_frac) * target_r
        return "be", banked
    if trail_at_r is not None and mfe >= trail_at_r:
        # a trailing stop that gives back `trail` R from the best price
        give_back = max(0.0, mfe - trail_at_r)
        if mae <= -stop_r and mfe < (target_r or 1e9):
            return "loss", -stop_r
        return "be", give_back
    if mae <= -stop_r:
        return "loss", -stop_r - fee_r
    if target_r is not None and mfe >= target_r:
        return "win", target_r - fee_r
    return "open", 0.0


def policy_table(rows: list[dict], policies: list[tuple[str, dict]]) -> None:
    print("\n" + HEAD.replace("group", "exit policy"))
    for name, kwargs in policies:
        scored = []
        for row in rows:
            if name.startswith("as published"):
                # the engine's own target, already walked forward by the study
                state = row.get("result")
                r = float(row.get("r") or 0.0)
                if state in ("win", "loss"):
                    r -= FEE_ROUND_TRIP_PCT / 100 * float(row["entry"]) / max(
                        1e-12, abs(float(row["entry"]) - float(row["stop"])))
                scored.append({"result": state, "r": r})
                continue
            result, r = policy_r(row, **kwargs)
            scored.append({"result": result, "r": r})
        print(line(name, stats(scored)))


# ---------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("study", help="the JSON written by book_study.py --json")
    ap.add_argument("--train", default="", help="YYYY-MM-DD:YYYY-MM-DD used to choose rules")
    ap.add_argument("--holdout", default="", help="YYYY-MM-DD:YYYY-MM-DD never used to choose")
    ap.add_argument("--top", type=int, default=14)
    args = ap.parse_args()

    data = json.loads(Path(args.study).read_text())
    rows: list[dict] = data["plans"]
    funnel = data.get("funnel", {})
    drops = data.get("drops", {})

    print(f"=== funnel: {funnel.get('minutes', 0):,} minutes -> {funnel.get('traps', 0):,} traps "
          f"-> {funnel.get('displacements', 0):,} displacements -> {len(rows):,} plans")
    if drops:
        total = sum(drops.values())
        print(f"refusals ({total:,}):")
        for reason, count in sorted(drops.items(), key=lambda kv: -kv[1])[:args.top]:
            print(f"  {reason:<52}{count:>7}")
        print(f"  {'(reasons not in this top list)':<52}"
              f"{total - sum(sorted(drops.values(), reverse=True)[:args.top]):>7}")

    if not rows:
        print("no plans: nothing to audit")
        return 0

    # ---------------------------------------------------------------- segments
    train = holdout = None
    if args.train:
        a, b = args.train.split(":")
        train = [r for r in rows if ms(a) <= r["created_ms"] <= ms(b)]
    if args.holdout:
        a, b = args.holdout.split(":")
        holdout = [r for r in rows if ms(a) <= r["created_ms"] <= ms(b)]

    print("\n=== outcome (stop first, target as published)")
    print(HEAD)
    print(line("all plans", stats(rows)))
    if train is not None:
        print(line("train (chosen on)", stats(train)))
    if holdout is not None:
        print(line("HOLDOUT (untouched)", stats(holdout)))
    by_year: dict[str, list] = defaultdict(list)
    for row in rows:
        by_year[year_of(row["created_ms"])[:4]].append(row)
    for name in sorted(by_year):
        print(line(f"  {name}", stats(by_year[name])))

    # --------------------------------------------------------------- integrity
    print("\n=== integrity: is each signal internally sound?")
    problems = Counter()
    examples: dict[str, str] = {}
    lags: list[float] = []
    risk_pct: list[float] = []
    rr_values: list[float] = []
    for row in rows:
        tf = row["timeframe"]
        step = TF_MS.get(tf, 1)
        entry, stop, target = float(row["entry"]), float(row["stop"]), float(row["target"])
        risk = abs(entry - stop)
        rr = float(row.get("rr") or 0)
        risk_pct.append(100 * risk / entry if entry else 0.0)
        rr_values.append(rr)
        shift = row.get("shift") or {}
        if shift.get("time"):
            lag = (row["created_ms"] - int(shift["time"])) / step
            lags.append(lag)
            if lag > 3:
                problems["entry more than 3 bars after the shift that confirmed it"] += 1
                examples.setdefault("lag", f"{row['symbol']} {tf} {year_of(row['created_ms'])} lag {lag:.0f} bars")
        gap = row.get("ac") or {}
        if gap.get("top") and gap.get("bottom"):
            height = float(gap["top"]) - float(gap["bottom"])
            if risk < height * 0.5:
                problems["stop is inside the imbalance (risk < half the gap)"] += 1
                examples.setdefault("stop_in_gap", f"{row['symbol']} {tf} risk {risk:.1f} gap {height:.1f}")
        if risk / entry * 100 < 0.03:
            problems["stop closer than 0.03% of price (fees and noise dominate)"] += 1
            examples.setdefault("tiny_stop", f"{row['symbol']} {tf} risk {risk:.4f}")
        if rr > 8:
            problems["draw more than 8R away (rarely reached, R total is a lottery)"] += 1
        if rr < 2:
            problems["draw closer than 2R (cannot pay for a loss)"] += 1
        if row.get("fee_r") and float(row["fee_r"]) > 0.15:
            problems["fees are more than 0.15R of the risk"] += 1
    # contradictory plans: same symbol+tf, opposite sides, inside 4 hours
    by_cell: dict[tuple, list] = defaultdict(list)
    for row in rows:
        by_cell[(row["symbol"], row["timeframe"])].append(row)
    clashes = 0
    for cells in by_cell.values():
        cells.sort(key=lambda r: r["created_ms"])
        for i, row in enumerate(cells):
            for other in cells[i + 1:]:
                if other["created_ms"] - row["created_ms"] > 4 * 3_600_000:
                    break
                if other["side"] != row["side"]:
                    clashes += 1
                    break
    if clashes:
        problems["both directions inside 4 hours on the same instrument"] += clashes

    if lags:
        lags.sort()
        print(f"  bars between the confirming shift and the published entry: "
              f"median {lags[len(lags)//2]:.0f}, 90th {lags[int(len(lags)*0.9)]:.0f}, max {lags[-1]:.0f}")
    if risk_pct:
        risk_pct.sort()
        print(f"  stop distance as % of price: median {risk_pct[len(risk_pct)//2]:.3f}%, "
              f"min {risk_pct[0]:.3f}%, max {risk_pct[-1]:.3f}%")
    if rr_values:
        rr_values.sort()
        print(f"  planned R:R: median {rr_values[len(rr_values)//2]:.2f}, "
              f"10th {rr_values[int(len(rr_values)*0.1)]:.2f}, 90th {rr_values[int(len(rr_values)*0.9)]:.2f}")
    if problems:
        for reason, count in problems.most_common():
            print(f"  ! {reason:<60}{count:>6}  ({_pct(count, len(rows))})")
        for name, example in examples.items():
            print(f"      e.g. {name}: {example}")
    else:
        print("  no structural problems found")

    # ------------------------------------------------------------------ slices
    scope = holdout if holdout else rows
    label = "HOLDOUT" if holdout else "all"
    print(f"\n=== which buckets actually work ({label})")
    for key in ("timeframe", "side", "grade", "session"):
        print("  " + HEAD.replace("group", key))
        groups: dict[str, list] = defaultdict(list)
        for row in scope:
            groups[str(row.get(key))].append(row)
        for name, group in sorted(groups.items()):
            if len(group) >= 5:
                print("  " + line(name, stats(group)))

    def bucket(name: str, values: list[float], edges: list[float], fmt: str = "{:.1f}") -> None:
        print("  " + HEAD.replace("group", name))
        buckets: dict[str, list] = defaultdict(list)
        for row, value in zip(scope, values):
            for low, high in zip(edges, edges[1:]):
                if low <= value < high:
                    buckets[f"{fmt.format(low)}-{fmt.format(high)}"].append(row)
                    break
            else:
                buckets[f">={fmt.format(edges[-1])}"].append(row)
        for name2, group in sorted(buckets.items()):
            if len(group) >= 5:
                print("  " + line(name2, stats(group)))

    bucket("planned R:R", [float(r.get("rr") or 0) for r in scope], [0, 2, 3, 4, 6, 8, 12, 1e9])
    if lags:
        lag_by_key = {}
        for row in scope:
            shift = row.get("shift") or {}
            if shift.get("time"):
                lag_by_key[row["created_ms"], row["symbol"], row["timeframe"]] = (
                    row["created_ms"] - int(shift["time"])) / TF_MS.get(row["timeframe"], 1)
        picked = [r for r in scope if (r["created_ms"], r["symbol"], r["timeframe"]) in lag_by_key]
        if picked:
            bucket("entry lag (bars)",
                   [lag_by_key[(r["created_ms"], r["symbol"], r["timeframe"])] for r in picked],
                   [-0.5, 0.5, 1.5, 3.5, 1e9], fmt="{:.0f}")
    bucket("stop as % of price", [100 * abs(float(r["entry"]) - float(r["stop"])) / float(r["entry"])
                                  for r in scope],
           [0, 0.05, 0.1, 0.2, 0.4, 0.8, 1e9], fmt="{:.2f}")

    # ------------------------------------------------------------ exit policies
    print("\n=== exit policies, scored from each plan's own MFE/MAE")
    print(f"  (target as published: median "
          f"{sorted(float(r.get('rr') or 0) for r in scope)[len(scope)//2]:.2f}R)")
    policy_table(scope, [
        ("as published (target)", {"target_r": None}),
        ("stop 1R, any target", {"target_r": None, "stop_r": 1.0}),
        ("2R target, 1R stop", {"target_r": 2.0}),
        ("3R target, 1R stop", {"target_r": 3.0}),
        ("2R, be at +1R", {"target_r": 2.0, "be_at_r": 1.0}),
        ("3R, be at +1.5R", {"target_r": 3.0, "be_at_r": 1.5}),
        ("3R, half off at +2R", {"target_r": 3.0, "scale_at_r": 2.0}),
        ("2R, half off at +1R", {"target_r": 2.0, "scale_at_r": 1.0}),
        ("trail 1R after +2R", {"target_r": None, "trail_at_r": 2.0}),
    ])

    # --------------------------------------------------------------- the truth
    print("\n=== what the numbers mean")
    for name, group in (("train", train), ("holdout", holdout)):
        if not group:
            continue
        st = stats(group)
        verdict = ("edge is inside the noise" if abs(st["t"]) < 2 else
                   "positive and outside the noise" if st["t"] > 0 else "negative and outside the noise")
        print(f"  {name:<8} n={st['n']:<4} R/trade {st['r']:+.3f} ± {st['se']:.3f} (t={st['t']:+.2f}) -> {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
