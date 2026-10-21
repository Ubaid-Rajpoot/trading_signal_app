"""Strategy registry.

Add your strategy module to ``_REGISTRY`` to make it available everywhere.
"""

from __future__ import annotations

from .base import ParamSpec, RuleCheck, Strategy, Targets, TriggerSet
from .custom import CustomStrategy
from .donchian import DonchianBreakout
from .ema_trend import EmaTrend
from .tdi_shift import TdiShift
from .tdi_shift_v1 import TdiShiftV1

_REGISTRY: dict[str, type[Strategy]] = {
    TdiShift.id: TdiShift,
    TdiShiftV1.id: TdiShiftV1,
    DonchianBreakout.id: DonchianBreakout,
    EmaTrend.id: EmaTrend,
    CustomStrategy.id: CustomStrategy,
}

# Old ids keep working: ``tdi_shift`` now means "the latest version of the
# trader's system". Pinned ids (e.g. tdi_shift_v1) never move.
_ALIASES: dict[str, str] = {
    "tdi_shift": TdiShift.id,
}

DEFAULT_STRATEGY = TdiShift.id


def resolve_id(strategy_id: str | None) -> str:
    key = strategy_id or DEFAULT_STRATEGY
    return _ALIASES.get(key, key)


def get_strategy(strategy_id: str | None) -> Strategy:
    key = resolve_id(strategy_id)
    cls = _REGISTRY.get(key)
    if cls is None:
        raise KeyError(f"Unknown strategy '{key}'")
    return cls()


def get_strategy_class(strategy_id: str | None) -> type[Strategy]:
    key = resolve_id(strategy_id)
    cls = _REGISTRY.get(key)
    if cls is None:
        raise KeyError(f"Unknown strategy '{key}'")
    return cls


def list_strategies() -> list[dict]:
    """Every available rule set, newest first, with its version metadata."""
    return [cls.schema() for cls in _REGISTRY.values()]


def list_versions() -> list[dict]:
    """Version history of the trader's TDI system (v1 first, then newer)."""
    rows = []
    for cls in (TdiShiftV1, TdiShift):
        rows.append(
            {
                "id": cls.id,
                "name": cls.name,
                "version": cls.version,
                "note": cls.version_note,
                "frozen": cls.frozen,
                "supersedes": cls.supersedes,
                "default": cls.id == DEFAULT_STRATEGY,
            }
        )
    return rows


__all__ = [
    "ParamSpec",
    "RuleCheck",
    "Strategy",
    "Targets",
    "TriggerSet",
    "DEFAULT_STRATEGY",
    "resolve_id",
    "get_strategy",
    "get_strategy_class",
    "list_strategies",
    "list_versions",
    "_REGISTRY",
    "_ALIASES",
]
