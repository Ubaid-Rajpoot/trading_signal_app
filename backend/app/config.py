"""Application configuration and shared constants."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
DATA_DIR = Path(os.getenv("TSA_DATA_DIR", str(BASE_DIR / "data")))
CACHE_DIR = DATA_DIR / "cache"
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "signals.db"

for _p in (DATA_DIR, CACHE_DIR):
    _p.mkdir(parents=True, exist_ok=True)

APP_NAME = "SignalForge"
APP_VERSION = "0.1.0"

# Public Binance REST endpoints. api.binance.com can be geo-restricted in some
# regions, so data-api.binance.vision (official public market-data mirror) is a
# fallback. No API key is required for any of the endpoints we call.
BINANCE_HOSTS = [
    "https://api.binance.com",
    "https://data-api.binance.vision",
]
HTTP_TIMEOUT = float(os.getenv("TSA_HTTP_TIMEOUT", "8"))
USER_AGENT = "SignalForge/0.1 (+https://github.com/Ubaid-Rajpoot/trading_signal_app)"

# timeframe -> (binance interval, seconds)
TIMEFRAMES: dict[str, dict[str, int | str]] = {
    "1m": {"interval": "1m", "seconds": 60, "label": "1 minute"},
    "3m": {"interval": "3m", "seconds": 180, "label": "3 minutes"},
    "5m": {"interval": "5m", "seconds": 300, "label": "5 minutes"},
    "15m": {"interval": "15m", "seconds": 900, "label": "15 minutes"},
    "30m": {"interval": "30m", "seconds": 1800, "label": "30 minutes"},
    "1h": {"interval": "1h", "seconds": 3600, "label": "1 hour"},
    "4h": {"interval": "4h", "seconds": 14400, "label": "4 hours"},
    "1d": {"interval": "1d", "seconds": 86400, "label": "1 day"},
}
DEFAULT_TIMEFRAME = "15m"
# The scalping timeframes the TDIGM setup is read on.
SCAN_TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m"]

# Curated liquid USDT pairs shown in the picker (any Binance symbol still works).
DEFAULT_SYMBOLS: list[dict[str, str]] = [
    {"symbol": "BTCUSDT", "base": "BTC", "name": "Bitcoin"},
    {"symbol": "ETHUSDT", "base": "ETH", "name": "Ethereum"},
    {"symbol": "SOLUSDT", "base": "SOL", "name": "Solana"},
    {"symbol": "BNBUSDT", "base": "BNB", "name": "BNB"},
    {"symbol": "XRPUSDT", "base": "XRP", "name": "XRP"},
    {"symbol": "ADAUSDT", "base": "ADA", "name": "Cardano"},
    {"symbol": "DOGEUSDT", "base": "DOGE", "name": "Dogecoin"},
    {"symbol": "AVAXUSDT", "base": "AVAX", "name": "Avalanche"},
    {"symbol": "LINKUSDT", "base": "LINK", "name": "Chainlink"},
    {"symbol": "DOTUSDT", "base": "DOT", "name": "Polkadot"},
    {"symbol": "MATICUSDT", "base": "MATIC", "name": "Polygon"},
    {"symbol": "LTCUSDT", "base": "LTC", "name": "Litecoin"},
    {"symbol": "ATOMUSDT", "base": "ATOM", "name": "Cosmos"},
    {"symbol": "NEARUSDT", "base": "NEAR", "name": "NEAR"},
    {"symbol": "APTUSDT", "base": "APT", "name": "Aptos"},
    {"symbol": "ARBUSDT", "base": "ARB", "name": "Arbitrum"},
    {"symbol": "OPUSDT", "base": "OP", "name": "Optimism"},
    {"symbol": "SUIUSDT", "base": "SUI", "name": "Sui"},
    {"symbol": "INJUSDT", "base": "INJ", "name": "Injective"},
    {"symbol": "TIAUSDT", "base": "TIA", "name": "Celestia"},
]

DEFAULT_WATCHLIST = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
]

# Rough anchor prices so generated demo data looks believable per asset.
DEMO_ANCHOR_PRICES = {
    "BTCUSDT": 68_000.0,
    "ETHUSDT": 3_400.0,
    "SOLUSDT": 165.0,
    "BNBUSDT": 590.0,
    "XRPUSDT": 0.58,
    "ADAUSDT": 0.45,
    "DOGEUSDT": 0.14,
    "AVAXUSDT": 34.0,
    "LINKUSDT": 16.5,
    "DOTUSDT": 7.1,
    "MATICUSDT": 0.62,
    "LTCUSDT": 82.0,
    "ATOMUSDT": 8.4,
    "NEARUSDT": 5.6,
    "APTUSDT": 9.2,
    "ARBUSDT": 1.1,
    "OPUSDT": 2.3,
    "SUIUSDT": 1.6,
    "INJUSDT": 26.0,
    "TIAUSDT": 9.5,
}
