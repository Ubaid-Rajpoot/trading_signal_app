#!/usr/bin/env python3
"""Scan the whole market and print where a setup is forming right now.

    # top 100 USDT pairs by turnover, the 5 scalping timeframes, real Binance data
    python scripts/scan.py --top 100 --real-only

    # a specific list, all timeframes incl. 1h/4h
    python scripts/scan.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --timeframes 5m,15m,1h

    # how many instruments are in the universe, and where it came from
    python scripts/scan.py --list-symbols --top 50

    # machine-readable (same shape the UI uses)
    python scripts/scan.py --top 50 --json > scan.json

Output is one row per instrument *and timeframe* that has something to say,
ordered SETUP -> ARMED -> FORMING -> BLOCKED. IDLE rows are hidden unless
``--all`` is passed.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import SCAN_TIMEFRAMES  # noqa: E402
from app.core.data.symbols import universe  # noqa: E402
from app.core.signals import build_signal  # noqa: E402

RANK = {"TRIGGERED": 0, "ARMED": 1, "FORMING": 2, "BLOCKED": 3, "IDLE": 4, "ERROR": 5}
LABEL = {"TRIGGERED": "SETUP", "ARMED": "ARMED", "FORMING": "FORMING", "BLOCKED": "BLOCKED", "IDLE": "-", "ERROR": "ERR"}


def scan_one(symbol: str, timeframe: str, strategy: str, params: dict, limit: int) -> dict:
    try:
        signal = build_signal(symbol, timeframe, strategy, params, limit=limit)
        setup = signal["setup"]
        regime = signal.get("regime") or {}
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": setup["status"],
            "direction": setup["direction"],
            "level": setup["level"],
            "distance_pct": setup["distance_pct"],
            "price": signal["price"],
            "regime": regime.get("state"),
            "blocked_reason": setup.get("blocked_reason") or regime.get("blocked"),
            "text": setup["text"],
            "source": signal["source"],
            "hvb_bars_ago": setup.get("hvb_bars_ago"),
            "hvb_last_bars": setup.get("hvb_last_bars"),
        }
    except Exception as exc:  # one bad symbol must not stop the scan
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "source": "-",
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan instruments x timeframes for live setups")
    ap.add_argument("--symbols", default="", help="comma-separated; default = the universe")
    ap.add_argument("--top", type=int, default=50, help="how many pairs from the universe (by turnover)")
    ap.add_argument("--quote", default="USDT")
    ap.add_argument("--timeframes", default=",".join(SCAN_TIMEFRAMES))
    ap.add_argument("--strategy", default="tdi_shift")
    ap.add_argument("--params", default="{}", help="JSON overrides for the strategy")
    ap.add_argument("--bars", type=int, default=500, help="candles per instrument per timeframe")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--all", action="store_true", help="show IDLE rows too")
    ap.add_argument("--real-only", action="store_true", help="abort unless the exchange answered")
    ap.add_argument("--list-symbols", action="store_true")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--out", default="", help="write the table to a markdown file")
    args = ap.parse_args()

    try:
        params = json.loads(args.params)
    except json.JSONDecodeError:
        print("--params must be valid JSON", file=sys.stderr)
        return 2

    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        universe_source = "given"
    else:
        info = universe(args.quote, args.top)
        symbols = info["symbols"]
        universe_source = info["source"]
        if args.list_symbols:
            print(f"{len(symbols)} symbols (source: {universe_source}):")
            print("  " + ", ".join(s.replace(args.quote, "") for s in symbols))
            return 0

    if not symbols:
        print("No symbols to scan.", file=sys.stderr)
        return 1

    tasks = [(s, tf) for s in symbols for tf in timeframes]
    if not args.json:
        print(
            f"Scanning {len(symbols)} instruments x {len(timeframes)} timeframes "
            f"({len(tasks)} checks) with {args.strategy} ...",
            file=sys.stderr,
        )

    rows: list[dict] = []
    with futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for row in pool.map(lambda t: scan_one(t[0], t[1], args.strategy, params, args.bars), tasks):
            rows.append(row)

    sources = sorted({r["source"] for r in rows if r.get("source") not in (None, "-")})
    real = bool(sources) and set(sources) <= {"live", "cache"}

    if args.real_only and not real:
        print(
            "FAILED: no real exchange data. Every value would come from the demo feed, "
            "which cannot produce a genuine volume breakout. Check the connection or use "
            "python scripts/winrate.py --explain to see why Binance was unreachable.",
            file=sys.stderr,
        )
        return 1

    rows.sort(key=lambda r: (RANK.get(r["status"], 9), r["symbol"], r["timeframe"]))
    shown = [r for r in rows if args.all or r["status"] not in ("IDLE", "ERROR")]

    if args.json:
        print(
            json.dumps(
                {
                    "generated_at": int(datetime.now(timezone.utc).timestamp() * 1000),
                    "strategy": args.strategy,
                    "timeframes": timeframes,
                    "symbols": symbols,
                    "universe_source": universe_source,
                    "sources": sources,
                    "is_real_data": real,
                    "rows": rows,
                },
                indent=2,
            )
        )
        return 0

    header = f"{args.strategy} · {len(symbols)} instruments · {', '.join(timeframes)}"
    print()
    print(header)
    print("=" * min(len(header), 110))
    if not shown:
        print("Nothing to report: no setup forming and nothing blocked on this window.")
    else:
        print(
            f"{'STATUS':<9}{'INSTRUMENT':<12}{'TF':<5}{'SIDE':<6}{'PRICE':>13}{'LEVEL':>13}"
            f"{'DIST':>8}{'HVB':>8}  MARKET / REASON"
        )
        for r in shown:
            if r["status"] == "ERROR":
                print(f"{'ERR':<9}{r['symbol']:<12}{r['timeframe']:<5}  {r.get('error', '')[:80]}")
                continue
            price = f"{r['price']:,.2f}" if r.get("price") else "-"
            level = f"{r['level']:,.2f}" if r.get("level") else "-"
            dist = f"{r['distance_pct']:+.2f}%" if r.get("distance_pct") is not None else "-"
            note = r.get("blocked_reason") or r.get("regime") or ""
            hvb = r.get("hvb_last_bars")
            hvb_text = "-" if hvb is None else ("never" if hvb < 0 else f"{hvb}b")
            print(
                f"{LABEL.get(r['status'], r['status']):<9}{r['symbol']:<12}{r['timeframe']:<5}"
                f"{(r.get('direction') or '-'):<6}{price:>13}{level:>13}{dist:>8}{hvb_text:>8}  {note}"
            )

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    per_tf: dict[str, int] = {}
    for r in rows:
        if r["status"] in ("TRIGGERED", "ARMED", "FORMING"):
            per_tf[r["timeframe"]] = per_tf.get(r["timeframe"], 0) + 1

    print()
    print("Status: " + "  ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: RANK.get(kv[0], 9))))
    if per_tf:
        print("Setup-forming per timeframe: " + "  ".join(f"{k} {v}" for k, v in sorted(per_tf.items())))
    print(f"Data: {', '.join(sources) or '-'}")
    if not real:
        print(
            "DEMO DATA: the exchange was unreachable, so these candles are synthetic. "
            "The demo feed has no real volume, so treat HVBs here as a code check only."
        )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"# Market scan — {args.strategy}",
            "",
            f"- Generated: {stamp}",
            f"- Instruments: {len(symbols)} ({universe_source})",
            f"- Timeframes: {', '.join(timeframes)}",
            f"- Data: **{', '.join(sources) or '-'}**",
            "",
            "| Status | Instrument | TF | Side | Price | Level | Distance | Market / reason |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in shown:
            if r["status"] == "ERROR":
                continue
            lines.append(
                f"| {LABEL.get(r['status'], r['status'])} | {r['symbol']} | {r['timeframe']} | "
                f"{r.get('direction') or '-'} | {r.get('price')} | {r.get('level')} | "
                f"{r.get('distance_pct')} | {r.get('blocked_reason') or r.get('regime') or ''} |"
            )
        if not real:
            lines += ["", "> DEMO DATA — not real market candles."]
        out.write_text("\n".join(lines))
        print(f"\nMarkdown written to {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
