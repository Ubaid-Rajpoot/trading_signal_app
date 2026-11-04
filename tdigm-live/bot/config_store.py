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

# One source of truth: the defaults the gate actually uses (bot/filters.py).
# A key that is missing here but present there would be silently dropped on the
# first save from the page, which is exactly the kind of bug this avoids.
from .filters import DEFAULT_FILTERS  # noqa: E402,F401  (re-exported)

DEFAULT_MARKET = "futures"

DEFAULT_STRUCTURE: dict[str, Any] = {
    "pivot_len": 5,
    "major_len": 20,
    "macro_len": 80,
    "hh_tolerance_atr": 0.10,
    "sweep_max_wait": 60,
    "level_max_age": 180,
    "max_levels": 40,
}

# keys added after the first release: the file on disk may not have them yet
_INT_KEYS = {"max_bars_since_sweep", "trend_ema", "cooldown_minutes", "bos_max_age",
             "volume_period"}
_FLOAT_KEYS = {"hvb_min_points", "hvb_min_speed", "regime_adx_min", "regime_er_min", "min_rr",
               "premium_level", "pool_tolerance", "pool_count", "structure_atr_min",
               "structure_atr_max", "volume_mult", "confirm_min_score",
               "breaker_body_atr", "breaker_stop_buffer_atr", "min_directional_ratio"}
_BOOL_KEYS = {"structure", "hvb", "break", "regime", "premium_discount", "structure_direction",
              "require_ms", "require_fvg", "block_liquidity_pool", "premium_on_entry",
              "trend_context", "require_hh_hl", "require_bos", "volume_break",
              "clear_structure"}
# whole-number keys that must stay integers.  Depth starts at one; windows may
# intentionally be zero (same-candle HVB, and no age-only setup expiry).
_COUNT_KEYS = {"max_internal_depth", "min_trend_steps"}
_ZERO_COUNT_KEYS = {"setup_max_bars", "hvb_window", "fresh_shift_max_bars"}


def read(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"missing {path}")
    data = json.loads(path.read_text())
    data.setdefault("filters", dict(DEFAULT_FILTERS))
    data.setdefault("structure", dict(DEFAULT_STRUCTURE))
    data.setdefault("exchange", "binance")
    data.setdefault("max_symbols", 2000)
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
    for key in ("pivot_len", "sweep_max_wait", "level_max_age", "max_levels",
                "major_len", "macro_len"):
        if key not in payload:
            continue
        try:
            merged[key] = max(1, int(payload[key]))
        except (TypeError, ValueError):
            pass
    if "hh_tolerance_atr" in payload:
        try:
            merged["hh_tolerance_atr"] = max(0.0, float(payload["hh_tolerance_atr"]))
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
        if key in _BOOL_KEYS:
            merged[key] = bool(value)
        elif key in _FLOAT_KEYS:
            try:
                merged[key] = max(0.0, float(value))
            except (TypeError, ValueError):
                pass
        elif key in _COUNT_KEYS:
            try:
                merged[key] = max(1, int(value))
            except (TypeError, ValueError):
                pass
        elif key in _ZERO_COUNT_KEYS:
            try:
                merged[key] = max(0, int(value))
            except (TypeError, ValueError):
                pass
        elif key in _INT_KEYS:
            try:
                merged[key] = max(0, int(value))
            except (TypeError, ValueError):
                pass
        elif key == "confirm_mode":
            merged[key] = value if value in {"all", "score"} else "all"
        elif key == "trend":
            merged[key] = value if value in {"off", "with", "against"} else "off"
        elif key == "regime_direction":
            merged[key] = value if value in {"any", "with"} else "any"
        elif key == "sides":
            sides = [s for s in (value or []) if s in {"long", "short"}]
            merged[key] = sides or ["long", "short"]
        elif key == "sessions":
            merged[key] = [str(s) for s in (value or [])][:8]
    merged["min_directional_ratio"] = min(1.0, merged["min_directional_ratio"])
    # These are strategy invariants, not optional confluence knobs. Keeping them
    # fixed prevents saved settings or score mode from admitting a body-break
    # candle without its own moving-centre-band HVB.
    merged["hvb"] = True
    merged["hvb_window"] = 0
    return merged
