#!/usr/bin/env python3
"""TDIGM break + HVB scanner - one command to run it.

    python run.py check                 # can this machine reach the exchange?
    python run.py serve                 # LIVE: websocket stream, real time
    python run.py replay data/btc 40    # replay real recorded candles at 40 bars/s

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

from bot.engine import Engine  # noqa: E402
from bot.feed import BINANCE_HOSTS, FeedError, LiveFeed, ReplayFeed  # noqa: E402
from bot.server import create_app  # noqa: E402

CONFIG_PATH = ROOT / "config.json"


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        raise SystemExit(f"missing {path}")
    return json.loads(path.read_text())


def make_engine(config: dict) -> Engine:
    return Engine(
        symbols=config["symbols"],
        timeframes=config["timeframes"],
        tdi_settings=config["tdi"],
        break_lookback=config.get("break_lookback", 20),
        hvb_min_points=config.get("hvb_min_points", 0),
    )


async def check(config: dict) -> int:
    symbol = config["symbols"][0]
    print(f"1) REST history  ({' / '.join(BINANCE_HOSTS)}) ...")
    feed = LiveFeed([symbol])
    try:
        candles = await feed.history(symbol, "1m", 10)
        last = candles[-1]
        print(f"   OK  {len(candles)} candles, last close {last['close']} at {last['close_time']}")
    except Exception as exc:
        print(f"   FAIL  {type(exc).__name__}: {exc}")
        print("   -> the exchange REST API is not reachable from this machine.")
        return 1

    print("2) websocket stream ...")
    try:
        import websockets

        url = (
            "wss://stream.binance.com:9443/stream?streams="
            + f"{symbol.lower()}@kline_1m"
        )
        async with websockets.connect(url, open_timeout=15) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            message = json.loads(raw)
            kline = message["data"]["k"]
            print(f"   OK  live kline {symbol} {kline['c']} closed={kline['x']}")
    except Exception as exc:
        print(f"   FAIL  {type(exc).__name__}: {exc}")
        print("   -> the websocket is blocked. Try a VPN, or run `python run.py replay`.")
        return 1

    print("\nEverything is reachable: `python run.py serve` will stream live.")
    return 0


def serve(config: dict, feed, source: str) -> None:
    engine = make_engine(config)
    engine.source = source
    available = sorted({symbol for symbol, _ in getattr(feed, "files", [])})
    if source == "replay" and available:
        # A replay can only speak for the instruments it has files for; empty
        # rows would look like the bot is broken.
        engine.restrict_symbols(available)
    app = create_app(config, feed, engine)
    server = config.get("server", {})
    host = server.get("host", "0.0.0.0")
    port = int(server.get("port", 8700))
    print(f"source={source}  symbols={len(engine.symbols)}  timeframes={engine.timeframes}")
    print(f"open http://localhost:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> int:
    ap = argparse.ArgumentParser(description="TDIGM break + HVB live scanner")
    ap.add_argument("command", choices=["check", "serve", "replay"])
    ap.add_argument("data", nargs="?", default="data/btc", help="replay: folder of real CSV files")
    ap.add_argument("speed", nargs="?", type=float, default=30.0, help="replay: bars per second")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--symbols", default="", help="comma-separated override, e.g. BTCUSDT,ETHUSDT")
    args = ap.parse_args()

    config = load_config(Path(args.config))
    if args.symbols:
        config["symbols"] = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.command == "check":
        return asyncio.run(check(config))

    if args.command == "serve":
        serve(config, LiveFeed(config["symbols"]), "live")
        return 0

    data_dir = Path(args.data)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    try:
        feed = ReplayFeed(data_dir, config["symbols"], speed=args.speed, loop=True)
    except FeedError as exc:
        print(f"replay error: {exc}")
        return 2
    serve(config, feed, "replay")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
