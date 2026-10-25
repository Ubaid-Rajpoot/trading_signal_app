"""Reading and writing ``config.json`` - including from the web page.

Instrument list and detection settings survive a restart. Writes are atomic
(temp file + rename) so a crash mid-write cannot leave half a config behind.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .map import DEFAULTS

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]

DEFAULT_SETTINGS: dict[str, Any] = dict(DEFAULTS)


def clean_symbols(values: Any) -> list[str]:
    """Upper-case instrument names, keeping the one separator that matters.

    ``BTC_USDT`` is a futures contract and ``BTCUSDT`` is the spot pair, so the
    underscore is kept (``BTC/USDT`` from a screenshot becomes ``BTC_USDT``);
    every other stray character is still removed.
    """
    out: list[str] = []
    for value in values or []:
        raw = str(value).upper().strip().replace("/", "_").replace("-", "_")
        symbol = "".join(ch for ch in raw if ch.isalnum() or ch == "_").strip("_")
        while "__" in symbol:
            symbol = symbol.replace("__", "_")
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def validate_settings(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_SETTINGS)
    for key, value in (payload or {}).items():
        if key not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[key]
        try:
            if isinstance(default, bool):
                merged[key] = bool(value)
            elif isinstance(default, int):
                merged[key] = max(1, int(value))
            elif isinstance(default, float):
                merged[key] = float(value)
            elif isinstance(default, list):
                if key == "sessions" and isinstance(value, list):
                    sessions = []
                    for item in value:
                        if isinstance(item, dict) and "start" in item and "end" in item:
                            sessions.append(
                                {
                                    "name": str(item.get("name") or "session"),
                                    "start": max(0, min(23, int(item["start"]))),
                                    "end": max(0, min(24, int(item["end"]))),
                                }
                            )
                    merged[key] = sessions or list(default)
            elif isinstance(default, str):
                merged[key] = str(value)
        except (TypeError, ValueError):
            continue
    # a pivot window wider than the major window makes the labels meaningless
    merged["pivot_len"] = max(1, int(merged["pivot_len"]))
    merged["major_len"] = max(int(merged["pivot_len"]) + 1, int(merged["major_len"]))
    if merged.get("mitigation_mode") not in {"zone", "level"}:
        merged["mitigation_mode"] = "zone"
    if merged.get("plan_stop_mode") not in {"level", "block"}:
        merged["plan_stop_mode"] = "level"
    # the target minimum and the fee ceiling are fractions of a plan, not prices
    merged["plan_target_atr_min"] = max(0.0, min(10.0, float(merged.get("plan_target_atr_min", 1.0))))
    merged["signal_prob_min"] = max(0.3, min(0.999, float(merged.get("signal_prob_min", 0.85))))
    merged["induce_count"] = max(1, int(merged["induce_count"]))
    return merged


def read(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {
            "symbols": list(DEFAULT_SYMBOLS),
            "timeframes": ["1m", "5m", "15m", "30m", "1h"],
            "settings": dict(DEFAULT_SETTINGS),
            "exchange": "mexc",
            "max_symbols": 400,
            "server": {"host": "0.0.0.0", "port": 8800},
        }
    data = json.loads(path.read_text())
    data.setdefault("symbols", list(DEFAULT_SYMBOLS))
    data.setdefault("timeframes", ["1m", "5m", "15m", "30m", "1h"])
    data["settings"] = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
    data.setdefault("exchange", "mexc")
    data.setdefault("max_symbols", 400)
    data.setdefault("server", {"host": "0.0.0.0", "port": 8800})
    return data


def write(path: str | Path, config: dict[str, Any]) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    os.replace(tmp, path)
