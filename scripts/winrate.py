#!/usr/bin/env python3
"""30-day win-rate study from the command line.

    # real Binance data (needs internet), 30 days, the 5 scalping timeframes
    python scripts/winrate.py --symbol BTCUSDT --days 30

    # force a fresh download instead of using the cached CSV
    python scripts/winrate.py --symbol BTCUSDT --days 30 --refresh

    # test other parameter values
    python scripts/winrate.py --symbol ETHUSDT --params '{"hvb_step": 2.0, "confluence_window": 1}'

    # show exactly where the candles come from, host by host
    python scripts/winrate.py --symbol BTCUSDT --days 30 --explain

    # refuse to run unless the exchange really answered (never prints demo numbers)
    python scripts/winrate.py --symbol BTCUSDT --days 30 --real-only

The CSV cache lives in backend/data/cache/history/, so the second run is instant
and works offline.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core import winrate  # noqa: E402
from app.core.data.history import probe  # noqa: E402


def explain(symbol: str, days: int, timeframes: list[str], allow_demo: bool) -> None:
    """Print the data trail for every timeframe: hosts, cache, candles, source."""
    for tf in timeframes:
        try:
            info = probe(symbol, tf, days, allow_demo=allow_demo)
        except Exception as exc:
            print(f"[{tf}] no data: {type(exc).__name__}: {exc}")
            continue
        print(f"\n[{tf}] window {info['window']['start']} -> {info['window']['end']}")
        print(f"      want ~{info['bars_expected']:,} bars of {info['bar_seconds']}s")
        for h in info["hosts"]:
            mark = "OK  " if h["ok"] else "FAIL"
            print(f"      {mark} {h['host']:<38} {h['ms']:>5} ms  {h['detail']}")
        c = info["cache"]
        if c["exists"]:
            print(
                f"      cache {c['path']}\n"
                f"            {c['bars']:,} bars {c['from']} -> {c['to']} "
                f"(fetched {c.get('fetched_at')}, recorded source {c.get('recorded_source')})"
            )
        else:
            print(f"      cache {c['path']}  (empty)")
        verdict = "REAL exchange data" if info["is_real"] else "DEMO / synthetic - NOT market data"
        print(
            f"      => using {info['bars']:,} bars {info['first_candle']} -> {info['last_candle']}"
            f" | close {info['first_close']:,} -> {info['last_close']:,}"
        )
        print(f"      => source: {info['source'].upper()}  ({verdict})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest win rate on a 30-day window")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--timeframes", default="1m,3m,5m,15m,30m")
    ap.add_argument("--strategy", default="tdi_shift")
    ap.add_argument("--params", default="{}", help="JSON overrides for the strategy")
    ap.add_argument("--refresh", action="store_true", help="ignore caches and re-download")
    ap.add_argument("--out", default="", help="write a markdown report to this path")
    ap.add_argument("--explain", action="store_true", help="show where the candles come from")
    ap.add_argument(
        "--real-only",
        action="store_true",
        help="fail instead of falling back to demo candles (use this for real results)",
    )
    ap.add_argument("--trades", action="store_true", help="list every trade of the first timeframe")
    args = ap.parse_args()

    try:
        params = json.loads(args.params)
    except json.JSONDecodeError:
        print("--params must be valid JSON", file=sys.stderr)
        return 2

    tfs = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    allow_demo = not args.real_only

    if args.explain:
        print(f"DATA TRAIL - {args.symbol.upper()}, last {args.days} days")
        print("=" * 60)
        explain(args.symbol, args.days, tfs, allow_demo)
        print()

    if args.real_only:
        print("--real-only: demo fallback disabled, only live downloads / real caches count\n")

    result = winrate.report(
        symbol=args.symbol,
        days=args.days,
        timeframes=tfs,
        strategy=args.strategy,
        params=params,
        refresh=args.refresh,
        allow_demo=allow_demo,
    )

    header = f"{args.symbol.upper()} · {args.days} days · {args.strategy}"
    print(header)
    print("=" * len(header))
    print(f"{'TF':<5}{'bars':>8}{'trades':>8}{'wins':>6}{'losses':>7}{'win %':>8}{'return %':>10}{'PF':>7}{'max DD':>9}  source")
    for row in result["rows"]:
        if row.get("error"):
            print(f"{row['timeframe']:<5}{'--':>8}  error: {row['error']}")
            continue
        wr = f"{row['win_rate_pct']:.1f}" if row["win_rate_pct"] is not None else "n/a"
        pf = f"{row['profit_factor']:.2f}" if row.get("profit_factor") is not None else "n/a"
        print(
            f"{row['timeframe']:<5}{row['bars']:>8}{row['trades']:>8}{row['wins']:>6}{row['losses']:>7}"
            f"{wr:>8}{row['total_return_pct']:>10}{pf:>7}{row['max_drawdown_pct']:>9}  {row.get('source')}"
        )

    s = result["summary"]
    print()
    print(f"Trades: {s['trades']}   Wins: {s['wins']}   Losses: {s['losses']}")
    print(f"Overall win rate: {s['win_rate_pct']}%   Best timeframe: {s['best_timeframe']}")
    print(f"Data: {', '.join(result['sources'])}")
    print(result["caveat"])

    if args.trades:
        print()
        try:
            detail = winrate.trades_detail(
                symbol=args.symbol,
                days=args.days,
                timeframe=tfs[0],
                strategy=args.strategy,
                params=params,
                allow_demo=allow_demo,
            )
        except Exception as exc:
            print(f"No trade list: {type(exc).__name__}: {exc}")
            detail = None
        if detail is None:
            return 1 if args.real_only else 0
        print(f"Each trade on {tfs[0]} (verify any of these on TradingView):")
        print(
            f"{'#':<4}{'side':<7}{'entry (UTC)':<19}{'entry px':>11}"
            f"   {'exit (UTC)':<19}{'exit px':>11}{'bars':>6}{'pnl %':>8}  exit reason"
        )
        for i, t in enumerate(detail["trades"], 1):
            print(
                f"{i:<4}{t['side']:<7}{t['entry_time_utc']:<19}{t['entry_price']:>11,.2f}"
                f"   {t['exit_time_utc']:<19}{t['exit_price']:>11,.2f}{t['bars_held']:>6}"
                f"{t['pnl_pct']:>8.2f}  {t['exit_reason']}"
            )
        print(f"wins: {detail['wins']}   losses: {detail['losses']}   win rate: {detail['win_rate_pct']}%")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"# Win-rate study — {args.symbol.upper()}",
            "",
            f"- Window: last **{args.days} days**",
            f"- Strategy: `{args.strategy}` with `{json.dumps(result['params'])}`",
            f"- Generated: {stamp}",
            f"- Data source: **{', '.join(result['sources'])}**",
            "",
            "| TF | Bars | Trades | Wins | Losses | Win % | Return % | PF | Max DD % |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for row in result["rows"]:
            if row.get("error"):
                lines.append(f"| {row['timeframe']} | — | — | — | — | — | — | — | — |")
                continue
            lines.append(
                f"| {row['timeframe']} | {row['bars']} | {row['trades']} | {row['wins']} | {row['losses']} | "
                f"{row['win_rate_pct'] if row['win_rate_pct'] is not None else '—'} | "
                f"{row['total_return_pct']} | {row['profit_factor'] if row['profit_factor'] is not None else '—'} | "
                f"{row['max_drawdown_pct']} |"
            )
        lines += ["", f"> {result['caveat']}", ""]
        out.write_text("\n".join(lines))
        print(f"\nMarkdown report written to {out}")

    if args.real_only and not result["is_real_data"]:
        print(
            "\nFAILED: no real exchange data available, so no trustworthy win rate could be "
            "produced. Numbers above are demo only.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
