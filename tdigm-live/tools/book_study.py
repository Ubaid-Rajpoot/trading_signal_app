#!/usr/bin/env python3
"""Gate-by-gate study: what does each book confluence change?

Runs the real engine over recorded candles for several filter combinations, in
parallel worker processes, and prints one row per combination. This is the
script behind the tables in ``docs/book-coverage.md`` - nothing here is a
shortcut around the live engine.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from scan_history import _bars_by_timeframe, _bucket, _outcome, read_candles  # noqa: E402

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h"]

BASE_FILTERS = {
    "structure": True,
    "hvb": True,
    "hvb_min_points": 1.0,
    "hvb_min_speed": 1.5,
    "regime": True,
    "regime_adx_min": 20.0,
    "regime_er_min": 0.10,
    "regime_direction": "any",
    "premium_discount": False,
    "structure_direction": False,
    "require_ms": False,
    "require_fvg": False,
    "block_liquidity_pool": False,
    "pool_count": 3,
    "structure_atr_min": 0.0,
    "structure_atr_max": 0.0,
}

GATES: dict[str, dict] = {
    "baseline (no book gates)": {},
    "premium at the SWEEP (p.42)": {"premium_discount": True},
    "premium at the ENTRY (p.42)": {"premium_discount": True, "premium_on_entry": True},
    "liquidity pool block": {"block_liquidity_pool": True},
    "sweep premium + pool": {"premium_discount": True, "block_liquidity_pool": True},
    "premium 0.62 / 0.38": {"premium_discount": True, "premium_level": 0.62},
    "structure direction (p.53)": {"structure_direction": True},
    "MS only (p.53)": {"require_ms": True},
    "FVG in the leg (p.87)": {"require_fvg": True},
    "structure 0.4-6 ATR": {"structure_atr_min": 0.4, "structure_atr_max": 6.0},
    "dir + pool": {"structure_direction": True, "block_liquidity_pool": True},
    "dir + pool + FVG": {"structure_direction": True, "block_liquidity_pool": True, "require_fvg": True},
    "dir + pool + premium": {"structure_direction": True, "block_liquidity_pool": True, "premium_discount": True},
    "dir + pool + MS": {"structure_direction": True, "block_liquidity_pool": True, "require_ms": True,
                        "pool_count": 3},
}


def run_one(args: tuple[str, str, dict]) -> tuple[str, dict]:
    folder, label, extra = args
    candles = read_candles(Path(folder), "BTCUSDT")
    bars = _bars_by_timeframe(candles, TIMEFRAMES)

    from bot.engine import Engine  # imported in the worker

    captured: list[dict] = []

    async def collect(message: dict) -> None:
        if message.get("type") == "alert" and message["alert"]["kind"] == "signal":
            captured.append(message["alert"])

    engine = Engine(
        broadcast=collect,
        symbols=["BTCUSDT"],
        timeframes=TIMEFRAMES,
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1,
                      "slow_period": 9, "band_mult": 1.6185, "hvb_reference": "mbl"},
        filters={**BASE_FILTERS, **extra},
        structure={"pivot_len": 5, "major_len": 20, "sweep_max_wait": 60, "target_mode": "range"},
    )

    async def go() -> None:
        await engine.warmup(type("Feed", (), {
            "history": lambda *a, **k: _empty(),
            "exchange_interval": lambda s, tf: tf,
        })())
        for candle in candles:
            await engine.on_candle(candle)

    asyncio.run(go())
    rows = [(res, r) for a in captured for res, r in [_outcome(a, bars[a["timeframe"]], 300)]]
    stats = _bucket(rows)
    days = (candles[-1]["close_time"] - candles[0]["open_time"]) / 86_400_000
    stats["per_day"] = len(captured) / max(days, 1)
    return label, stats


async def _empty():
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default="data/btc")
    ap.add_argument("--workers", type=int, default=5)
    args = ap.parse_args()

    folder = args.folder if Path(args.folder).is_absolute() else str(ROOT / args.folder)
    jobs = [(folder, label, extra) for label, extra in GATES.items()]
    print(f"{'gate':<30}{'n':>5}{'win':>5}{'loss':>5}{'open':>5}{'win%':>7}{'per/day':>9}{'totalR':>8}{'R/tr':>7}")
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for label, stats in pool.map(run_one, jobs):
            print(f"{label:<30}{stats['n']:>5}{stats['wins']:>5}{stats['losses']:>5}{stats['open']:>5}"
                  f"{(stats['win_rate'] * 100 if stats['scored'] else 0):>6.1f}%{stats['per_day']:>9.2f}"
                  f"{stats['total_r']:>8.2f}{stats['expectancy']:>7.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
