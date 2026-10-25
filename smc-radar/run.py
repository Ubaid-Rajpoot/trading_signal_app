#!/usr/bin/env python3
"""SMC radar - one command to run it.

    python run.py check                     # can this machine reach the exchange?
    python run.py serve                     # LIVE stream (exchange from config.json)
    python run.py serve --exchange mexc
    python run.py serve --symbols BTCUSDT,ETHUSDT
    python run.py replay ../tdigm-live/data/btc 40   # replay real recorded candles
    python run.py universe --exchange mexc  # list the exchange's pairs, biggest first

The page is served on http://localhost:8701 (configurable in config.json).
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
from bot.engine import Engine  # noqa: E402
from bot.feed import (  # noqa: E402
    FeedError,
    MexcFuturesFeed,
    ReplayFeed,
    is_futures,
    make_feed,
    market_of,
    make_market_feed,
    to_futures,
)
from bot.server import create_app  # noqa: E402

CONFIG_PATH = ROOT / "config.json"


def load_config(path: Path = CONFIG_PATH) -> dict:
    try:
        return config_store.read(path)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")


def markets_of(config: dict) -> list[str]:
    """Which markets this configuration scans. Round 9: futures only."""
    wanted = [m for m in (config.get("markets") or []) if m in ("spot", "futures")]
    if wanted:
        return wanted
    # no explicit choice: infer it from the symbols, futures-first
    if config.get("futures_symbols"):
        return ["futures"]
    return ["spot"] if config.get("symbols") else ["futures"]


def market_symbols(config: dict) -> list[str]:
    """The instruments the engine scans, filtered to the enabled markets."""
    markets = markets_of(config)
    spot = [str(s) for s in (config.get("symbols") or [])] if "spot" in markets else []
    futures = [to_futures(s) for s in (config.get("futures_symbols") or [])] if "futures" in markets else []
    spot = [s for s in spot if not is_futures(s)]
    return list(dict.fromkeys(spot + futures))


def make_engine(config: dict) -> Engine:
    return Engine(
        symbols=market_symbols(config),
        timeframes=config["timeframes"],
        settings=config.get("settings"),
        preview_max_symbols=int(config.get("preview_max_symbols", 200)),
    )


async def check(config: dict, exchange: str) -> int:
    markets = markets_of(config)
    if exchange == "mexc" and "futures" in markets and (config.get("futures_symbols") or []):
        spot = [s for s in (config.get("symbols") or []) if not is_futures(s)]
        futures = [to_futures(s) for s in (config.get("futures_symbols") or [])]
        print("0) MEXC futures REST ...")
        try:
            feed = MexcFuturesFeed(futures[:1] or ["BTC_USDT"])
            bars = await feed.history(feed.symbols[0], "1m", 10)
            print(f"   OK  {len(bars)} futures candles, last close {bars[-1]['close']} ({feed.symbols[0]})")
        except Exception as exc:
            print(f"   FAIL  {type(exc).__name__}: {exc}")
            print("   -> futures REST is not reachable; this app needs it to scan anything")
        config = {**config, "symbols": spot}
    # the universe on both markets: this is what the "add instruments" panel uses
    if exchange == "mexc":
        print(f"0b) MEXC instrument list ({', '.join(markets)}) ...")
        try:
            lookup = make_market_feed(
                "mexc",
                [] if "spot" not in markets else
                ([s for s in (config.get("symbols") or []) if not is_futures(s)][:1] or ["BTCUSDT"]),
                ([to_futures(s) for s in (config.get("futures_symbols") or [])][:1] or ["BTC_USDT"])
                if "futures" in markets else [],
            )
            items = await lookup.universe()
            per_market = {}
            for item in items:
                per_market[item.get("market") or market_of(item["symbol"])] = per_market.get(
                    item.get("market") or market_of(item["symbol"]), 0
                ) + 1
            print(f"   OK  {len(items)} instruments: " + ", ".join(f"{k} {v}" for k, v in sorted(per_market.items())))
            errors = getattr(lookup, "universe_errors", {}) or {}
            for market, problem in errors.items():
                print(f"   FAIL {market}: {problem}")
        except Exception as exc:
            print(f"   FAIL  {type(exc).__name__}: {exc}")

    symbol = (config["symbols"][0] if config.get("symbols") else "BTCUSDT") if "spot" in markets else (
        (config.get("futures_symbols") or ["BTC_USDT"])[0])
    print(f"1) {exchange} REST history for {symbol} ...")
    feed = make_market_feed(exchange, [] if "spot" not in markets else [symbol],
                            [to_futures(symbol)] if "futures" in markets else [])
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
        got: asyncio.Queue = asyncio.Queue()

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


async def universe(exchange: str, top: int, market: str = "spot") -> int:
    if market == "futures":
        feed = MexcFuturesFeed([])
    else:
        feed = make_feed(exchange, [])
    try:
        items = await feed.universe()
    except Exception as exc:
        print(f"FAIL {type(exc).__name__}: {exc}")
        return 1
    if market == "futures":
        items = [i for i in items if is_futures(i["symbol"])]
    print(f"{len(items)} USDT {market} instruments on {exchange} (by 24h turnover):")
    for item in items[:top]:
        print(f"  {item['symbol']:<16} {item['price']:>14,.8g}  {item['change']:>7.2f}%  {item['turnover']:>16,.0f}")
    print("\nAdd any of them from the web page, or: python run.py serve --symbols BTCUSDT,ETHUSDT")
    return 0


def serve(
    config: dict,
    feed,
    source: str,
    config_path: Path,
    port: int | None = None,
    host: str | None = None,
    preroll: int = 0,
) -> None:
    engine = make_engine(config)
    engine.source = source
    engine.exchange = getattr(feed, "name", "mexc")
    if source == "replay":
        # A replay can only speak for the instruments it has files for; empty
        # rows would look like the app is broken.
        available = sorted({symbol for symbol, _ in getattr(feed, "files", [])})
        if available:
            engine.set_symbols(available)
        if preroll:
            # A replay should open on a filled chart, like the live app does.
            loaded = engine.preroll_feed(feed, preroll)
            print(f"pre-roll: {loaded} recorded candles loaded before streaming")
    app = create_app(config, feed, engine, config_path)
    server = config.get("server", {})
    host = host or server.get("host", "0.0.0.0")
    port = int(port or server.get("port", 8800))
    print(f"source={source}  exchange={engine.exchange}  symbols={len(engine.symbols)}  timeframes={engine.timeframes}")
    print(f"open http://localhost:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> int:
    ap = argparse.ArgumentParser(description="SMC radar - strong highs, algo candles, rejection blocks")
    ap.add_argument("command", choices=["check", "serve", "replay", "universe"])
    ap.add_argument("data", nargs="?", default="data/btc", help="replay: folder of real CSV files")
    ap.add_argument("speed", nargs="?", type=float, default=40.0, help="replay: bars per second")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--symbols", default="", help="comma-separated override, e.g. BTCUSDT,ETHUSDT")
    ap.add_argument("--exchange", default="", help="mexc (default) or binance")
    ap.add_argument("--top", type=int, default=20, help="universe: how many to print")
    ap.add_argument("--futures", default="", help="comma-separated futures contracts, e.g. BTC_USDT,ETH_USDT")
    ap.add_argument("--market", default="", choices=["", "spot", "futures"], help="universe: which market to list")
    ap.add_argument("--port", type=int, default=0, help="override the port from config.json")
    ap.add_argument("--host", default="", help="override the host from config.json")
    ap.add_argument("--preroll", type=int, default=2400, help="replay: candles loaded before streaming starts")
    ap.add_argument("--ticks", type=int, default=4, help="replay: partial updates per candle, so candles form visibly")
    args = ap.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    if args.symbols:
        config["symbols"] = [s for s in config_store.clean_symbols(args.symbols.split(",")) if not is_futures(s)]
        config["markets"] = ["spot"] if not config.get("futures_symbols") else ["spot", "futures"]
    if args.futures:
        config["futures_symbols"] = [to_futures(s) for s in config_store.clean_symbols(args.futures.split(","))]
        config["markets"] = ["futures"] if not config.get("symbols") else ["spot", "futures"]
    if args.exchange:
        config["exchange"] = args.exchange
    exchange = str(config.get("exchange", "mexc")).lower()

    if args.command == "check":
        return asyncio.run(check(config, exchange))

    if args.command == "universe":
        return asyncio.run(universe(exchange, args.top, args.market or ("futures" if "futures" in markets_of(config) else "spot")))

    if args.command == "serve":
        try:
            markets = markets_of(config)
            feed = make_market_feed(
                exchange,
                [s for s in config.get("symbols", []) if not is_futures(s)] if "spot" in markets else [],
                [to_futures(s) for s in (config.get("futures_symbols") or [])] if "futures" in markets else [],
            )
        except FeedError as exc:
            print(f"feed error: {exc}")
            return 2
        serve(config, feed, "live", config_path, args.port or None, args.host or None)
        return 0

    data_dir = Path(args.data)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    try:
        feed = ReplayFeed(
            data_dir,
            market_symbols(config),
            speed=args.speed,
            loop=True,
            skip=max(0, args.preroll),
            ticks=max(1, args.ticks),
        )
    except FeedError as exc:
        print(f"replay error: {exc}")
        return 2
    serve(config, feed, "replay", config_path, args.port or None, args.host or None, max(0, args.preroll))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
