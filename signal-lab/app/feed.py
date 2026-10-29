"""MEXC futures klines - the same exchange the bot trades on.

Lessons carried over from the previous app, because they were expensive:

* MEXC sometimes serves **plain JSON labelled** ``content-encoding: br``. If you
  let httpx/requests decode the body it can raise
  ``DecodingError: brotli: decoder process called with data when
  'can_accept_more_data()' is False``. So: ask for raw bytes, look at the first
  byte, and only treat the body as compressed if it actually is.
* A symbol can legitimately have no kline for a fresh listing - that is a
  retry-then-skip, not a crash.
"""
from __future__ import annotations

import gzip
import json
import time
import zlib
from typing import Any

import httpx

BASE = "https://contract.mexc.com"
# MEXC serves these intervals directly. Everything else (3m, 10m, 45m, 2h, 3h) is
# built by aggregating the nearest native interval below it.
TF_TO_INTERVAL = {"1m": "Min1", "5m": "Min5", "15m": "Min15", "30m": "Min30",
                  "1h": "Min60", "4h": "Hour4"}
NATIVE_FOR = {"1m": "1m", "3m": "1m", "5m": "5m", "10m": "5m", "15m": "15m", "30m": "15m",
              "45m": "15m", "1h": "1h", "2h": "1h", "3h": "1h", "4h": "4h"}


def decode_body(raw: bytes, encoding: str = "") -> bytes:
    """Return the bytes of a body, decoding only if the magic says so."""
    if not raw:
        return raw
    if raw[:1] in (b"{", b"[", b" "):
        return raw          # plain JSON, whatever the header claimed
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if raw[:1] == b"\x78":
        return zlib.decompress(raw)
    try:
        return zlib.decompress(raw, -zlib.MAX_WBITS)
    except zlib.error:
        return raw


def get_json(client: httpx.Client, path: str, params: dict[str, Any] | None = None,
             timeout: float = 20.0) -> Any:
    r = client.get(BASE + path, params=params, timeout=timeout,
                   headers={"accept-encoding": "identity", "user-agent": "signal-lab/1.0"})
    r.raise_for_status()
    body = decode_body(r.content, r.headers.get("content-encoding", ""))
    return json.loads(body)


def klines(client: httpx.Client, symbol: str, tf: str, bars: int = 400) -> list[list[float]]:
    """Most recent `bars` candles: [[time_seconds, open, close, high, low, vol], ...]."""
    interval = TF_TO_INTERVAL[tf]
    now = int(time.time())
    span = bars * {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}[tf] * 2
    data = get_json(client, f"/api/v1/contract/kline/{symbol}",
                    {"interval": interval, "start": now - span, "end": now})
    rows = (data.get("data") or {}).get("data") if isinstance(data.get("data"), dict) else data.get("data")
    if not rows:
        # flat-array shape
        d = data.get("data") or {}
        if isinstance(d, dict) and "time" in d:
            rows = list(zip(d["time"], d["open"], d["close"], d["high"], d["low"], d["vol"]))
        else:
            rows = []
    out = []
    for r in rows:
        if isinstance(r, (list, tuple)) and len(r) >= 6:
            out.append([float(x) for x in r[:6]])
    return out[-bars:]


def native_intervals(tfs: list[str]) -> list[str]:
    """The distinct exchange intervals needed to build `tfs`."""
    out = []
    for tf in tfs:
        n = NATIVE_FOR.get(tf, tf)
        if n not in out:
            out.append(n)
    return out


def klines_raw(client: httpx.Client, symbol: str, interval: str, bars: int = 2000) -> list[list[float]]:
    """Most recent `bars` candles of an exchange interval, oldest first."""
    span = bars * {"Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800,
                   "Min60": 3600, "Hour4": 14400}.get(interval, 3600) * 2
    now = int(time.time())
    data = get_json(client, f"/api/v1/contract/kline/{symbol}",
                    {"interval": interval, "start": now - span, "end": now})
    d = data.get("data")
    rows: list[list[float]] = []
    if isinstance(d, dict) and "time" in d:
        rows = list(zip(d["time"], d["open"], d["close"], d["high"], d["low"], d["vol"]))
    elif isinstance(d, list):
        for r in d:
            if isinstance(r, (list, tuple)) and len(r) >= 6:
                rows.append(r[:6])
    return [[float(x) for x in r] for r in rows][-bars:]


def symbols(client: httpx.Client, top: int = 60) -> list[str]:
    """The most liquid USDT futures contracts (same universe the bot uses)."""
    try:
        tick = get_json(client, "/api/v1/contract/ticker")
        rows = tick.get("data") or []
        rows = [r for r in rows if str(r.get("symbol", "")).endswith("_USDT")]
        rows.sort(key=lambda r: float(r.get("amount24") or 0), reverse=True)
        return [r["symbol"] for r in rows[:top]]
    except Exception:
        return ["BTC_USDT", "ETH_USDT"]
