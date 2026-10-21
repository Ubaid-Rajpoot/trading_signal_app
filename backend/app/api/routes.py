"""HTTP API."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import APP_NAME, APP_VERSION, DEFAULT_SYMBOLS, DEFAULT_WATCHLIST, SCAN_TIMEFRAMES, TIMEFRAMES
from ..core import backtest_service, winrate as winrate_service
from ..core.alerts import notifier, scheduler
from ..core.data.history import probe as history_probe
from ..core.data.symbols import universe as symbol_universe
from ..core.data.providers import MarketDataError, provider_status
from ..core.signals import build_signal, scan
from ..core.store import (
    get_settings,
    list_notifications,
    list_signals,
    redacted_settings,
    save_settings,
    signal_stats,
)
from ..core.strategies import list_strategies
from ..utils import sanitize

router = APIRouter(prefix="/api")


def _params(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="params must be a JSON object")


class SettingsPatch(BaseModel):
    watchlist: list[str] | None = None
    timeframe: str | None = None
    strategy: str | None = None
    refresh_seconds: int | None = Field(default=None, ge=20, le=3600)
    alerts: dict[str, Any] | None = None


class BacktestRequest(BaseModel):
    symbol: str = "BTCUSDT"
    timeframe: str = "15m"
    strategy: str = "tdi_shift"
    params: dict[str, Any] = Field(default_factory=dict)
    bars: int = Field(default=800, ge=100, le=1000)
    initial_equity: float = Field(default=10_000, gt=0)
    risk_pct: float = Field(default=1.0, gt=0, le=100)
    fee_bps: float = Field(default=5.0, ge=0, le=100)
    slippage_bps: float = Field(default=2.0, ge=0, le=100)
    leverage: float = Field(default=1.0, ge=1, le=20)


@router.get("/health")
def health() -> dict[str, Any]:
    return sanitize(
        {
            "status": "ok",
            "app": APP_NAME,
            "version": APP_VERSION,
            "provider": provider_status(),
            "stats": signal_stats(),
        }
    )


@router.get("/meta")
def meta() -> dict[str, Any]:
    strategies = list_strategies()
    return sanitize(
        {
            "app": APP_NAME,
            "version": APP_VERSION,
            "strategies": strategies,
            "timeframes": [{"value": k, "label": v["label"]} for k, v in TIMEFRAMES.items()],
            "scan_timeframes": SCAN_TIMEFRAMES,
            "symbols": DEFAULT_SYMBOLS,
            "default_watchlist": DEFAULT_WATCHLIST,
            "default_strategy": strategies[0]["id"],
            "provider": provider_status(),
            "settings": redacted_settings(),
        }
    )


@router.get("/signal")
def signal(
    symbol: str = Query("BTCUSDT"),
    timeframe: str = Query("15m"),
    strategy: str = Query("tdi_shift"),
    params: str | None = Query(None, description="JSON object of strategy parameter overrides"),
    limit: int = Query(400, ge=100, le=1000),
) -> dict[str, Any]:
    try:
        return sanitize(build_signal(symbol, timeframe, strategy, _params(params), limit=limit))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except MarketDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/scan")
def scan_endpoint(
    symbols: str = Query("", description="Comma-separated symbols; defaults to the saved watchlist"),
    timeframe: str = Query("15m"),
    strategy: str = Query("tdi_shift"),
    params: str | None = Query(None),
) -> dict[str, Any]:
    if symbols.strip():
        wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        wanted = get_settings().get("watchlist", DEFAULT_WATCHLIST)
    if len(wanted) > 25:
        raise HTTPException(status_code=400, detail="At most 25 symbols per scan")
    try:
        return sanitize(scan(wanted, timeframe, strategy, _params(params)))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/backtest")
def backtest(payload: BacktestRequest) -> dict[str, Any]:
    try:
        return sanitize(
            backtest_service.run(
                payload.symbol,
                payload.timeframe,
                payload.strategy,
                payload.params,
                bars=payload.bars,
                initial_equity=payload.initial_equity,
                risk_pct=payload.risk_pct,
                fee_bps=payload.fee_bps,
                slippage_bps=payload.slippage_bps,
                leverage=payload.leverage,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (MarketDataError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/symbols")
def symbols(
    limit: int = Query(50, ge=1, le=500),
    quote: str = Query("USDT"),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    """The scan universe: tradable pairs ordered by 24h turnover."""
    return sanitize(symbol_universe(quote.upper(), limit, refresh))


@router.get("/data-source")
def data_source(
    symbol: str = Query("BTCUSDT"),
    timeframe: str = Query("15m"),
    days: int = Query(30, ge=1, le=120),
) -> dict[str, Any]:
    """Where the candles for this window come from - hosts, cache, source.

    Exposed so the data trail is inspectable from the UI or curl instead of
    being taken on trust.
    """
    try:
        return sanitize(history_probe(symbol, timeframe, days))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/winrate")
def winrate(
    symbol: str = Query("BTCUSDT"),
    days: int = Query(30, ge=1, le=120),
    timeframes: str = Query("", description="Comma-separated; defaults to 1m,3m,5m,15m,30m"),
    strategy: str = Query("tdi_shift"),
    params: str | None = Query(None),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    tfs = [t.strip() for t in timeframes.split(",") if t.strip()] or None
    try:
        return sanitize(
            winrate_service.report(
                symbol=symbol,
                days=days,
                timeframes=tfs,
                strategy=strategy,
                params=_params(params),
                refresh=refresh,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/radar")
def radar(
    symbols: str = Query("", description="Comma-separated; defaults to the saved watchlist"),
    timeframes: str = Query("", description="Comma-separated; defaults to 1m,3m,5m,15m,30m"),
    strategy: str = Query("tdi_shift"),
    params: str | None = Query(None),
    scope: str = Query("watchlist", description="watchlist | top (the whole market by turnover)"),
    limit: int = Query(25, ge=1, le=40, description="how many symbols when scope=top"),
) -> dict[str, Any]:
    from ..config import SCAN_TIMEFRAMES

    tfs = [t.strip() for t in timeframes.split(",") if t.strip()] or SCAN_TIMEFRAMES
    explicit = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if explicit:
        wanted = explicit
    elif scope == "top":
        wanted = symbol_universe("USDT", limit)["symbols"]
    else:
        wanted = get_settings().get("watchlist", [])
    if len(wanted) > 40 or len(tfs) > 8:
        raise HTTPException(status_code=400, detail="At most 40 symbols and 8 timeframes per radar scan")

    resolved = _params(params)
    grid: list[dict[str, Any]] = []
    for symbol in wanted:
        cells: dict[str, Any] = {}
        for tf in tfs:
            try:
                signal = build_signal(symbol, tf, strategy, resolved, limit=500)
                cells[tf] = {
                    "status": signal["setup"]["status"],
                    "direction": signal["setup"]["direction"],
                    "level": signal["setup"]["level"],
                    "distance_pct": signal["setup"]["distance_pct"],
                    "price": signal["price"],
                    "change_pct": signal["change_pct"],
                    "tdi": signal["setup"]["tdi"],
                    "text": signal["setup"]["text"],
                    "blocked_reason": signal["setup"].get("blocked_reason"),
                    "hvb_last_bars": signal["setup"].get("hvb_last_bars"),
                    "regime": (signal.get("regime") or {}).get("state"),
                    "source": signal["source"],
                }
            except Exception as exc:
                cells[tf] = {"status": "ERROR", "error": str(exc)}
        grid.append({"symbol": symbol, "cells": cells})

    actionable = [
        {"symbol": row["symbol"], "timeframe": tf, **cell}
        for row in grid
        for tf, cell in row["cells"].items()
        if cell.get("status") in ("TRIGGERED", "ARMED", "FORMING")
    ]
    actionable.sort(key=lambda r: ({"TRIGGERED": 0, "ARMED": 1, "FORMING": 2}.get(r["status"], 3), r["symbol"]))
    blocked = sum(1 for row in grid for cell in row["cells"].values() if cell.get("status") == "BLOCKED")
    return sanitize(
        {
            "generated_at": int(time.time() * 1000),
            "symbols": wanted,
            "timeframes": tfs,
            "scope": "custom" if explicit else scope,
            "grid": grid,
            "actionable": actionable,
            "blocked_count": blocked,
        }
    )


@router.get("/signals")
def signals(
    limit: int = Query(100, ge=1, le=500),
    symbol: str | None = Query(None),
    strategy: str | None = Query(None),
) -> dict[str, Any]:
    return sanitize({"items": list_signals(limit, symbol, strategy)})


@router.get("/settings")
def read_settings() -> dict[str, Any]:
    return sanitize(redacted_settings())


@router.put("/settings")
def update_settings(patch: SettingsPatch) -> dict[str, Any]:
    data = patch.model_dump(exclude_none=True)
    if "timeframe" in data and data["timeframe"] not in TIMEFRAMES:
        raise HTTPException(status_code=400, detail="Unknown timeframe")
    if "watchlist" in data:
        cleaned = [s.strip().upper() for s in data["watchlist"] if s.strip()][:25]
        if not cleaned:
            raise HTTPException(status_code=400, detail="Watchlist cannot be empty")
        data["watchlist"] = cleaned
    updated = save_settings(data)
    return sanitize(redacted_settings() if updated else updated)


@router.get("/alerts")
def alerts_log(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    return sanitize({"items": list_notifications(limit), "scheduler": scheduler.status()})


@router.post("/alerts/test")
def alerts_test(channel: str = Query("webhook", pattern="^(telegram|webhook)$")) -> dict[str, Any]:
    return sanitize(notifier.test_channel(channel))


@router.get("/scheduler")
def scheduler_status() -> dict[str, Any]:
    return sanitize(scheduler.status())
