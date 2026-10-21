"""The scan universe: which instruments to look at, and in what order.

Two sources:

* **live** - ``/api/v3/exchangeInfo`` for the tradable USDT pairs plus
  ``/api/v3/ticker/24hr`` for turnover, so the list is ordered by how much money
  actually moves through each pair. Cached on disk for a day.
* **curated** - the built-in list in :mod:`app.config`, used when the exchange is
  unreachable. Order is still sensible, so a scan works offline (on the demo
  feed) instead of failing.

``exclude_leveraged`` drops UP/DOWN/BULL/BEAR tokens, which are not spot pairs a
swing trader wants in a radar.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from ...config import BINANCE_HOSTS, CACHE_DIR, DEFAULT_SYMBOLS, HTTP_TIMEOUT, USER_AGENT

UNIVERSE_FILE = CACHE_DIR / "universe.json"
UNIVERSE_TTL = 24 * 3600
_LEVERAGED = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def _curated(quote: str, limit: int) -> list[str]:
    symbols = [row["symbol"] for row in DEFAULT_SYMBOLS if row["symbol"].endswith(quote)]
    return symbols[:limit]


def _download(quote: str) -> list[dict]:
    """Tradable pairs with 24h turnover, biggest first. Raises if offline."""
    last_error: Exception | None = None
    for host in BINANCE_HOSTS:
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
                info = client.get(f"{host}/api/v3/exchangeInfo").json()
                tickers = client.get(f"{host}/api/v3/ticker/24hr").json()
            tradable = {
                row["symbol"]
                for row in info.get("symbols", [])
                if row.get("status") == "TRADING" and row.get("quoteAsset") == quote
                and row.get("isSpotTradingAllowed", True)
            }
            turnover = {
                row["symbol"]: float(row.get("quoteVolume") or 0)
                for row in tickers
                if row.get("symbol") in tradable
            }
            rows = [
                {"symbol": symbol, "quote_volume": volume}
                for symbol, volume in turnover.items()
                if not symbol.endswith(_LEVERAGED)
            ]
            rows.sort(key=lambda r: r["quote_volume"], reverse=True)
            if rows:
                return rows
            raise RuntimeError("exchange returned no tradable pairs")
        except Exception as exc:  # try the mirror
            last_error = exc
    raise RuntimeError(f"universe unavailable: {last_error}")


def universe(quote: str = "USDT", limit: int = 100, refresh: bool = False) -> dict:
    """Return ``{symbols, source, updated_at}`` for the scan universe.

    ``source`` is ``live`` (exchange metadata) or ``curated`` (built-in list).
    """
    meta: dict = {}
    if UNIVERSE_FILE.exists() and not refresh:
        try:
            meta = json.loads(UNIVERSE_FILE.read_text())
            if time.time() - float(meta.get("fetched_at", 0)) < UNIVERSE_TTL:
                symbols = [s for s in meta.get("symbols", []) if s.endswith(quote)]
                if symbols:
                    return {
                        "symbols": symbols[:limit],
                        "source": "live",
                        "updated_at": int(meta["fetched_at"] * 1000),
                    }
        except Exception:
            meta = {}

    try:
        rows = _download(quote)
        UNIVERSE_FILE.write_text(
            json.dumps(
                {
                    "fetched_at": time.time(),
                    "quote": quote,
                    "symbols": [r["symbol"] for r in rows],
                    "turnover": {r["symbol"]: r["quote_volume"] for r in rows},
                }
            )
        )
        return {
            "symbols": [r["symbol"] for r in rows][:limit],
            "source": "live",
            "updated_at": int(time.time() * 1000),
        }
    except Exception:
        # stale disk copy is still real metadata
        symbols = [s for s in meta.get("symbols", []) if s.endswith(quote)]
        if symbols:
            return {
                "symbols": symbols[:limit],
                "source": "cache",
                "updated_at": int(float(meta.get("fetched_at", 0)) * 1000),
            }
        return {"symbols": _curated(quote, limit), "source": "curated", "updated_at": None}
