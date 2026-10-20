"""HTTP API."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import APP_NAME, APP_VERSION, DEFAULT_SYMBOLS, DEFAULT_WATCHLIST, TIMEFRAMES
from ..core import backtest_service
from ..core.alerts import notifier, scheduler
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
    timeframe: str = "1h"
    strategy: str = "donchian"
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
    timeframe: str = Query("1h"),
    strategy: str = Query("donchian"),
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
    timeframe: str = Query("1h"),
    strategy: str = Query("donchian"),
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
