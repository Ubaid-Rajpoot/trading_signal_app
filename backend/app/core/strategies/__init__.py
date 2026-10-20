"""Strategy registry.

Add your strategy module to ``_REGISTRY`` to make it available everywhere.
"""

from __future__ import annotations

from .base import ParamSpec, RuleCheck, Strategy, Targets, TriggerSet
from .custom import CustomStrategy
from .donchian import DonchianBreakout
from .ema_trend import EmaTrend

_REGISTRY: dict[str, type[Strategy]] = {
    DonchianBreakout.id: DonchianBreakout,
    EmaTrend.id: EmaTrend,
    CustomStrategy.id: CustomStrategy,
}

DEFAULT_STRATEGY = DonchianBreakout.id


def get_strategy(strategy_id: str | None) -> Strategy:
    key = strategy_id or DEFAULT_STRATEGY
    cls = _REGISTRY.get(key)
    if cls is None:
        raise KeyError(f"Unknown strategy '{key}'")
    return cls()


def get_strategy_class(strategy_id: str | None) -> type[Strategy]:
    key = strategy_id or DEFAULT_STRATEGY
    cls = _REGISTRY.get(key)
    if cls is None:
        raise KeyError(f"Unknown strategy '{key}'")
    return cls


def list_strategies() -> list[dict]:
    return [cls.schema() for cls in _REGISTRY.values()]


__all__ = [
    "ParamSpec",
    "RuleCheck",
    "Strategy",
    "Targets",
    "TriggerSet",
    "DEFAULT_STRATEGY",
    "get_strategy",
    "get_strategy_class",
    "list_strategies",
    "_REGISTRY",
]
