#!/usr/bin/env python3
"""30-day win-rate study from the command line.

    # real Binance data (needs internet), 30 days, the 5 scalping timeframes
    python scripts/winrate.py --symbol BTCUSDT --days 30

    # force a fresh download instead of using the cached CSV
    python scripts/winrate.py --symbol BTCUSDT --days 30 --refresh

    # test other parameter values
    python scripts/winrate.py --symbol ETHUSDT --params '{"hvb_step": 2.0, "confluence_window": 1}'

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


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest win rate on a 30-day window")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--timeframes", default="1m,3m,5m,15m,30m")
    ap.add_argument("--strategy", default="tdi_shift")
    ap.add_argument("--params", default="{}", help="JSON overrides for the strategy")
    ap.add_argument("--refresh", action="store_true", help="ignore caches and re-download")
    ap.add_argument("--out", default="", help="write a markdown report to this path")
    args = ap.parse_args()

    try:
        params = json.loads(args.params)
    except json.JSONDecodeError:
        print("--params must be valid JSON", file=sys.stderr)
        return 2

    result = winrate.report(
        symbol=args.symbol,
        days=args.days,
        timeframes=[t.strip() for t in args.timeframes.split(",") if t.strip()],
        strategy=args.strategy,
        params=params,
        refresh=args.refresh,
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
