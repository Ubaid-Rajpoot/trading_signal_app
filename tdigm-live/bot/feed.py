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
    market = "spot"
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
        # One pooled client for thousands of concurrent history pages. Opening a
        # new TLS connection per request made a full futures-universe warm-up far
        # slower and more likely to exhaust sockets.
        self._http = httpx.AsyncClient(
            timeout=20,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=40),
        )
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
        """Return the engine's canonical interval when this feed supports it.

        ``history()`` owns exchange-specific translation (``1h`` -> ``Min60``).
        Returning ``Min60`` here and then passing it back into ``history()`` made
        MEXC map the value a second time to ``None``—so every futures warm-up
        failed even though direct history calls worked. ``None`` only means the
        engine must aggregate a lower interval.
        """
        if timeframe in self.tf_map:
            return timeframe if self.tf_map[timeframe] is not None else None
        return timeframe

    # ------------------------------------------------------------- history
    async def history(self, symbol: str, interval: str, limit: int = 500,
                      end_time: int | None = None) -> list[dict]:
        """Closed candles, oldest first.

        ``end_time`` (ms) asks for candles that closed at or before it, which is
        what makes pagination possible: the narrative engine needs thousands of
        1m bars to warm its liquidity map, and the exchange only serves 1000 per
        request. Feeds that cannot page simply ignore it.
        """
        raise NotImplementedError

    async def _get_json(self, url: str, params: dict | None = None, retries: int = 4) -> object:
        delay = 1.0
        for attempt in range(retries):
            try:
                resp = await self._http.get(url, params=params)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429 and attempt < retries - 1:
                    await asyncio.sleep(delay + random.uniform(0, 0.5))
                    delay *= 2.0
                    continue
                if resp.status_code >= 500 and attempt < retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2.0
                    continue
                raise FeedError(f"{url} -> HTTP {resp.status_code} {resp.text[:120]}")
            except (httpx.RequestError, asyncio.TimeoutError) as exc:
                if attempt == retries - 1:
                    raise FeedError(f"{url} -> {type(exc).__name__}: {exc}")
                await asyncio.sleep(delay)
                delay *= 2.0
        raise FeedError(f"{url} -> max retries exceeded")

    async def close(self) -> None:
        """Release pooled REST connections on application shutdown."""
        await self._http.aclose()

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

    async def history(self, symbol: str, interval: str, limit: int = 500,
                      end_time: int | None = None) -> list[dict]:
        symbol = symbol.upper()
        remaining = max(1, int(limit))
        out: list[dict] = []
        end = int(end_time) if end_time else None
        now_ms = int(time.time() * 1000)
        while remaining > 0:
            params: dict[str, object] = {"symbol": symbol, "interval": interval,
                                         "limit": min(remaining, 1000)}
            if end:
                params["endTime"] = end
            last: Exception | None = None
            rows = None
            for host in BINANCE_HOSTS:
                try:
                    rows = await self._get_json(f"{host}/api/v3/klines", params)
                    break
                except Exception as exc:
                    last = exc
            if rows is None:
                if out:
                    break
                raise FeedError(f"binance history {symbol} {interval}: {last}")
            candles = [
                {
                    "symbol": symbol,
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
            if not candles:
                break
            out = candles + out
            remaining -= len(candles)
            if len(rows) < min(remaining + len(candles), 1000):  # type: ignore[arg-type]
                break
            end = int(rows[0][0]) - 1  # type: ignore[index]
        if not out:
            raise FeedError(f"binance history {symbol} {interval}: no closed candles")
        return out

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



# ------------------------------------------------------------------ futures
# The trader trades the perpetual contract, so the price that matters is the
# contract's, not spot. The two differ (basis, funding, different wicks), and a
# swing high on spot is not the level the contract swept.

MEXC_FUTURES_REST = "https://contract.mexc.com"
MEXC_FUTURES_WS = "wss://contract.mexc.com/edge"
MEXC_FUTURES_TFS = {
    "1m": "Min1",
    "3m": None,          # build from 1m
    "5m": "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h": "Min60",
    "2h": None,          # build from 1m
    "4h": "Hour4",
    "1d": "Day1",
}


def contract_symbol(symbol: str) -> str:
    """``BTCUSDT`` -> ``BTC_USDT`` (MEXC's contract naming)."""
    symbol = str(symbol).upper().replace("_", "")
    for quote in ("USDT", "USDC", "USD"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}_{quote}"
    return symbol


def plain_symbol(symbol: str) -> str:
    """``BTC_USDT`` -> ``BTCUSDT`` (what the rest of the bot speaks)."""
    return str(symbol).upper().replace("_", "")


def _kline(
    symbol: str,
    open_time: int,
    o,
    h,
    l,
    c,
    v,
    trades: int = 0,
    closed: bool = True,
    duration_ms: int = 60_000,
) -> dict:
    """Normalise one contract candle without lying about when it closed.

    The old helper always added 59,999 ms, including to 4h and daily history.
    Indicator values were unaffected, but signal timestamps (and therefore
    archive keys) were hours early.  Live contract updates are 1m and keep the
    default; REST callers pass the interval's real duration.
    """
    duration = max(1, int(duration_ms))
    return {
        "symbol": symbol,
        "open_time": int(open_time),
        "close_time": int(open_time) + duration - 1,
        "open": _num(o),
        "high": _num(h),
        "low": _num(l),
        "close": _num(c),
        "volume": _num(v),
        "trades": int(trades or 0),
        "closed": closed,
    }


class MexcFuturesFeed(BaseFeed):
    """MEXC USDT-M perpetuals (contract.mexc.com), streamed as 1m klines.

    ``market = "futures"``. The exchange names contracts ``BTC_USDT``; everything
    above this class uses ``BTCUSDT``, so the mapping happens here and only here.
    """

    name = "mexc"
    market = "futures"
    tf_map = MEXC_FUTURES_TFS
    # MEXC limits subscriptions per connection.  Small shards also contain a
    # reconnect to one part of the universe instead of taking every contract
    # offline together.
    chunk_size = 20
    history_page_size = 2000  # documented maximum for contract klines

    def __init__(self, symbols: Iterable[str], **kwargs: object) -> None:
        super().__init__(symbols, **kwargs)
        self._last_candle: dict[str, dict] = {}

    async def history(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        end_time: int | None = None,
    ) -> list[dict]:
        """Return the requested depth, paging MEXC's 2,000-bar windows.

        Previously ``limit=2500`` only made one request.  MEXC caps that response
        at 2,000 rows, so the scanner claimed to scan a long history while
        silently receiving less.  Windows now walk backwards, are de-duplicated,
        and retain only closed bars.
        """
        mapped = MEXC_FUTURES_TFS.get(interval, None)
        if not mapped:
            raise FeedError(f"MEXC futures has no {interval} interval - aggregate a lower interval instead")
        contract = contract_symbol(symbol)
        symbol_plain = plain_symbol(contract)
        step_ms = int(TIMEFRAMES.get(interval, 60_000))
        step_s = max(1, step_ms // 1000)
        wanted = max(1, min(int(limit), 50_000))
        now_ms = int(time.time() * 1000)
        cursor_s = min(int(end_time // 1000) if end_time else now_ms // 1000, now_ms // 1000)
        found: dict[int, dict] = {}
        previous_earliest: int | None = None

        while len(found) < wanted:
            page_size = min(self.history_page_size, wanted - len(found))
            # A two-bar overlap protects the boundary from inclusive/exclusive
            # differences in the exchange's start/end interpretation.
            start_s = cursor_s - step_s * (page_size + 2)
            rows = await self._get_json(
                f"{MEXC_FUTURES_REST}/api/v1/contract/kline/{contract}",
                {"interval": mapped, "start": start_s, "end": cursor_s},
            )
            data = (rows or {}).get("data") or {}  # type: ignore[union-attr]
            times = data.get("time") or []
            columns = [data.get(name) or [] for name in ("open", "high", "low", "close", "vol")]
            count = min([len(times), *(len(column) for column in columns)]) if times else 0
            if count <= 0:
                break

            page_times: list[int] = []
            for i in range(count):
                open_time = int(times[i]) * 1000
                page_times.append(open_time)
                if open_time + step_ms > now_ms or open_time // 1000 > cursor_s:
                    continue  # still forming or in an overlapping newer window
                found[open_time] = _kline(
                    symbol_plain,
                    open_time,
                    columns[0][i],
                    columns[1][i],
                    columns[2][i],
                    columns[3][i],
                    columns[4][i],
                    duration_ms=step_ms,
                )

            earliest = min(page_times)
            if previous_earliest is not None and earliest >= previous_earliest:
                break  # a defensive stop for an exchange ignoring start/end
            previous_earliest = earliest
            cursor_s = earliest // 1000 - 1
            if count < page_size:
                break  # this contract has no older history

        candles = [found[key] for key in sorted(found)]
        if not candles:
            raise FeedError(f"MEXC futures returned no closed {interval} candles for {contract}")
        return candles[-wanted:]

    async def universe(self) -> list[dict]:
        """Return every enabled USDT perpetual; ticker data only ranks it.

        Contract detail is the authoritative listing.  The previous implementation
        intersected it with the ticker response and discarded zero/missing
        ``amount24`` rows.  A partial ticker response could therefore turn 700+
        enabled contracts into a plausible-looking list of 45 without raising an
        error.  Missing ticker rows now sort last instead of disappearing.
        """

        def rows(payload: object, endpoint: str) -> list[dict]:
            if not isinstance(payload, dict):
                raise FeedError(f"MEXC futures {endpoint} returned {type(payload).__name__}, not an object")
            if payload.get("success") is False:
                raise FeedError(
                    f"MEXC futures {endpoint} failed: code={payload.get('code')} "
                    f"message={payload.get('message') or payload.get('msg') or 'unknown'}"
                )
            data = payload.get("data")
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            # Be explicit about a changed/blocked response.  Returning [] here is
            # what allowed startup to silently retain the tiny configured list.
            raise FeedError(f"MEXC futures {endpoint} response has no data list")

        def truthy(value: object) -> bool:
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)

        detail = await self._get_json(f"{MEXC_FUTURES_REST}/api/v1/contract/detail")
        contracts = rows(detail, "contract/detail")
        ticker_error: str | None = None
        try:
            tickers = await self._get_json(f"{MEXC_FUTURES_REST}/api/v1/contract/ticker")
            ticker_rows = rows(tickers, "contract/ticker")
        except Exception as exc:
            # Discovery must survive an optional ranking endpoint being down.
            ticker_rows = []
            ticker_error = f"{type(exc).__name__}: {exc}"
        stats = {str(t.get("symbol") or "").upper(): t for t in ticker_rows if t.get("symbol")}

        out: list[dict] = []
        excluded = {"non_usdt": 0, "disabled": 0, "hidden": 0, "malformed": 0}
        seen: set[str] = set()
        for item in contracts:
            native = str(item.get("symbol") or "").strip().upper()
            quote = str(item.get("quoteCoin") or "").strip().upper()
            if not native:
                excluded["malformed"] += 1
                continue
            if quote != "USDT" and not (not quote and native.endswith("_USDT")):
                excluded["non_usdt"] += 1
                continue
            # MEXC: state 0 = enabled; 1 delivery, 2 completed, 3 offline,
            # 4 paused. Hidden/offline contracts must not consume a socket and
            # then fail every history request.
            state = item.get("state")
            if state is not None and str(state) != "0":
                excluded["disabled"] += 1
                continue
            if truthy(item.get("isHidden", False)):
                excluded["hidden"] += 1
                continue
            symbol = plain_symbol(native)
            if not symbol or symbol in seen:
                excluded["malformed"] += 1
                continue
            seen.add(symbol)
            tick = stats.get(native) or {}
            turnover = max(0.0, _num(tick.get("amount24")))
            out.append(
                {
                    "symbol": symbol,
                    "base": item.get("baseCoin") or symbol.removesuffix("USDT"),
                    "quote": quote or "USDT",
                    "price": _num(tick.get("lastPrice")),
                    "change": _num(tick.get("riseFallRate")) * 100,
                    "turnover": turnover,
                    "ticker": bool(tick),
                    "liquid": turnover > 0,
                    "market": "futures",
                }
            )
        if not out:
            raise FeedError(
                f"MEXC futures contract/detail listed {len(contracts)} rows but no enabled USDT contracts"
            )
        self.universe_diagnostics = {
            "detail_rows": len(contracts),
            "ticker_rows": len(ticker_rows),
            "eligible": len(out),
            "missing_tickers": sum(1 for item in out if not item["ticker"]),
            "zero_turnover": sum(1 for item in out if not item["liquid"]),
            "excluded": excluded,
            "ticker_error": ticker_error,
        }
        # Positive turnover first, then deterministic symbol order. All enabled
        # contracts remain present even if the ticker endpoint was partial.
        return sorted(out, key=lambda item: (-item["turnover"], item["symbol"]))

    async def stream(self, symbols: list[str]) -> None:
        if websockets is None:
            raise FeedError("the 'websockets' package is not installed")
        async with websockets.connect(MEXC_FUTURES_WS, ping_interval=None, close_timeout=5, max_queue=4096) as ws:
            self.connected = True
            self.status = (
                f"futures - {len(self._symbols)} contracts on {len(self._chunks())} socket(s)"
            )
            for symbol in symbols:
                await ws.send(
                    json.dumps(
                        {
                            "method": "sub.kline",
                            "param": {"symbol": contract_symbol(symbol), "interval": "Min1"},
                        }
                    )
                )
            ping = asyncio.create_task(self._ping(ws))
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("channel") != "push.kline":
                        continue
                    data = message.get("data") or {}
                    symbol = plain_symbol(data.get("symbol") or "")
                    if not symbol:
                        continue
                    open_time = int(_num(data.get("t")) * 1000)
                    candle = _kline(
                        symbol,
                        open_time,
                        data.get("o"),
                        data.get("h"),
                        data.get("l"),
                        data.get("c"),
                        data.get("q") or data.get("v"),
                        closed=False,
                    )
                    previous = self._last_candle.get(symbol)
                    if previous is not None and open_time > previous["open_time"]:
                        gap = open_time - int(previous["open_time"])
                        if gap > 60_000:
                            # The socket was away across one or more closes. Pull
                            # the authoritative missing bars before resuming;
                            # otherwise a reconnect creates a permanent hole and
                            # can miss the exact break candle.
                            try:
                                missing = await self.history(
                                    symbol, "1m", min(2000, gap // 60_000 + 2), end_time=open_time - 1
                                )
                                recovered = [
                                    row for row in missing
                                    if int(previous["open_time"]) <= int(row["open_time"]) < open_time
                                ]
                                if recovered:
                                    for row in recovered:
                                        await self._push(row)
                                else:
                                    await self._push({**previous, "closed": True})
                            except Exception:
                                await self._push({**previous, "closed": True})
                        else:
                            await self._push({**previous, "closed": True})
                    self._last_candle[symbol] = candle
                    await self._push(dict(candle))
            finally:
                ping.cancel()

    @staticmethod
    async def _ping(ws) -> None:
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send(json.dumps({"method": "ping"}))
            except Exception:
                return


BINANCE_FUTURES_REST = "https://fapi.binance.com"
BINANCE_FUTURES_WS = "wss://fstream.binance.com"


class BinanceFuturesFeed(BaseFeed):
    """Binance USDT-M futures (fapi / fstream). ``market = "futures"``."""

    name = "binance"
    market = "futures"
    tf_map = {tf: tf for tf in TIMEFRAMES}
    chunk_size = 150

    def __init__(self, symbols: Iterable[str], **kwargs: object) -> None:
        super().__init__(symbols, **kwargs)
        self._last_candle: dict[str, dict] = {}

    async def history(self, symbol: str, interval: str, limit: int = 500,
                      end_time: int | None = None) -> list[dict]:
        symbol = symbol.upper()
        remaining = max(1, int(limit))
        out: list[dict] = []
        end = int(end_time) if end_time else None
        now_ms = int(time.time() * 1000)
        while remaining > 0:
            params: dict[str, object] = {"symbol": symbol, "interval": interval,
                                         "limit": min(remaining, 1000)}
            if end:
                params["endTime"] = end
            rows = await self._get_json(f"{BINANCE_FUTURES_REST}/fapi/v1/klines", params)
            candles = [
                _kline(
                    symbol,
                    int(r[0]),
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                    int(r[8]) if len(r) > 8 else 0,
                    duration_ms=int(TIMEFRAMES.get(interval, 60_000)),
                )
                for r in rows  # type: ignore[union-attr]
                if int(r[6]) < now_ms
            ]
            if not candles:
                break
            out = candles + out
            remaining -= len(candles)
            if len(rows) < min(remaining + len(candles), 1000):  # type: ignore[arg-type]
                break
            end = int(rows[0][0]) - 1  # type: ignore[index]
        if not out:
            raise FeedError(f"binance futures returned no closed {interval} candles for {symbol}")
        return out

    async def universe(self) -> list[dict]:
        info = await self._get_json(f"{BINANCE_FUTURES_REST}/fapi/v1/exchangeInfo")
        tickers = await self._get_json(f"{BINANCE_FUTURES_REST}/fapi/v1/ticker/24hr")
        stats = {t.get("symbol"): t for t in tickers or []}  # type: ignore[union-attr]
        out = []
        for item in (info or {}).get("symbols") or []:  # type: ignore[union-attr]
            if item.get("quoteAsset") != "USDT" or item.get("status") != "TRADING":
                continue
            if item.get("contractType") not in {"PERPETUAL", None}:
                continue
            tick = stats.get(item.get("symbol"))
            if not tick:
                continue
            turnover = _num(tick.get("quoteVolume"))
            if turnover <= 0:
                continue
            out.append(
                {
                    "symbol": item.get("symbol"),
                    "base": item.get("baseAsset"),
                    "quote": item.get("quoteAsset"),
                    "price": _num(tick.get("lastPrice")),
                    "change": _num(tick.get("priceChangePercent")),
                    "turnover": turnover,
                    "market": "futures",
                }
            )
        return sorted(out, key=lambda x: -x["turnover"])

    async def stream(self, symbols: list[str]) -> None:
        if websockets is None:
            raise FeedError("the 'websockets' package is not installed")
        streams = "/".join(f"{s.lower()}@kline_1m" for s in symbols)
        url = f"{BINANCE_FUTURES_WS}/stream?streams={streams}"
        async with websockets.connect(url, ping_interval=180, close_timeout=5, max_queue=4096) as ws:
            self.connected = True
            self.status = f"futures - {len(self._symbols)} contracts on one stream"
            async for raw in ws:
                message = json.loads(raw)
                k = (message.get("data") or {}).get("k") or {}
                if not k:
                    continue
                symbol = str(k.get("s") or "").upper()
                if not symbol:
                    continue
                candle = _kline(
                    symbol,
                    int(k.get("t", 0)),
                    k.get("o"),
                    k.get("h"),
                    k.get("l"),
                    k.get("c"),
                    k.get("v"),
                    int(k.get("n") or 0),
                    closed=bool(k.get("x")),
                )
                await self._push(candle)

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
    market = "replay"
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

    async def history(self, symbol: str, interval: str, limit: int = 500,
                      end_time: int | None = None) -> list[dict]:
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


SPOT_FEEDS = {"binance": BinanceFeed, "mexc": MexcFeed}
FUTURES_FEEDS = {"binance": BinanceFuturesFeed, "mexc": MexcFuturesFeed}
FEEDS = SPOT_FEEDS  # backwards compatible name


def make_feed(
    exchange: str,
    symbols: Iterable[str],
    market: str = "futures",
    **kwargs: object,
) -> BaseFeed:
    """Build the feed for an exchange **and a market** (perpetuals by default).

    The trader trades USDT-M perpetuals, so ``market="futures"`` is the default:
    spot wicks and contract wicks are not the same levels, and a signal taken off
    the wrong book is a signal for a market nobody is trading.
    """
    key = (exchange or "binance").lower()
    market = (market or "futures").lower()
    table = FUTURES_FEEDS if market == "futures" else SPOT_FEEDS
    if key not in table:
        raise FeedError(
            f"unknown exchange {exchange!r} for the {market} market "
            f"(choose from {', '.join(table)})"
        )
    feed = table[key](symbols, **kwargs)
    feed.market = market
    return feed
