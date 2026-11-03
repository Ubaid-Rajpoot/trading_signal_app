#!/usr/bin/env python3
"""Fill the archive from recorded candles - the same engine, the same filters.

The live page only ever records what it *sees*. A fresh install therefore shows
an empty history until the market hands it a setup, and a restart is only safe if
the archive was already there. This tool replays the recorded CSVs through the
real engine, writes every setup into the database, and then walks each one
forward so its outcome (win / stopped) is stored too - not guessed at, not
backfilled from a formula, but the actual next candles.

    python tools/backfill_archive.py data/btc --symbol BTCUSDT
    python tools/backfill_archive.py data/btc --symbol BTCUSDT --timeframes 15m,1h --dry-run

It is exactly ``tools/scan_history.py`` plus a database, so a signal it writes to
the archive is a signal the live bot would have shown. ``--dry-run`` prints what
it would add and touches nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.engine import Engine  # noqa: E402
from bot.store import SignalStore  # noqa: E402
from tools.scan_history import read_candles  # noqa: E402

TDI = {"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9,
       "band_mult": 1.6185, "hvb_reference": "mbl"}


async def replay(args: argparse.Namespace, candles: list[dict], store: SignalStore | None) -> dict:
    engine = Engine(
        symbols=[args.symbol],
        timeframes=args.timeframes.split(","),
        tdi_settings=TDI,
        filters={
            "trend_context": args.trend_context,
            "require_hh_hl": args.trend_context,
            "clear_structure": args.trend_context,
            "min_trend_steps": 2,
            "min_directional_ratio": 0.75,
            "fresh_shift_max_bars": 20,
            "require_bos": args.trend_context,
            "max_internal_depth": 2,
            "hvb_window": args.hvb_window,
            "setup_target_rr": 4.0,
            "breaker_body_atr": 1.0,
            "breaker_stop_buffer_atr": 0.10,
            "confirm_mode": "all",
            "regime": not args.no_regime,
            "regime_adx_min": 22.0,
            "regime_er_min": 0.30,
        },
        structure={"pivot_len": 5, "major_len": 20, "macro_len": 80, "sweep_max_wait": 60,
                   "target_mode": "next"},
    )
    if store is not None:
        engine.attach_store(store)
        engine.market = "futures"
    await engine.warmup(type("Feed", (), {"history": _empty, "exchange_interval": lambda s, tf: tf})())
    for candle in candles:
        await engine.on_candle(candle)
    return dict(engine.setup_payloads)


def _empty(self, symbol: str, timeframe: str) -> list[dict]:
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default="data/btc")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframes", default="5m,15m,30m,1h")
    ap.add_argument("--hvb-window", type=int, default=0,
                    help="0 keeps the shipped same-candle body-break + HVB rule")
    ap.add_argument("--db", default=str(ROOT / "data" / "tdigm.sqlite3"))
    ap.add_argument("--dry-run", action="store_true")
    trend = ap.add_mutually_exclusive_group()
    trend.add_argument("--trend-context", dest="trend_context", action="store_true", default=True,
                       help="apply the shipped strict structural-context gate (default)")
    trend.add_argument("--no-trend-context", dest="trend_context", action="store_false",
                       help="research only: disable structural-context and BOS gates")
    ap.add_argument("--no-regime", action="store_true")
    args = ap.parse_args()

    folder = Path(args.folder)
    if not folder.is_absolute():
        folder = ROOT / folder
    candles = read_candles(folder, args.symbol)
    if not candles:
        print(f"no CSV files for {args.symbol} in {folder}")
        return 1

    store = None if args.dry_run else SignalStore(args.db)
    if store is not None and not store.enabled:
        print(f"cannot open {args.db}: {store.problem}")
        return 1

    setups = asyncio.run(replay(args, candles, store))
    print(f"{args.symbol}: {len(candles)} 1m bars -> {len(setups)} setups"
          + (" (dry run, nothing written)" if args.dry_run else f" -> {args.db}"))

    # The engine has already advanced every setup over every later candle using
    # the exact live lifecycle: pending until mitigation, then fixed 4R/stop.
    # A second formula here used to overwrite that truth with a synthetic 2R
    # result, including setups that were never filled.
    states: Counter[str] = Counter(payload.get("state") or "pending" for payload in setups.values())
    print(f"outcomes: {dict(states)}")
    if store is not None:
        print(f"archive now holds {store.counts().get('total', 0)} rows")
        print(store.results().get("all"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
