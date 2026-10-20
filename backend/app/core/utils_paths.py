"""Cache path helpers kept separate to avoid circular imports."""

from __future__ import annotations

from pathlib import Path

from ..config import CACHE_DIR


def history_cache_path(symbol: str, timeframe: str, days: int) -> Path:
    folder = Path(CACHE_DIR) / "history"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{symbol.upper()}_{timeframe}_{days}d.csv"
