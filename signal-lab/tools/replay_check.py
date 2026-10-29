#!/usr/bin/env python3
"""Replay 2024-2026 through the *app's* engine, headless, and print what it recorded.

This is the bridge between the research tables and the running page: it uses
`app/engine.py` exactly as the server does - the validation model (trained up to
2023-12-31), per-timeframe geometry, next-open fills, first-barrier resolution,
one entry per stream per horizon - and reports the same statistics the page shows.

    python tools/replay_check.py                      # full 2024-2026 window
    python tools/replay_check.py --max-steps 20000    # a quick slice
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

import config as appconfig  # noqa: E402
from engine import Engine  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--tfs", default="")
    ap.add_argument("--max-steps", type=int, default=0)
    ap.add_argument("--db", default="/tmp/replay_check.db")
    args = ap.parse_args()

    cfg = appconfig.load()
    tfs = [t for t in (args.tfs.split(",") if args.tfs else cfg.timeframes)]
    if os.path.exists(args.db):
        os.remove(args.db)
    e = Engine(mode="replay", tfs=tfs, symbols=args.symbols.split(","), config=cfg,
               db=args.db)
    e.prepare()
    print(f"replaying {', '.join(args.symbols.split(','))} on {', '.join(e.tfs)} "
          f"({e.model_label})", flush=True)
    t0 = time.time()
    n = 0
    while e.step():
        n += 1
        if n % 5000 == 0:
            print(f"  {n} bars, {len(e.memory)} signals, {time.time()-t0:.0f}s", flush=True)
        if args.max_steps and n >= args.max_steps:
            break
    s = e.snapshot(limit=10_000)
    allr = s["stats"]["all"]
    print(f"\nbars walked      {n:,} in {time.time()-t0:.0f}s")
    print(f"signals recorded {allr['n']}")
    print(f"resolved         {allr['won']} won / {allr['lost']} lost / {allr['expired']} expired")
    if allr["accuracy"] is not None:
        print(f"accuracy         {100*allr['accuracy']:.1f} %  "
              f"(break-even {100*(allr['break_even'] or 0):.1f} %)")
    print(f"expectancy       {allr['expectancy']:+.3f} R per trade, {allr['net_r']:+.1f} R net")
    print("\nby timeframe")
    for tf, st in s["stats"]["by_tf"].items():
        print(f"  {tf:>4}  n={st['n']:>4}  win={100*(st['accuracy'] or 0):>5.1f} %  "
              f"EV={(st['expectancy'] if st['expectancy'] is not None else 0):+.3f} R  "
              f"(break-even {100*(st['break_even'] or 0):.1f} %)")
    print("\nby band")
    for b, st in s["stats"]["bands"].items():
        print(f"  {b:>2}  n={st['n']:>4}  win={100*(st['accuracy'] or 0):>5.1f} %  "
              f"EV={(st['expectancy'] if st['expectancy'] is not None else 0):+.3f} R")


if __name__ == "__main__":
    main()
