"""Reading and writing ``config.json`` - including from the web page.

The page can add and remove instruments, and that has to survive a restart, so
the change is written to the same file the bot boots from. Writes are atomic
(temp file + rename) so a crash mid-write cannot leave a half config behind,
and the file is written pretty-printed so it stays hand-editable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

DEFAULT_FILTERS: dict[str, Any] = {
    "structure": True,
    "hvb": True,
    "hvb_min_points": 1.0,
    "hvb_min_speed": 1.5,
    "regime": True,
    "regime_adx_min": 20.0,
    "regime_er_min": 0.10,
    "regime_direction": "any",
    "break": False,
    "sides": ["long", "short"],
    "min_rr": 0.0,
    "max_bars_since_sweep": 0,
    "trend": "off",
    "trend_ema": 50,
    "sessions": [],
    "cooldown_minutes": 0,
}

DEFAULT_STRUCTURE: dict[str, Any] = {
    "pivot_len": 5,
    "sweep_max_wait": 60,
    "level_max_age": 180,
    "max_levels": 40,
}


def read(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"missing {path}")
    data = json.loads(path.read_text())
    data.setdefault("filters", dict(DEFAULT_FILTERS))
    data.setdefault("structure", dict(DEFAULT_STRUCTURE))
    data.setdefault("exchange", "binance")
    data.setdefault("max_symbols", 500)
    return data


def write(path: str | Path, config: dict[str, Any]) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    os.replace(tmp, path)


def normalise_symbol(value: str) -> str:
    symbol = "".join(ch for ch in str(value).upper().strip() if ch.isalnum())
    return symbol


def clean_symbols(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        symbol = normalise_symbol(value)
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def set_symbols(path: str | Path, config: dict[str, Any], symbols: Iterable[str]) -> dict[str, Any]:
    """Persist a symbol list, keeping the rest of the file untouched."""
    config["symbols"] = clean_symbols(symbols)
    write(path, config)
    return config


def validate_structure(payload: dict[str, Any]) -> dict[str, Any]:
    """Only known keys, clamped to sane ranges - this writes a user-facing file."""
    merged = dict(DEFAULT_STRUCTURE)
    for key in ("pivot_len", "sweep_max_wait", "level_max_age", "max_levels"):
        if key not in payload:
            continue
        try:
            merged[key] = max(1, int(payload[key]))
        except (TypeError, ValueError):
            pass
    if "target_mode" in payload:
        merged["target_mode"] = payload["target_mode"] if payload["target_mode"] in {"next", "range"} else "next"
    return merged


def validate_filters(payload: dict[str, Any]) -> dict[str, Any]:
    """Only accept known keys, with sane types - the file is user-facing."""
    merged = dict(DEFAULT_FILTERS)
    for key in DEFAULT_FILTERS:
        if key not in payload:
            continue
        value = payload[key]
        if key in {"structure", "hvb", "break", "regime"}:
            merged[key] = bool(value)
        elif key in {"hvb_min_points", "hvb_min_speed", "regime_adx_min", "regime_er_min", "min_rr"}:
            try:
                merged[key] = max(0.0, float(value))
            except (TypeError, ValueError):
                pass
        elif key in {"max_bars_since_sweep", "trend_ema", "cooldown_minutes"}:
            try:
                merged[key] = max(0, int(value))
            except (TypeError, ValueError):
                pass
        elif key == "trend":
            merged[key] = value if value in {"off", "with", "against"} else "off"
        elif key == "regime_direction":
            merged[key] = value if value in {"any", "with"} else "any"
        elif key == "sides":
            sides = [s for s in (value or []) if s in {"long", "short"}]
            merged[key] = sides or ["long", "short"]
        elif key == "sessions":
            merged[key] = [str(s) for s in (value or [])][:8]
    return merged
