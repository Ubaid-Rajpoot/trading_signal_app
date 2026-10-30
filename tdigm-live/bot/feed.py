"""Market data: live exchange streams, or a replay of real recorded candles.

Every feed produces the same thing - **1-minute candles** - so the engine above
them cannot tell which exchange it is talking to, and cannot tell a replay from
a live market except by the label it is given:

    {"symbol": "BTCUSDT", "open_time": ms, "close_time": ms,
     "open": f, "high": f, "low": f, "close": f, "volume": f, "closed": bool}

Feeds
-----
``BinanceFeed`` : combined kline websocket, REST warm-up, N symbols per socket.
``MexcFeed``    : MEXC spot klines (``spot@public.kline.v3.api``), which push
                  *updates* - the previous candle is closed when a new one starts.
``ReplayFeed``  : real recorded CSV candles pushed at a chosen speed.

Symbols can be added and removed while the bot is running: the feeds shard their
symbol list across several sockets and reconnect only the shard that changed, so
watching 300 instruments is the same code path as watching 3.

Nothing here invents data. If a feed cannot reach the exchange it says so and
retries; it never fabricates a candle.
"""

from __future__ import annotations

import asyncio
import csv
import json
import random
import time
from pathlib import Path
from typing import AsyncIterator, Iterable

import httpx

from .buckets import TIMEFRAMES, Rollup

try:  # websockets is required for live mode only
    import websockets
except Exception:  # pragma: no cover
    websockets = None  # type: ignore


BINANCE_REST = "https://api.binance.com"
BINANCE_HOSTS = [BINANCE_REST, "https://data-api.binance.vision"]
BINANCE_WS = "wss://stream.binance.com:9443/stream"
BINANCE_TFS = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h"}

MEXC_REST = "https://api.mexc.com"
MEXC_HOSTS = [MEXC_REST]
MEXC_WS = "wss://wbs.mexc.com/ws"
MEXC_TFS = {"1m": "1m", "3m": None, "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m"}
MEXC_WS_INTERVAL = {"1m": "Min1", "5m": "Min5", "15m": "Min15", "30m": "Min30", "1h": "Min60"}


class FeedError(RuntimeError):
    pass


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------- base
class BaseFeed:
    """Common plumbing: a symbol set, a queue, and (re)connect supervision."""

    source = "live"
    name = "feed"
    chunk_size = 100
    tf_map: dict[str, str | None] = {}
    supports_history = True

    def __init__(self, symbols: Iterable[str], *, chunk_size: int | None = None, **_: object) -> None:
        self._symbols: list[str] = sorted({str(s).upper() for s in symbols})
        if chunk_size:
            self.chunk_size = max(1, int(chunk_size))
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=50_000)
        self.connected = False
        self.status = "starting"
        self.last_message = 0.0
        self.frames = 0
        self._generation = 0  # bumped when the symbol list changes

    # ------------------------------------------------------------- symbols
    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    def add_symbols(self, symbols: Iterable[str]) -> list[str]:
        wanted = {str(s).upper() for s in symbols}
        added = sorted(wanted - set(self._symbols))
        if added:
            self._symbols = sorted(set(self._symbols) | wanted)
            self._generation += 1
        return added

    def remove_symbols(self, symbols: Iterable[str]) -> list[str]:
        drop = {str(s).upper() for s in symbols}
        removed = sorted(drop & set(self._symbols))
        if removed:
            self._symbols = [s for s in self._symbols if s not in drop]
            self._generation += 1
        return removed

    def exchange_interval(self, timeframe: str) -> str | None:
        return self.tf_map.get(timeframe, timeframe)

    # ------------------------------------------------------------- history
    async def history(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        raise NotImplementedError

    async def _get_json(self, url: str, params: dict | None = None) -> object:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            raise FeedError(f"{url} -> HTTP {resp.status_code} {resp.text[:120]}")
        return resp.json()

    # -------------------------------------------------------------- stream
    async def stream(self, symbols: list[str]) -> AsyncIterator[dict]:  # pragma: no cover
        raise NotImplementedError
        yield {}

    async def candles(self) -> AsyncIterator[dict]:
        """Yield every candle from every shard, forever."""
        worker: asyncio.Task | None = None
        while True:
            generation = self._generation
            if worker is not None:
                worker.cancel()
                try:
                    await worker
                except (asyncio.CancelledError, Exception):
                    pass
            worker = asyncio.create_task(self._run_shards(generation))
            # wait until the symbol list changes (or we are cancelled)
            while generation == self._generation:
                try:
                    candle = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                yield candle

    async def _run_shards(self, generation: int) -> None:
        chunks = self._chunks()
        tasks = [asyncio.create_task(self._shard(chunk)) for chunk in chunks]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise

    def _chunks(self) -> list[list[str]]:
        symbols = list(self._symbols)
        size = self.chunk_size
        return [symbols[i : i + size] for i in range(0, len(symbols), size)] or [[]]

    async def _shard(self, symbols: list[str]) -> None:
        attempt = 0
        while self._generation >= 0:
            try:
                self.status = "connecting"
                await self.stream(symbols)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                self.connected = False
                self.status = f"reconnecting ({attempt}) {type(exc).__name__}: {str(exc)[:90]}"
                await asyncio.sleep(min(30.0, 2 ** min(attempt, 5)) + random.uniform(0, 1))

    async def _push(self, candle: dict) -> None:
        self.frames += 1
        self.last_message = time.time()
        try:
            self.queue.put_nowait(candle)
        except asyncio.QueueFull:
            # The engine is behind. Dropping the oldest is the honest option:
            # stale candles are worth less than fresh ones.
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(candle)
            except Exception:
                pass


# ------------------------------------------------------------------ binance
class BinanceFeed(BaseFeed):
    name = "binance"
    tf_map = BINANCE_TFS
    chunk_size = 400

    async def history(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        params = {"symbol": symbol.upper(), "interval": interval, "limit": min(limit, 1000)}
        last: Exception | None = None
        for host in BINANCE_HOSTS:
            try:
                rows = await self._get_json(f"{host}/api/v3/klines", params)
                now_ms = int(time.time() * 1000)
                candles = [
                    {
                        "symbol": symbol.upper(),
                        "open_time": int(r[0]),
                        "close_time": int(r[6]),
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[5]),
                        "trades": int(r[8]) if len(r) > 8 else 0,
                        "closed": True,
                    }
                    for r in rows  # type: ignore[union-attr]
                    if int(r[6]) < now_ms
                ]
                if candles:
                    return candles
                last = FeedError("exchange returned no closed candles")
            except Exception as exc:
                last = exc
        raise FeedError(f"binance history {symbol} {interval}: {last}")

    async def stream(self, symbols: list[str]) -> None:
        if websockets is None:
            raise FeedError("the 'websockets' package is not installed")
        streams = "/".join(f"{s.lower()}@kline_1m" for s in symbols)
        url = f"{BINANCE_WS}?streams={streams}"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=4096) as ws:
            self.connected = True
            self.status = f"live - {len(self._symbols)} symbols on {len(self._chunks())} socket(s)"
            async for raw in ws:
                message = json.loads(raw)
                data = message.get("data") or {}
                k = data.get("k")
                if not k:
                    continue
                await self._push(
                    {
                        "symbol": str(data.get("s", "")).upper(),
                        "open_time": int(k["t"]),
                        "close_time": int(k["T"]),
                        "open": _num(k["o"]),
                        "high": _num(k["h"]),
                        "low": _num(k["l"]),
                        "close": _num(k["c"]),
                        "volume": _num(k["v"]),
                        "trades": int(k.get("n", 0)),
                        "closed": bool(k.get("x")),
                    }
                )

    async def universe(self) -> list[dict]:
        """Every spot pair on Binance with its 24h turnover, biggest first."""
        info = await self._get_json(f"{BINANCE_REST}/api/v3/exchangeInfo")
        tickers = await self._get_json(f"{BINANCE_REST}/api/v3/ticker/24hr")
        stats = {t["symbol"]: t for t in tickers}  # type: ignore[union-attr]
        out = []
        for item in info["symbols"]:  # type: ignore[index]
            if item.get("status") != "TRADING" or item.get("quoteAsset") != "USDT":
                continue
            tick = stats.get(item["symbol"], {})
            out.append(
                {
                    "symbol": item["symbol"],
                    "base": item.get("baseAsset"),
                    "quote": item.get("quoteAsset"),
                    "price": _num(tick.get("lastPrice")),
                    "change": _num(tick.get("priceChangePercent")),
                    "turnover": _num(tick.get("quoteVolume")),
                }
            )
        return sorted(out, key=lambda x: -x["turnover"])


# --------------------------------------------------------------------- mexc
class MexcFeed(BaseFeed):
    """MEXC spot. Their kline stream sends updates, not "closed" flags, so a
    candle is closed the moment a bar with a newer start time appears for that
    symbol."""

    name = "mexc"
    tf_map = MEXC_TFS
    chunk_size = 80

    def __init__(self, symbols: Iterable[str], **kwargs: object) -> None:
        super().__init__(symbols, **kwargs)
        self._last_candle: dict[str, dict] = {}

    async def history(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        mapped = MEXC_TFS.get(interval, interval)
        if not mapped:
            raise FeedError(f"MEXC has no {interval} interval - aggregate 1m instead")
        params = {"symbol": symbol.upper(), "interval": mapped, "limit": min(limit, 1000)}
        rows = await self._get_json(f"{MEXC_REST}/api/v3/klines", params)
        now_ms = int(time.time() * 1000)
        candles = []
        for r in rows:  # type: ignore[union-attr]
            close_time = int(r[6])
            if close_time >= now_ms:
                continue
            candles.append(
                {
                    "symbol": symbol.upper(),
                    "open_time": int(r[0]),
                    "close_time": close_time,
                    "open": _num(r[1]),
                    "high": _num(r[2]),
                    "low": _num(r[3]),
                    "close": _num(r[4]),
                    "volume": _num(r[5]),
                    "trades": 0,
                    "closed": True,
                }
            )
        if not candles:
            raise FeedError("MEXC returned no closed candles")
        return candles

    async def stream(self, symbols: list[str]) -> None:
        if websockets is None:
            raise FeedError("the 'websockets' package is not installed")
        async with websockets.connect(MEXC_WS, ping_interval=None, close_timeout=5, max_queue=4096) as ws:
            self.connected = True
            self.status = f"live - {len(self._symbols)} symbols on {len(self._chunks())} socket(s)"
            # MEXC spot kline channel: spot@public.kline.v3.api@<SYMBOL>@<INTERVAL>
            params = [f"spot@public.kline.v3.api@{s}@Min1" for s in symbols]
            for i in range(0, len(params), 20):  # MEXC caps subscriptions per frame
                await ws.send(json.dumps({"method": "SUBSCRIPTION", "params": params[i : i + 20]}))
            ping = asyncio.create_task(self._ping(ws))
            try:
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        continue  # protobuf channels are not subscribed here
                    message = json.loads(raw)
                    kline = self._parse(message)
                    if kline is None:
                        continue
                    symbol = kline["symbol"]
                    open_time = kline["open_time"]
                    previous = self._last_candle.get(symbol)
                    if previous is not None and open_time > previous["open_time"]:
                        # a new bar opened: the one we were holding just closed
                        await self._push({**previous, "closed": True})
                    self._last_candle[symbol] = {**kline, "closed": False}
                    await self._push(dict(kline))
            finally:
                ping.cancel()

    @staticmethod
    def _parse(message: dict) -> dict | None:
        """Both MEXC kline encodings: JSON ``d.k`` and ``publicspotkline``."""
        payload = message.get("d") if isinstance(message.get("d"), dict) else None
        kline = (payload or {}).get("k") or message.get("publicspotkline")
        if not kline:
            return None
        symbol = str(message.get("symbol") or kline.get("symbol") or "").upper()
        if not symbol:
            channel = str(message.get("c") or message.get("channel") or "")
            parts = channel.split("@")
            symbol = next((p for p in parts if p.isalnum() and p.isupper() and len(p) > 5), "").upper()
        if not symbol:
            return None
        if "windowstart" in kline:  # protobuf-style field names
            start = int(kline["windowstart"]) * 1000
            return {
                "symbol": symbol,
                "open_time": start,
                "close_time": start + 59_999,
                "open": _num(kline.get("openingprice")),
                "high": _num(kline.get("highestprice")),
                "low": _num(kline.get("lowestprice")),
                "close": _num(kline.get("closingprice")),
                "volume": _num(kline.get("volume")),
                "trades": 0,
                "closed": False,
            }
        raw_time = int(kline.get("t") or kline.get("windowstart") or 0)
        start = raw_time * 1000 if raw_time and raw_time < 10**11 else raw_time
        return {
            "symbol": symbol,
            "open_time": start,
            "close_time": start + 59_999,
            "open": _num(kline.get("o")),
            "high": _num(kline.get("h")),
            "low": _num(kline.get("l")),
            "close": _num(kline.get("c")),
            "volume": _num(kline.get("v")),
            "trades": int(kline.get("n") or 0),
            "closed": False,
        }

    @staticmethod
    async def _ping(ws) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send('{"method":"PING"}')
            except Exception:
                return

    async def universe(self) -> list[dict]:
        info = await self._get_json(f"{MEXC_REST}/api/v3/exchangeInfo")
        tickers = await self._get_json(f"{MEXC_REST}/api/v3/ticker/24hr")
        stats = {t["symbol"]: t for t in tickers}  # type: ignore[union-attr]
        out = []
        for item in info["symbols"]:  # type: ignore[index]
            if item.get("quoteAsset") not in {"USDT", None}:
                continue
            tick = stats.get(item["symbol"])
            if not tick or not _num(tick.get("quoteVolume")):
                continue  # nothing traded: not a market the bot should watch
            out.append(
                {
                    "symbol": item["symbol"],
                    "base": item.get("baseAsset"),
                    "quote": item.get("quoteAsset") or "USDT",
                    "price": _num(tick.get("lastPrice")),
                    "change": _num(tick.get("priceChangePercent")),
                    "turnover": _num(tick.get("quoteVolume")),
                }
            )
        return sorted(out, key=lambda x: -x["turnover"])


# ------------------------------------------------------------------- replay
class ReplayFeed(BaseFeed):
    """Real recorded candle files played back at ``speed`` bars per second.

    A replay used to warm up "by playing", which meant the 1h row needed 120
    hours of 1-minute bars before it could show a single signal - two hours of
    wall clock at 60 bars/s. The feed now starts ``skip_bars`` minutes in and
    serves the rows it skipped as ordinary history, so every timeframe is warm
    in the first second and the replay still only ever sees data from its own
    files.
    """

    source = "replay"
    name = "replay"
    supports_history = True

    def __init__(
        self,
        data_dir: str | Path,
        symbols: list[str] | None = None,
        speed: float = 20.0,
        loop: bool = True,
        skip_bars: int = 0,
        **kwargs: object,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.speed = max(0.1, float(speed))
        self.loop = loop
        self.files = self._discover(symbols)
        if not self.files:
            raise FeedError(f"no CSV files found in {self.data_dir}")
        self.skip_bars = max(0, int(skip_bars))
        # _cursor = rows read from the files so far; _played = how deep the play
        # head is, i.e. how much history the engine is allowed to see. The
        # skipped prefix is history from the first moment, so the higher
        # timeframes are warm before a single bar is paced out.
        self._cursor: dict[str, int] = {symbol: 0 for symbol, _ in self.files}
        self._played: dict[str, int] = {symbol: self.skip_bars for symbol, _ in self.files}
        self._prefix: dict[str, list[dict]] = {}
        super().__init__([symbol for symbol, _ in self.files], **kwargs)
        self.connected = True
        self.status = f"replay {self.speed:g} bars/s" + (f", from +{self.skip_bars} bars" if self.skip_bars else "")

    def _discover(self, symbols: list[str] | None) -> list[tuple[str, Path]]:
        found: list[tuple[str, Path]] = []
        wanted = {s.upper() for s in symbols} if symbols else None
        for path in sorted(self.data_dir.rglob("*.csv")):
            symbol = path.name.split("-")[0].upper()
            if wanted and symbol not in wanted:
                continue
            found.append((symbol, path))
        return found

    def _rows(self, symbol: str, upto: int) -> list[dict]:
        """The first ``upto`` 1m rows of one symbol, read from its own files."""
        cached = self._prefix.get(symbol)
        if cached is not None and len(cached) >= upto:
            return cached[:upto]
        rows: list[dict] = []
        for sym, path in self.files:
            if sym != symbol:
                continue
            with path.open(newline="") as handle:
                for row in csv.reader(handle):
                    if len(row) < 6:
                        continue
                    if len(rows) >= upto:
                        break
                    rows.append(_replay_row(symbol, row))
            if len(rows) >= upto:
                break
        if not self._prefix.get(symbol) or len(rows) >= len(self._prefix.get(symbol, [])):
            self._prefix[symbol] = rows
        return rows[:upto]

    async def history(self, symbol: str, interval: str, limit: int = 500) -> list[dict]:
        """History = the bars this replay has already passed, aggregated.

        Never data from the future: only rows at or before the play head.
        """
        symbol = symbol.upper()
        upto = max(self._played.get(symbol, 0), self.skip_bars)
        if upto <= 0:
            return []
        rows = self._rows(symbol, upto)
        if interval in ("1m", None):
            return rows[-limit:]
        if interval not in TIMEFRAMES:
            return []
        rollup = Rollup(interval)
        bars: list[dict] = []
        for row in rows:
            for done in rollup.add(row):
                bars.append(done)
        return bars[-limit:]

    def exchange_interval(self, timeframe: str) -> str | None:
        # a replay can build any timeframe from its 1m rows
        return timeframe if timeframe in TIMEFRAMES else None

    async def universe(self) -> list[dict]:
        raise FeedError(
            "replay mode has no exchange symbol list - run `python run.py serve --exchange mexc` "
            "and open this panel again"
        )

    async def candles(self) -> AsyncIterator[dict]:  # type: ignore[override]
        while True:
            for symbol, path in self.files:
                with path.open(newline="") as handle:
                    for row in csv.reader(handle):
                        if len(row) < 6:
                            continue
                        self._cursor[symbol] = self._cursor.get(symbol, 0) + 1
                        if self._cursor[symbol] <= self.skip_bars:
                            continue  # the warm-up prefix is history, not a tick
                        self._played[symbol] = self._cursor[symbol]
                        candle = _replay_row(symbol, row)
                        yield candle
                        await asyncio.sleep(1.0 / self.speed)
            if not self.loop:
                return


def _replay_row(symbol: str, row: list[str]) -> dict:
    """One CSV row -> one closed 1m candle (the layout the engine expects)."""
    raw_time = int(row[0])
    open_time = raw_time // 1000 if raw_time > 10**14 else raw_time
    if open_time < 10**12:  # seconds
        open_time *= 1000
    return {
        "symbol": symbol,
        "open_time": open_time,
        "close_time": open_time + 59_999,
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "trades": 0,
        "closed": True,
    }


FEEDS = {"binance": BinanceFeed, "mexc": MexcFeed}


def make_feed(exchange: str, symbols: Iterable[str], **kwargs: object) -> BaseFeed:
    key = (exchange or "binance").lower()
    if key not in FEEDS:
        raise FeedError(f"unknown exchange {exchange!r} (choose from {', '.join(FEEDS)})")
    return FEEDS[key](symbols, **kwargs)
