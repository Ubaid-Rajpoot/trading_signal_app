#!/usr/bin/env python3
"""TDIGM liquidity scanner - one command to run it.

    python run.py check                     # can this machine reach the exchange?
    python run.py serve                     # LIVE stream (exchange from config.json)
    python run.py serve --exchange mexc     # LIVE from MEXC
    python run.py serve --symbols BTCUSDT,ETHUSDT
    python run.py replay data/btc 40        # replay real recorded candles at 40 bars/s
    python run.py replay data/btc 40 --skip 20000   # start 20,000 minutes in
    python run.py universe --exchange mexc  # list the exchange's pairs, biggest first

The page is served on http://localhost:8700 (configurable in config.json).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from bot import config_store  # noqa: E402
from bot.buckets import TIMEFRAMES  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.feed import FeedError, ReplayFeed, make_feed  # noqa: E402
from bot.server import create_app  # noqa: E402

CONFIG_PATH = ROOT / "config.json"


def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        return config_store.read(path)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")


def make_engine(config: dict) -> Engine:
    return Engine(
        symbols=config["symbols"],
        timeframes=config["timeframes"],
        tdi_settings=config["tdi"],
        break_lookback=config.get("break_lookback", 20),
        hvb_min_points=config.get("hvb_min_points", 0),
        filters=config.get("filters"),
        structure=config.get("structure"),
        preview_max_symbols=int(config.get("preview_max_symbols", 200)),
    )


async def check(config: dict, exchange: str) -> int:
    symbol = config["symbols"][0] if config.get("symbols") else "BTCUSDT"
    print(f"1) {exchange} REST history for {symbol} ...")
    feed = make_feed(exchange, [symbol])
    try:
        candles = await feed.history(symbol, "1m", 10)
        last = candles[-1]
        print(f"   OK  {len(candles)} closed candles, last close {last['close']} at {last['close_time']}")
    except Exception as exc:
        print(f"   FAIL  {type(exc).__name__}: {exc}")
        print("   -> the exchange REST API is not reachable from this machine.")
        return 1

    print(f"2) {exchange} websocket stream ...")
    try:
        got = asyncio.Queue()

        async def one() -> None:
            async for candle in feed.candles():
                await got.put(candle)
                return

        task = asyncio.create_task(one())
        candle = await asyncio.wait_for(got.get(), timeout=30)
        task.cancel()
        print(f"   OK  live candle {candle['symbol']} @ {candle['close']} closed={candle['closed']}")
    except Exception as exc:
        print(f"   FAIL  {type(exc).__name__}: {exc}")
        print("   -> the websocket is blocked. Try a VPN, or run `python run.py replay`.")
        return 1

    print(f"\nEverything is reachable: `python run.py serve --exchange {exchange}` will stream live.")
    return 0


async def universe(config: dict, exchange: str, top: int) -> int:
    feed = make_feed(exchange, [])
    try:
        items = await feed.universe()
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    print(f"{len(items)} USDT pairs on {exchange} (by 24h turnover):")
    for item in items[:top]:
        print(f"  {item['symbol']:<16} {item['price']:>14,.8g}  {item['change']:>7.2f}%  {item['turnover']:>16,.0f}")
    print("\nAdd any of them from the web page, or: python run.py serve --symbols BTCUSDT,ETHUSDT")
    return 0


def serve(config: dict, feed, source: str, config_path: Path) -> None:
    engine = make_engine(config)
    engine.source = source
    engine.exchange = getattr(feed, "name", "binance")
    available = sorted({symbol for symbol, _ in getattr(feed, "files", [])})
    if source == "replay" and available:
        # A replay can only speak for the instruments it has files for; empty
        # rows would look like the bot is broken.
        engine.restrict_symbols(available)
    app = create_app(config, feed, engine, config_path)
    server = config.get("server", {})
    host = server.get("host", "0.0.0.0")
    port = int(server.get("port", 8700))
    print(f"source={source}  exchange={engine.exchange}  symbols={len(engine.symbols)}  timeframes={engine.timeframes}")
    print(f"open http://localhost:{port}   (the page is up now; history warms in the background)")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> int:
    ap = argparse.ArgumentParser(description="TDIGM liquidity scanner")
    ap.add_argument("command", choices=["check", "serve", "replay", "universe"])
    ap.add_argument("data", nargs="?", default="data/btc", help="replay: folder of real CSV files")
    ap.add_argument("speed", nargs="?", type=float, default=30.0, help="replay: bars per second")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--symbols", default="", help="comma-separated override, e.g. BTCUSDT,ETHUSDT")
    ap.add_argument("--exchange", default="", help="binance (default) or mexc")
    ap.add_argument("--top", type=int, default=20, help="universe: how many to print")
    ap.add_argument(
        "--skip",
        type=int,
        default=0,
        help="replay: minutes to start in (0 = enough to warm every timeframe)",
    )
    args = ap.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    if args.symbols:
        config["symbols"] = config_store.clean_symbols(args.symbols.split(","))
    if args.exchange:
        config["exchange"] = args.exchange
    exchange = str(config.get("exchange", "binance")).lower()

    if args.command == "check":
        return asyncio.run(check(config, exchange))

    if args.command == "universe":
        return asyncio.run(universe(config, exchange, args.top))

    if args.command == "serve":
        try:
            feed = make_feed(exchange, config["symbols"])
        except FeedError as exc:
            print(f"feed error: {exc}")
            return 2
        serve(config, feed, "live", config_path)
        return 0

    data_dir = Path(args.data)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    # Start deep enough into the files that every timeframe can be warmed from
    # history: a 1h cell needs 120 closed hours before it can signal, and those
    # 7,200 one-minute bars would otherwise have to be *played* first.
    skip = args.skip
    if skip <= 0:
        minutes = max(TIMEFRAMES.get(tf, 60_000) for tf in config["timeframes"]) // 60_000
        skip = int(minutes * 130)
    try:
        feed = ReplayFeed(
            data_dir, config["symbols"], speed=args.speed, loop=True, skip_bars=skip
        )
    except FeedError as exc:
        print(f"replay error: {exc}")
        return 2
    serve(config, feed, "replay", config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
