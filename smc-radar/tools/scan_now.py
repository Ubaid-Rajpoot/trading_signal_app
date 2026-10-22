#!/usr/bin/env python3
"""One-shot scan: ask the exchange for candles, print what is forming right now.

    python tools/scan_now.py                       # every instrument in config.json
    python tools/scan_now.py --preset top20        # the 20 biggest MEXC pairs
    python tools/scan_now.py --symbols BTCUSDT,ETHUSDT --json out.json
    python tools/scan_now.py --replay ../smc-radar/data/btc   # a recorded folder

The point of this tool is reproducibility: the same wandb-free, dependency-free
command that produced a table can be run again later and will print the same
numbers, because it re-reads the same candles from the same place.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import config_store  # noqa: E402
from bot.buckets import TIMEFRAMES, Rollup  # noqa: E402
from bot.feed import FeedError, make_feed  # noqa: E402
from bot.map import MarketMap  # noqa: E402

CONFIG_PATH = ROOT / "config.json"
BARS_WANTED = 400


async def scan_symbol(feed, symbol: str, timeframes: list[str], settings: dict, minutes_per_bar: int = 0) -> list[dict]:
    zones: list[dict] = []
    for tf in timeframes:
        interval = feed.exchange_interval(tf)
        rollup = Rollup(tf) if tf != "1m" else None
        try:
            if tf == "1m" or interval is None:
                bars = await feed.history(symbol, "1m", BARS_WANTED * (TIMEFRAMES[tf] // 60_000) if tf != "1m" else BARS_WANTED)
            else:
                bars = await feed.history(symbol, interval, BARS_WANTED)
        except Exception as exc:
            print(f"  {symbol} {tf}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        map_ = MarketMap(symbol, tf, settings)
        if rollup is None:
            for bar in bars:
                map_.add(bar)
        else:
            for bar in bars:
                for closed in rollup.add(bar):
                    map_.add(closed)
        for zone in map_.active():
            zones.append(zone.as_dict())
    return zones


def replay_rows(folder: Path) -> dict[str, list[dict]]:
    """Read recorded 1m CSV files (``SYMBOL-...csv``) from a folder."""
    out: dict[str, list[dict]] = {}
    for path in sorted(folder.rglob("*.csv")):
        symbol = path.name.split("-")[0].upper()
        rows = out.setdefault(symbol, [])
        with path.open(newline="") as handle:
            for row in csv.reader(handle):
                if len(row) < 6:
                    continue
                open_time = int(row[0])
                open_time = open_time // 1000 if open_time > 10**14 else open_time
                rows.append(
                    {
                        "symbol": symbol,
                        "open_time": open_time,
                        "close_time": open_time + 59_999,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                    }
                )
    return out


def scan_recorded(symbol: str, rows: list[dict], timeframes: list[str], settings: dict) -> list[dict]:
    zones: list[dict] = []
    for tf in timeframes:
        map_ = MarketMap(symbol, tf, settings)
        if tf == "1m":
            for bar in rows:
                map_.add(bar)
        else:
            rollup = Rollup(tf)
            for bar in rows:
                for closed in rollup.add(bar):
                    map_.add(closed)
        zones += [zone.as_dict() for zone in map_.active()]
    return zones


async def main_async(args) -> int:
    if args.replay:
        return run_replay(args)
    return await run_live(args)


def run_replay(args) -> int:
    folder = Path(args.replay)
    if not folder.is_absolute():
        folder = ROOT / folder
    data = replay_rows(folder)
    if not data:
        print(f"no CSV files in {folder}", file=sys.stderr)
        return 2
    config = config_store.read(CONFIG_PATH)
    timeframes = args.timeframes or config["timeframes"]
    zones: list[dict] = []
    print(f"replay · {len(data)} recorded instrument(s) · timeframes {', '.join(timeframes)} · {folder}")
    for symbol, rows in sorted(data.items()):
        found = scan_recorded(symbol, rows, timeframes, config["settings"])
        zones += found
        print(f"  {symbol:<14} {len(rows):>6} 1m bars  {len(found):>3} active zone(s)")
    return report(zones, "replay", timeframes, args.json)


async def run_live(args) -> int:
    config = config_store.read(CONFIG_PATH)
    settings = config["settings"]
    timeframes = args.timeframes or config["timeframes"]
    exchange = args.exchange or config["exchange"]
    feed = make_feed(exchange, [])

    symbols = config_store.clean_symbols(args.symbols.split(",")) if args.symbols else list(config["symbols"])
    if args.preset:
        try:
            items = await feed.universe()
        except Exception as exc:
            print(f"cannot read the exchange list: {exc}", file=sys.stderr)
            return 2
        count = None if args.preset == "all" else int(args.preset.replace("top", "") or 20)
        symbols = [item["symbol"] for item in items[:count]] if count else [item["symbol"] for item in items]

    print(f"{len(symbols)} instrument(s) · {len(timeframes)} timeframe(s) on {exchange}: {', '.join(timeframes)}")
    all_zones: list[dict] = []
    for symbol in symbols:
        found = await scan_symbol(feed, symbol, timeframes, settings)
        all_zones += found
        if found:
            print(f"  {symbol:<14} {len(found):>3} active zone(s)")

    return report(all_zones, exchange, timeframes, args.json)


def report(all_zones: list[dict], source: str, timeframes: list[str], json_out: str) -> int:
    all_zones.sort(key=lambda z: (z["kind"], z["symbol"], -z["time"]))
    print()
    if not all_zones:
        print("nothing active right now")
    else:
        header = f"{'kind':<22}{'symbol':<12}{'tf':<5}{'level':>16}   {'block':<32}{'formed (UTC)':<16}ev"
        print(header)
        print("-" * len(header))
        for z in all_zones:
            from datetime import datetime, timezone

            when = datetime.fromtimestamp(z["time"] / 1000, tz=timezone.utc).strftime("%d %b %H:%M")
            block = f"{z['bottom']:,.4f} – {z['top']:,.4f}"
            print(f"{z['kind']:<22}{z['symbol']:<12}{z['timeframe']:<5}{z['level']:>16,.6f}   {block:<32}{when:<16}{len(z['evidence'])}")

    if json_out:
        Path(json_out).write_text(json.dumps({"source": source, "timeframes": timeframes, "zones": all_zones}, indent=2))
        print(f"\nwritten to {json_out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="scan every instrument once and print the active zones")
    ap.add_argument("--symbols", default="", help="comma separated, defaults to config.json")
    ap.add_argument("--preset", default="", help="top10 / top20 / top50 / all")
    ap.add_argument("--timeframes", nargs="*", default=None, help="1m 3m 5m 15m 30m 1h")
    ap.add_argument("--exchange", default="", help="mexc (default) or binance")
    ap.add_argument("--json", default="", help="write the result as JSON")
    ap.add_argument("--replay", default="", help="scan a folder of recorded 1m CSV files instead of the exchange")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except FeedError as exc:
        print(f"feed error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
