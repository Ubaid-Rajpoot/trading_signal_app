#!/usr/bin/env python3
"""Measure the book engine on recorded candles.

Runs the *same* ``BookRunner`` the server runs, over the same 1m CSVs, and
walks every plan forward with its own stop and target. Nothing here can produce
a signal the live bot would not produce - that is the point of the exercise.

    python tools/book_study.py data/btc --symbol BTCUSDT
    python tools/book_study.py data/btc --timeframes 15m --mode shift --min-rr 3

The forward walk is deliberately unkind:

* the **stop is checked before the target** inside the same candle, so a bar
  that touched both is a loss;
* a trade that never resolves inside the horizon is reported as ``open`` and
  excluded from the win rate but included in the R total at its mark;
* break-even management (p.109) is reported separately, never mixed in.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.book import BookRunner, BookSettings  # noqa: E402
from bot.book.liquidity import session_of  # noqa: E402
from bot.buckets import Rollup, TIMEFRAMES  # noqa: E402

TF_MS = {name: ms for name, ms in TIMEFRAMES.items()}


def read_candles(folder: Path, symbol: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(folder.rglob(f"{symbol}-*.csv")):
        with path.open(newline="") as handle:
            for row in csv.reader(handle):
                if len(row) < 6:
                    continue
                raw = int(row[0])
                open_time = raw // 1000 if raw > 10**14 else raw
                if open_time < 10**12:
                    open_time *= 1000
                rows.append({
                    "symbol": symbol,
                    "open_time": open_time,
                    "close_time": open_time + 59_999,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "trades": 0,
                    "closed": True,
                })
    rows.sort(key=lambda r: r["open_time"])
    return rows


def bars_of(candles: list[dict], timeframe: str) -> list[dict]:
    rollup = Rollup(timeframe)
    out: list[dict] = []
    for candle in candles:
        out.extend(rollup.add(candle))
    out.extend(rollup.flush())
    return out


def walk(plan: dict, bars: list[dict], horizon: int, *, be_at_r: float | None,
         scale_at_r: float | None = None, scale_frac: float = 0.5) -> dict:
    """What happened after the entry: stop first, then target, or nothing."""
    start = None
    # the entry is the *close* of the signal candle, so the walk starts on the
    # next candle: the signal candle's range happened before the trade existed
    for i, bar in enumerate(bars):
        if bar["open_time"] > plan["created_ms"]:
            start = i
            break
    if start is None:
        return {"result": "open", "r": 0.0, "bars": 0, "mfe": 0.0, "mae": 0.0}
    direction = 1 if plan["side"] == "LONG" else -1
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    risk = abs(entry - stop)
    if risk <= 0 or target is None:
        return {"result": "open", "r": 0.0, "bars": 0, "mfe": 0.0, "mae": 0.0}
    moved_be = False
    scaled = False
    banked = 0.0
    live = 1.0
    mfe = mae = 0.0
    for offset, bar in enumerate(bars[start:start + horizon]):
        high, low = float(bar["high"]), float(bar["low"])
        if direction > 0:
            mfe = max(mfe, (high - entry) / risk)
            mae = min(mae, (low - entry) / risk)
        else:
            mfe = max(mfe, (entry - low) / risk)
            mae = min(mae, (entry - high) / risk)
        if moved_be:
            if direction > 0 and low <= entry:
                return {"result": "be", "r": 0.0, "bars": offset, "mfe": mfe, "mae": mae}
            if direction < 0 and high >= entry:
                return {"result": "be", "r": 0.0, "bars": offset, "mfe": mfe, "mae": mae}
        hit_stop = low <= stop if direction > 0 else high >= stop
        if hit_stop:
            if scaled:
                # half was banked at +nR, the runner is still on: it comes back
                # to the break-even stop and gives back the rest
                return {"result": "be", "r": banked - live,
                        "bars": offset, "mfe": mfe, "mae": mae}
            if moved_be:
                return {"result": "be", "r": 0.0, "bars": offset, "mfe": mfe, "mae": mae}
            return {"result": "loss", "r": -1.0, "bars": offset, "mfe": mfe, "mae": mae}
        hit_target = high >= target if direction > 0 else low <= target
        if hit_target:
            r = banked + live * abs(target - entry) / risk
            return {"result": "win", "r": r, "bars": offset, "mfe": mfe, "mae": mae}
        if scale_at_r and not scaled and not moved_be:
            reached = (high >= entry + scale_at_r * risk if direction > 0
                       else low <= entry - scale_at_r * risk)
            if reached:
                banked += scale_frac * scale_at_r
                live -= scale_frac
                scaled = True
                moved_be = True
        if be_at_r and not moved_be:
            reached = (high >= entry + be_at_r * risk if direction > 0
                       else low <= entry - be_at_r * risk)
            if reached:
                moved_be = True
    return {"result": "open", "r": 0.0, "bars": horizon, "mfe": mfe, "mae": mae}


def _ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _stats(rows: list[dict]) -> dict:
    wins = [r for r in rows if r["result"] == "win"]
    losses = [r for r in rows if r["result"] == "loss"]
    bes = [r for r in rows if r["result"] == "be"]
    opens = [r for r in rows if r["result"] == "open"]
    decided = len(wins) + len(losses) + len(bes)
    total_r = sum(r["r"] for r in rows)
    return {
        "n": len(rows),
        "win": len(wins),
        "loss": len(losses),
        "be": len(bes),
        "open": len(opens),
        "win_pct": round(100 * len(wins) / decided, 1) if decided else 0.0,
        "total_r": round(total_r, 2),
        "r_per_trade": round(total_r / len(rows), 3) if rows else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default="data/btc")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframes", default="5m,15m,30m,1h")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--mode", default="shift", choices=["shift", "mitigation", "immediate"])
    ap.add_argument("--fill", type=float, default=1.0)
    ap.add_argument("--min-rr", type=float, default=3.0)
    ap.add_argument("--body-atr", type=float, default=1.0)
    ap.add_argument("--vol-mult", type=float, default=1.3)
    ap.add_argument("--arm-bars", type=int, default=30)
    ap.add_argument("--entry-max-bars", type=int, default=40)
    ap.add_argument("--htf", default="", help="override the higher timeframe for every entry tf")
    ap.add_argument("--no-htf", dest="htf_required", action="store_false")
    ap.add_argument("--premium", dest="premium_discount", action="store_true")
    ap.set_defaults(htf_required=True, premium_discount=False)
    ap.add_argument("--sessions", default="", help="comma list, e.g. london,ny")
    ap.add_argument("--target", default="auto", choices=["auto", "major", "medium"],
                    help="which draw the trade is aimed at (auto = medium intraday, major above 1h)")
    ap.add_argument("--stop", default="structure", choices=["structure", "hvi"])
    ap.add_argument("--trap-grade", default="medium", choices=["medium", "major"],
                    help="which pool is worth raiding (major = PDH/PWH/PMH only)")
    ap.add_argument("--require-hvi", action="store_true",
                    help="only accept displacements that carry volume (p.81)")
    ap.add_argument("--require-vector", action="store_true",
                    help="only accept engulfing displacements (p.71)")
    ap.add_argument("--be-at-r", type=float, default=2.0, help="also report break-even at +nR")
    ap.add_argument("--scale-at-r", type=float, default=0.0,
                    help="bank half the position at +nR and move the stop to break-even")
    ap.add_argument("--scale-frac", type=float, default=0.5)
    ap.add_argument("--fee-pct", type=float, default=0.10,
                    help="round-trip taker fee, %% of notional (0.10 = 0.05%% a side, perp)")
    ap.add_argument("--max-fee-r", type=float, default=0.12,
                    help="refuse a plan whose fees are more than this fraction of its risk")
    ap.add_argument("--min-stop-pct", type=float, default=0.0,
                    help="refuse a plan whose stop is closer than this %% of price")
    ap.add_argument("--horizon-bars", type=int, default=2000)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    folder = Path(args.folder)
    if not folder.is_absolute():
        folder = ROOT / folder
    candles = read_candles(folder, args.symbol)
    if args.start:
        candles = [c for c in candles if c["open_time"] >= _ms(args.start)]
    if args.end:
        candles = [c for c in candles if c["open_time"] < _ms(args.end)]
    if not candles:
        print(f"no CSV files for {args.symbol} in {folder}")
        return 1

    timeframes = [tf for tf in args.timeframes.split(",") if tf]
    settings = BookSettings(
        entry_tf=timeframes[0],
        min_body_atr=args.body_atr,
        vol_mult=args.vol_mult,
        arm_bars=args.arm_bars,
        entry_mode=args.mode,
        fill_frac=args.fill,
        entry_max_bars=args.entry_max_bars,
        min_rr=args.min_rr,
        htf_required=args.htf_required,
        premium_discount=args.premium_discount,
        sessions=tuple(s for s in args.sessions.split(",") if s),
        min_trap_grade=args.trap_grade,
        require_hvi=args.require_hvi,
        need_vector=args.require_vector,
        fee_pct=args.fee_pct,
        max_fee_r=args.max_fee_r,
        min_stop_pct=args.min_stop_pct,
    )
    htf_map = {tf: args.htf for tf in timeframes} if args.htf else None
    runner = BookRunner([args.symbol], timeframes, settings, htf_map=htf_map)

    if args.target != "auto":
        for tf in timeframes:
            state = runner.states[args.symbol.upper()]
            state.settings[tf].target_min_grade = args.target
            state.narratives[tf].s.target_min_grade = args.target

    for candle in candles:
        runner.on_candle(candle)

    days = (candles[-1]["close_time"] - candles[0]["open_time"]) / 86_400_000
    print(f"{args.symbol}: {len(candles):,} 1m bars, {days:.0f} days, "
          f"timeframes {timeframes}")
    print(f"mode={args.mode} minRR={args.min_rr} bodyATR={args.body_atr} fill={args.fill} "
          f"htf={'on' if args.htf_required else 'off'} premium={args.premium_discount} "
          f"sessions={args.sessions or 'all'} target={args.target} "
          f"fee={args.fee_pct}% maxFeeR={args.max_fee_r}")
    print(f"funnel: {len(candles):,} minutes -> {runner.traps:,} traps -> "
          f"{runner.armed:,} displacements -> {len(runner.plans)} plans")
    drops = runner.drops()
    if drops:
        print("dropped: " + ", ".join(f"{k} x{v}" for k, v in
                                      sorted(drops.items(), key=lambda kv: -kv[1])))

    bars = {tf: bars_of(candles, tf) for tf in timeframes}
    scored: list[dict] = []
    for plan in runner.plans:
        data = plan.as_dict()
        outcome = walk(data, bars[plan.timeframe], args.horizon_bars,
                       be_at_r=args.be_at_r or None,
                       scale_at_r=args.scale_at_r or None)
        if args.be_at_r:
            outcome_be = walk(data, bars[plan.timeframe], args.horizon_bars,
                              be_at_r=args.be_at_r)
            outcome["r_be"] = outcome_be["r"]
            outcome["result_be"] = outcome_be["result"]
        scored.append({**data, **outcome})

    if not scored:
        print("\nno plans at all - the settings are too strict")
        return 0

    groups: dict[str, dict] = defaultdict(list)
    groups["all"] = scored
    for key in ("timeframe", "side", "grade", "session"):
        for row in scored:
            groups[f"{key}:{row.get(key)}"].append(row)
    by_month: dict[str, list] = defaultdict(list)
    for row in scored:
        month = datetime.fromtimestamp(row["created_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m")
        by_month[month].append(row)

    row_fmt = "{:<22}{:>5}{:>6}{:>6}{:>6}{:>6}{:>8}{:>9}"
    print("\n" + row_fmt.format("group", "n", "win", "loss", "be", "open", "win%", "totalR", "R/trade"))
    for name, rows in sorted(groups.items(), key=lambda kv: (kv[0] != "all", kv[0])):
        st = _stats(rows)
        print(row_fmt.format(name, st["n"], st["win"], st["loss"], st["be"], st["open"],
                             f"{st['win_pct']:.1f}%", f"{st['total_r']:+.2f}",
                             f"{st['r_per_trade']:+.3f}"))

    rrs = [r["rr"] for r in scored if r.get("rr")]
    if rrs:
        rrs.sort()
        print(f"\nmedian planned R:R {rrs[len(rrs) // 2]:.2f} · "
              f"median bars held {sorted(r['bars'] for r in scored)[len(scored) // 2]}")

    if args.be_at_r:
        be_rows = [{"result": r["result_be"], "r": r["r_be"]} for r in scored]
        st = _stats(be_rows)
        print(f"with break-even at +{args.be_at_r:g}R: "
              f"{st['win']} win / {st['be']} be / {st['loss']} loss, "
              f"{st['total_r']:+.2f}R total, {st['r_per_trade']:+.3f}R per trade")

    print("\nby month: " + ", ".join(
        f"{month}:{_stats(rows)['total_r']:+.1f}R/{len(rows)}"
        for month, rows in sorted(by_month.items())))

    if args.json:
        Path(args.json).write_text(json.dumps({
            "settings": {k: v for k, v in settings.__dict__.items()},
            "funnel": {"minutes": len(candles), "traps": runner.traps,
                       "displacements": runner.armed, "plans": len(runner.plans)},
            "drops": drops,
            "stats": {name: _stats(rows) for name, rows in groups.items()},
            "plans": scored,
        }, indent=2, default=str))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
