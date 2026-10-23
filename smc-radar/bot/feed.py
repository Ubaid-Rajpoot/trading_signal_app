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
from typing import AsyncIterator, Iterable, Iterator

import httpx

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
# MEXC has moved its websocket host around; both are tried, and a host that
# accepts the connection but never sends a kline is abandoned for the next one.
MEXC_WS_HOSTS = ["wss://wbs.mexc.com/ws", "wss://wbs-api.mexc.com/ws"]
MEXC_WS = MEXC_WS_HOSTS[0]
MEXC_TFS = {"1m": "1m", "3m": None, "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m"}
MEXC_WS_INTERVAL = {"1m": "Min1", "5m": "Min5", "15m": "Min15", "30m": "Min30", "1h": "Min60"}

# ---------------------------------------------------------------- MEXC futures
# A different host, a different namespace and a different naming convention from
# spot: futures contracts are "BTC_USDT" and every endpoint lives under
# contract.mexc.com. The app therefore treats the underscore as the market tag.
MEXC_CONTRACT_REST = "https://contract.mexc.com"
MEXC_CONTRACT_WS_HOSTS = ["wss://contract.mexc.com/edge"]
MEXC_FUTURE_TFS = {"1m": "Min1", "3m": None, "5m": "Min5", "15m": "Min15", "30m": "Min30", "1h": "Min60"}
FUTURE_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


def is_futures(symbol: str) -> bool:
    """``BTC_USDT`` is a perpetual contract, ``BTCUSDT`` is the spot pair."""
    return "_" in str(symbol)


def market_of(symbol: str) -> str:
    return "futures" if is_futures(symbol) else "spot"


def to_spot(symbol: str) -> str:
    return str(symbol).replace("_", "").upper()


def to_futures(symbol: str) -> str:
    """``BTCUSDT`` -> ``BTC_USDT``; anything already tagged is left alone."""
    raw = str(symbol).upper()
    if "_" in raw:
        return raw
    for quote in ("USDT", "USDC", "BTC", "ETH"):
        if raw.endswith(quote) and len(raw) > len(quote):
            return f"{raw[:-len(quote)]}_{quote}"
    return raw


def pretty_symbol(symbol: str) -> str:
    raw = str(symbol).upper()
    return raw.split("_")[0] + "/" + raw.split("_")[1] if "_" in raw else (
        raw[:-4] + "/USDT" if raw.endswith("USDT") else raw
    )


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
        self.messages = 0        # raw frames off the socket
        self.parsed = 0          # frames that turned into a candle
        self.ignored = 0         # acks / errors / channels we do not use
        self.bytes_seen = 0
        self.samples: list[str] = []   # the last few raw frames, for diagnostics
        self.subscribed: list[str] = []
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

    def force_reconnect(self) -> None:
        """Drop the sockets and rebuild them (used when the stream goes silent)."""
        self._generation += 1
        self.connected = False
        self.status = "reconnecting (stalled stream)"

    def exchange_interval(self, timeframe: str, symbol: str | None = None) -> str | None:
        """The exchange's own name for this timeframe (``None`` = aggregate 1m).

        ``symbol`` is optional: one market can have several feeds behind it, and
        then only the feed that owns that symbol can answer.
        """
        return self.tf_map.get(timeframe, timeframe)

    # ------------------------------------------------------------- history
    async def history(
        self, symbol: str, interval: str, limit: int = 500, include_open: bool = False
    ) -> list[dict]:
        """Closed candles, or also the one still forming when ``include_open``."""
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

    def sample(self, raw) -> None:
        """Remember what the exchange actually sent, so a silent feed is provable."""
        self.messages += 1
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        self.bytes_seen += len(text)
        if len(self.samples) >= 6:
            self.samples.pop(0)
        self.samples.append(text[:300])

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

    async def history(self, symbol: str, interval: str, limit: int = 500, include_open: bool = False) -> list[dict]:
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
                        "closed": int(r[6]) < now_ms or not include_open,
                    }
                    for r in rows  # type: ignore[union-attr]
                    if include_open or int(r[6]) < now_ms
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

    async def history(
        self, symbol: str, interval: str, limit: int = 500, include_open: bool = False
    ) -> list[dict]:
        mapped = MEXC_TFS.get(interval, interval)
        if not mapped:
            raise FeedError(f"MEXC has no {interval} interval - aggregate 1m instead")
        params = {"symbol": symbol.upper(), "interval": mapped, "limit": min(limit, 1000)}
        rows = await self._get_json(f"{MEXC_REST}/api/v3/klines", params)
        now_ms = int(time.time() * 1000)
        candles = []
        for r in rows:  # type: ignore[union-attr]
            close_time = int(r[6])
            if close_time >= now_ms and not include_open:
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
                    "closed": close_time < now_ms,
                }
            )
        if not candles:
            raise FeedError("MEXC returned no closed candles")
        return candles

    async def stream(self, symbols: list[str]) -> None:
        if websockets is None:
            raise FeedError("the 'websockets' package is not installed")
        last_error: Exception | None = None
        for host in MEXC_WS_HOSTS:
            try:
                await self._stream_host(host, symbols)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                self.connected = False
                self.status = f"{host} failed: {type(exc).__name__}: {str(exc)[:70]}"
                await asyncio.sleep(0.5)
        raise FeedError(f"no MEXC websocket host delivered: {last_error}")

    async def _stream_host(self, host: str, symbols: list[str]) -> None:
        async with websockets.connect(host, ping_interval=None, close_timeout=5, max_queue=4096) as ws:
            self.connected = True
            # MEXC spot kline channel: spot@public.kline.v3.api@<SYMBOL>@<INTERVAL>
            params = [f"spot@public.kline.v3.api@{s}@Min1" for s in symbols]
            self.subscribed = list(params)
            for i in range(0, len(params), 20):  # MEXC caps subscriptions per frame
                await ws.send(json.dumps({"method": "SUBSCRIPTION", "params": params[i : i + 20]}))
            self.status = f"live - {len(symbols)} symbols on {host.split('//')[1].split('/')[0]}"
            ping = asyncio.create_task(self._ping(ws))
            first = asyncio.create_task(self._first_candle(ws))
            try:
                # a socket that connects but stays silent is not "live": if the
                # first kline does not arrive in 20s, try the next host
                await asyncio.wait_for(asyncio.shield(first), timeout=20)
            except asyncio.TimeoutError:
                first.cancel()
                ping.cancel()
                self.connected = False
                self.status = f"{host} connected but sent no kline in 20s"
                raise FeedError(self.status)
            except Exception:
                pass
            finally:
                if not first.done():
                    first.cancel()
            try:
                async for raw in ws:
                    self.sample(raw)
                    if isinstance(raw, (bytes, bytearray)) and not raw.lstrip().startswith(b"{"):
                        self.ignored += 1
                        continue  # a protobuf channel we did not subscribe to
                    try:
                        message = json.loads(raw)
                    except Exception:
                        self.ignored += 1
                        continue
                    kline = self._parse(message)
                    if kline is None:
                        self.ignored += 1
                        continue
                    self.parsed += 1
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

    async def _first_candle(self, ws) -> None:
        """Wait for the first parseable kline (used as a liveness gate)."""
        while True:
            raw = await ws.recv()
            self.sample(raw)
            try:
                message = json.loads(raw)
            except Exception:
                continue
            kline = self._parse(message)
            if kline is not None:
                self.parsed += 1
                self._last_candle[kline["symbol"]] = {**kline, "closed": False}
                await self._push(dict(kline))
                return

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


# ------------------------------------------------------------------- futures
class MexcFuturesFeed(BaseFeed):
    """MEXC **perpetual futures** (USDT-margined) - the contract API.

    Documented shapes this class speaks (checked against the exchange's own API
    docs, and pinned by tests with recorded frames):

    * REST  ``GET https://contract.mexc.com/api/v1/contract/kline/BTC_USDT``
      ``?interval=Min1&start=<sec>&end=<sec>`` ->
      ``{"success": true, "data": {"time": [...], "open": [...], "close": [...],
      "high": [...], "low": [...], "vol": [...]}}`` (seconds, max 2000 points)
    * WS    ``wss://contract.mexc.com/edge``, subscribe one contract per frame:
      ``{"method": "sub.kline", "param": {"symbol": "BTC_USDT", "interval": "Min1"}}``
      and pushes ``{"channel": "push.kline", "data": {"symbol": "BTC_USDT",
      "t": <sec>, "o": .., "c": .., "h": .., "l": .., "q": volume}}``
    * universe ``GET /api/v1/contract/detail`` (+ ``/api/v1/contract/ticker``)

    Futures candles are their own data, not spot candles relabelled: they come
    from the contract endpoint and carry a funding-driven basis of their own.
    """

    name = "mexc_futures"
    market = "futures"
    tf_map = MEXC_FUTURE_TFS
    chunk_size = 40

    def __init__(self, symbols: Iterable[str], **kwargs: object) -> None:
        canonical = []
        for raw in symbols:
            sym = to_futures(raw)
            if sym and is_futures(sym):
                canonical.append(sym)
        super().__init__(canonical, **kwargs)
        self._last_candle: dict[str, dict] = {}

    # ------------------------------------------------------------------ rest
    async def history(
        self, symbol: str, interval: str, limit: int = 500, include_open: bool = False
    ) -> list[dict]:
        symbol = to_futures(symbol)
        mapped = MEXC_FUTURE_TFS.get(interval, interval)
        if not mapped:
            raise FeedError(f"MEXC futures has no {interval} interval - aggregate 1m instead")
        seconds = FUTURE_SECONDS.get(interval, 60)
        now = int(time.time())
        start = now - min(max(1, int(limit)), 2000) * seconds - seconds
        params = {"interval": mapped, "start": start, "end": now}
        payload = await self._get_json(f"{MEXC_CONTRACT_REST}/api/v1/contract/kline/{symbol}", params)
        block = (payload or {}).get("data") if isinstance(payload, dict) else None
        if not isinstance(block, dict) or not block.get("time"):
            raise FeedError(f"MEXC futures returned no kline for {symbol}")
        times = block["time"]
        now_ms = int(time.time() * 1000)
        candles: list[dict] = []
        for i, raw_time in enumerate(times):
            open_time = int(raw_time) * 1000
            close_time = open_time + seconds * 1000 - 1
            try:
                candles.append(
                    {
                        "symbol": symbol,
                        "open_time": open_time,
                        "close_time": close_time,
                        "open": _num(block["open"][i]),
                        "high": _num(block["high"][i]),
                        "low": _num(block["low"][i]),
                        "close": _num(block["close"][i]),
                        "volume": _num(block.get("vol", [0] * len(times))[i]),
                        "trades": 0,
                        "closed": close_time < now_ms,
                    }
                )
            except (IndexError, KeyError):
                continue
        candles = [c for c in candles if include_open or c["closed"]]
        if not candles:
            raise FeedError("MEXC futures returned no closed candles")
        return sorted(candles, key=lambda c: c["open_time"])[-limit:]

    # ---------------------------------------------------------------- stream
    async def stream(self, symbols: list[str]) -> None:
        if websockets is None:
            raise FeedError("the 'websockets' package is not installed")
        last_error: Exception | None = None
        for host in MEXC_CONTRACT_WS_HOSTS:
            try:
                await self._stream_host(host, [to_futures(s) for s in symbols])
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                self.connected = False
                self.status = f"{host} failed: {type(exc).__name__}: {str(exc)[:70]}"
                await asyncio.sleep(0.5)
        raise FeedError(f"no MEXC futures websocket host delivered: {last_error}")

    async def _stream_host(self, host: str, symbols: list[str]) -> None:
        async with websockets.connect(host, ping_interval=None, close_timeout=5, max_queue=4096) as ws:
            self.connected = True
            self.subscribed = [f"sub.kline {s} Min1" for s in symbols]
            for symbol in symbols:
                await ws.send(json.dumps({"method": "sub.kline", "param": {"symbol": symbol, "interval": "Min1"}}))
            self.status = f"futures live - {len(symbols)} contracts on {host.split('//')[1].split('/')[0]}"
            ping = asyncio.create_task(self._ping(ws))
            # a socket that connects and says nothing is not live: wait for one
            first = asyncio.create_task(self._first_candle(ws))
            try:
                await asyncio.wait_for(asyncio.shield(first), timeout=20)
            except asyncio.TimeoutError:
                ping.cancel()
                self.connected = False
                self.status = f"{host} connected but sent no futures kline in 20s"
                raise FeedError(self.status)
            except Exception:
                pass
            finally:
                if not first.done():
                    first.cancel()
            try:
                async for raw in ws:
                    self.sample(raw)
                    try:
                        message = json.loads(raw)
                    except Exception:
                        self.ignored += 1
                        continue
                    if message.get("channel") == "push.pong" or message.get("channel") == "rs.sub.kline":
                        self.ignored += 1
                        continue
                    kline = self._parse(message)
                    if kline is None:
                        self.ignored += 1
                        continue
                    self.parsed += 1
                    symbol = kline["symbol"]
                    open_time = kline["open_time"]
                    previous = self._last_candle.get(symbol)
                    if previous is not None and open_time > previous["open_time"]:
                        await self._push({**previous, "closed": True})
                    self._last_candle[symbol] = {**kline, "closed": False}
                    await self._push(dict(kline))
            finally:
                ping.cancel()

    async def _first_candle(self, ws) -> None:
        while True:
            raw = await ws.recv()
            self.sample(raw)
            try:
                message = json.loads(raw)
            except Exception:
                continue
            kline = self._parse(message)
            if kline is not None:
                self.parsed += 1
                self._last_candle[kline["symbol"]] = {**kline, "closed": False}
                await self._push(dict(kline))
                return

    @staticmethod
    def _parse(message: dict) -> dict | None:
        """``{"channel": "push.kline", "data": {...}}`` -> a 1m candle.

        The futures kline payload carries ``t`` (window start, seconds), the
        prices ``o/h/l/c``, and the contract volume ``q``. It is not the spot
        format, which is why a separate parser exists instead of a flag.
        """
        data = message.get("data")
        if not isinstance(data, dict) or message.get("channel") != "push.kline":
            return None
        symbol = str(data.get("symbol") or message.get("symbol") or "").upper()
        if not symbol:
            return None
        raw_time = int(_num(data.get("t"), 0))
        if not raw_time:
            return None
        start = raw_time * 1000 if raw_time < 10**11 else raw_time
        return {
            "symbol": to_futures(symbol),
            "open_time": start,
            "close_time": start + 59_999,
            "open": _num(data.get("o")),
            "high": _num(data.get("h")),
            "low": _num(data.get("l")),
            "close": _num(data.get("c")),
            "volume": _num(data.get("q") or data.get("v")),
            "trades": 0,
            "closed": False,
        }

    @staticmethod
    async def _ping(ws) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send('{"method":"ping"}')
            except Exception:
                return

    async def universe(self) -> list[dict]:
        detail = await self._get_json(f"{MEXC_CONTRACT_REST}/api/v1/contract/detail")
        try:
            tickers = await self._get_json(f"{MEXC_CONTRACT_REST}/api/v1/contract/ticker")
        except Exception:
            tickers = {"data": []}
        stats = {
            t.get("symbol"): t
            for t in (tickers.get("data") or [])
            if isinstance(t, dict) and t.get("symbol")
        }
        out = []
        for item in (detail.get("data") or []):
            if not isinstance(item, dict):
                continue
            symbol = to_futures(str(item.get("symbol") or ""))
            if not symbol or not item.get("quoteCoin") == "USDT":
                continue
            tick = stats.get(item.get("symbol")) or stats.get(symbol) or {}
            out.append(
                {
                    "symbol": symbol,
                    "market": "futures",
                    "base": item.get("baseCoin"),
                    "quote": item.get("quoteCoin") or "USDT",
                    "price": _num(tick.get("lastPrice")),
                    "change": _num(tick.get("riseFallRate")) * 100,
                    "turnover": _num(tick.get("amount24")) or _num(tick.get("volume24")),
                    "max_leverage": item.get("maxLeverage"),
                }
            )
        return sorted(out, key=lambda x: -x["turnover"])


# ----------------------------------------------------------------- composite
class CompositeFeed(BaseFeed):
    """One feed in front of two markets: spot pairs and futures contracts.

    The engine and the page only ever talk to this object. It routes every call
    by the symbol's market tag (``BTCUSDT`` -> spot, ``BTC_USDT`` -> futures) and
    merges both candle streams into the one queue the engine already reads, so a
    futures contract is real futures data - not a spot candle wearing a label.
    """

    name = "mexc+spot+futures"
    chunk_size = 100

    def __init__(self, feeds: dict[str, BaseFeed], **_: object) -> None:
        symbols: list[str] = []
        for feed in feeds.values():
            symbols += list(feed.symbols)
        super().__init__(symbols)
        self.feeds = feeds
        self._reader: asyncio.Task | None = None

    def market_of(self, symbol: str) -> str:
        return market_of(symbol)

    def _feed_for(self, symbol: str) -> BaseFeed | None:
        return self.feeds.get(market_of(symbol))

    # ------------------------------------------------------------- symbols
    def add_symbols(self, symbols: Iterable[str]) -> list[str]:
        added: list[str] = []
        for raw in symbols:
            symbol = str(raw).upper()
            feed = self._feed_for(symbol)
            if feed is None:
                continue
            added += feed.add_symbols([symbol])
        for symbol in added:
            if symbol not in self._symbols:
                self._symbols.append(symbol)
        if added:
            self._generation += 1
        return sorted(set(added))

    def remove_symbols(self, symbols: Iterable[str]) -> list[str]:
        drop = {str(s).upper() for s in symbols}
        removed: list[str] = []
        for feed in self.feeds.values():
            removed += feed.remove_symbols(drop)
        self._symbols = [s for s in self._symbols if s not in drop]
        if removed:
            self._generation += 1
        return sorted(set(removed))

    def exchange_interval(self, timeframe: str, symbol: str | None = None) -> str | None:
        feed = self._feed_for(symbol) if symbol else None
        if feed is None:
            return "1m" if timeframe == "1m" else None
        return feed.exchange_interval(timeframe, symbol)

    def force_reconnect(self) -> None:
        for feed in self.feeds.values():
            feed.force_reconnect()
        self.connected = False

    # ------------------------------------------------------------- plumbing
    @property
    def status(self) -> str:  # type: ignore[override]
        parts = [f"{key}: {feed.status}" for key, feed in self.feeds.items()]
        return " | ".join(parts)

    @status.setter
    def status(self, value: str) -> None:
        self._status = value

    def stats(self) -> dict:
        return {
            key: {
                "connected": feed.connected,
                "status": feed.status,
                "messages": feed.messages,
                "parsed": feed.parsed,
                "symbols": len(feed.symbols),
            }
            for key, feed in self.feeds.items()
        }

    async def history(
        self, symbol: str, interval: str, limit: int = 500, include_open: bool = False
    ) -> list[dict]:
        feed = self._feed_for(symbol)
        if feed is None:
            raise FeedError(f"no feed for {symbol}")
        mapped = feed.exchange_interval(interval, symbol) if interval in getattr(feed, "tf_map", {}) else interval
        return await feed.history(symbol, mapped or interval, limit, include_open)

    def _rows(self, path: Path) -> Iterator[dict]:
        """Every recorded candle of one file, as a plain generator."""
        with path.open(newline="") as handle:
            for index, row in enumerate(csv.reader(handle)):
                if len(row) < 6 or index < self.skip:
                    continue
                raw_time = int(row[0])
                open_time = raw_time // 1000 if raw_time > 10**14 else raw_time
                if open_time < 10**12:  # seconds
                    open_time *= 1000
                yield {
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

    async def candles(self) -> AsyncIterator[dict]:
        """Both markets at once: the files are merged on time, not played in turn.

        Playing one file after the other would leave one market frozen while the
        other streamed - which is not what two feeds look like. Each pass keeps
        one cursor per file and always emits the oldest candle next, so spot and
        futures advance together, exactly as they do live.
        """
        while True:
            heads: list[list] = [[self._rows(path), None] for _, path in self.files]
            for head in heads:
                head[1] = next(head[0], None)
            while any(head[1] is not None for head in heads):
                at = min(
                    (i for i, h in enumerate(heads) if h[1] is not None),
                    key=lambda i: (heads[i][1]["open_time"], i),
                )
                head = heads[at]
                base = head[1]
                head[1] = next(head[0], None)
                symbol = self.files[at][0]
                bar = {**base, "symbol": symbol}
                if self.ticks > 1:
                    for k in range(1, self.ticks):
                        p = k / self.ticks
                        o = bar["open"]
                        close_p = o + (bar["close"] - o) * p
                        high_p = max(o, close_p, o + (bar["high"] - o) * p)
                        low_p = min(o, close_p, o + (bar["low"] - o) * p)
                        yield {
                            **bar,
                            "high": high_p,
                            "low": low_p,
                            "close": close_p,
                            "volume": bar["volume"] * p,
                            "closed": False,
                        }
                        await asyncio.sleep(1.0 / (self.speed * self.ticks))
                yield bar
                await asyncio.sleep(1.0 / self.speed)
            if not self.loop:
                return


# ------------------------------------------------------------------- replay
class ReplayFeed(BaseFeed):
    """Real recorded candle files played back at ``speed`` bars per second."""

    source = "replay"
    name = "replay"
    supports_history = False

    def __init__(
        self,
        data_dir: str | Path,
        symbols: list[str] | None = None,
        speed: float = 20.0,
        loop: bool = True,
        skip: int = 0,
        ticks: int = 1,
        **kwargs: object,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.speed = max(0.1, float(speed))
        self.loop = loop
        self.skip = max(0, int(skip))  # bars already handed over as a pre-roll
        # how many partial updates each recorded candle is shown as. The prices
        # are interpolated between that candle's own open/high/low/close - a
        # replay has no ticks, and inventing any is not allowed.
        self.ticks = max(1, int(ticks))
        self.files = self._discover(symbols)
        if not self.files:
            raise FeedError(f"no CSV files found in {self.data_dir}")
        super().__init__([symbol for symbol, _ in self.files], **kwargs)
        self.connected = True
        self.status = f"replay {self.speed:g} bars/s"

    def _discover(self, symbols: list[str] | None) -> list[tuple[str, Path]]:
        found: list[tuple[str, Path]] = []
        wanted = {s.upper() for s in symbols} if symbols else None
        for path in sorted(self.data_dir.rglob("*.csv")):
            symbol = path.name.split("-")[0].upper()
            if wanted and symbol not in wanted:
                continue
            found.append((symbol, path))
        return found

    async def history(
        self, symbol: str, interval: str, limit: int = 500, include_open: bool = False
    ) -> list[dict]:
        return []  # the replay warms up by playing

    def file_for(self, symbol: str) -> Path | None:
        return next((path for sym, path in self.files if sym == symbol.upper()), None)

    def first_bars(self, symbol: str, limit: int) -> list[dict]:
        """The first ``limit`` closed 1m bars, for a pre-roll before streaming."""
        path = self.file_for(symbol)
        if path is None or limit <= 0:
            return []
        rows: list[dict] = []
        with path.open(newline="") as handle:
            for row in csv.reader(handle):
                if len(row) < 6:
                    continue
                raw_time = int(row[0])
                open_time = raw_time // 1000 if raw_time > 10**14 else raw_time
                if open_time < 10**12:
                    open_time *= 1000
                rows.append(
                    {
                        "symbol": symbol.upper(),
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
                )
                if len(rows) >= limit:
                    break
        return rows

    async def universe(self) -> list[dict]:
        raise FeedError(
            "replay mode has no exchange symbol list - run `python run.py serve --exchange mexc` "
            "and open this panel again"
        )

    def _rows(self, path: Path) -> Iterator[dict]:
        """Every recorded candle of one file, as a plain generator."""
        with path.open(newline="") as handle:
            for index, row in enumerate(csv.reader(handle)):
                if len(row) < 6 or index < self.skip:
                    continue
                raw_time = int(row[0])
                open_time = raw_time // 1000 if raw_time > 10**14 else raw_time
                if open_time < 10**12:  # seconds
                    open_time *= 1000
                yield {
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

    async def candles(self) -> AsyncIterator[dict]:
        """Both markets at once: the files are merged on time, not played in turn.

        Playing one file after the other left one market frozen while the other
        streamed, which is not what two live feeds look like. Each pass keeps one
        cursor per file and always emits the oldest candle next, so spot and
        futures advance together (``tools/ui/quality.mjs`` asserts both sections
        stay alive on the same page).
        """
        while True:
            heads: list[list] = [[self._rows(path), None] for _, path in self.files]
            for head in heads:
                head[1] = next(head[0], None)
            while any(head[1] is not None for head in heads):
                at = min(
                    (i for i, h in enumerate(heads) if h[1] is not None),
                    key=lambda i: (heads[i][1]["open_time"], i),
                )
                head = heads[at]
                base = head[1]
                head[1] = next(head[0], None)
                bar = {**base, "symbol": self.files[at][0]}
                if self.ticks > 1:
                    for k in range(1, self.ticks):
                        p = k / self.ticks
                        o = bar["open"]
                        close_p = o + (bar["close"] - o) * p
                        high_p = max(o, close_p, o + (bar["high"] - o) * p)
                        low_p = min(o, close_p, o + (bar["low"] - o) * p)
                        yield {
                            **bar,
                            "high": high_p,
                            "low": low_p,
                            "close": close_p,
                            "volume": bar["volume"] * p,
                            "closed": False,
                        }
                        await asyncio.sleep(1.0 / (self.speed * self.ticks))
                yield bar
                await asyncio.sleep(1.0 / self.speed)
            if not self.loop:
                return


FEEDS = {"binance": BinanceFeed, "mexc": MexcFeed, "mexc_futures": MexcFuturesFeed}


def make_market_feed(
    exchange: str,
    spot_symbols: Iterable[str],
    futures_symbols: Iterable[str] | None = None,
    **kwargs: object,
) -> BaseFeed:
    """The feed the app actually runs: spot and futures side by side.

    With no futures symbols this returns the plain exchange feed, so every
    existing entry point (and every test) keeps working unchanged.
    """
    key = (exchange or "binance").lower()
    futures_symbols = [s for s in (futures_symbols or []) if str(s).strip()]
    if key == "mexc" and futures_symbols:
        return CompositeFeed(
            {
                "spot": MexcFeed(spot_symbols, **kwargs),
                "futures": MexcFuturesFeed(futures_symbols, **kwargs),
            }
        )
    return make_feed(key, list(spot_symbols) + [to_futures(s) for s in futures_symbols], **kwargs)


def make_feed(exchange: str, symbols: Iterable[str], **kwargs: object) -> BaseFeed:
    key = (exchange or "binance").lower()
    if key not in FEEDS:
        raise FeedError(f"unknown exchange {exchange!r} (choose from {', '.join(FEEDS)})")
    return FEEDS[key](symbols, **kwargs)
