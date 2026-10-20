"""Small shared helpers."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def sanitize(obj: Any) -> Any:
    """Recursively convert numpy/pandas scalars and NaN into strict JSON types.

    FastAPI's default encoder chokes on ``np.float64`` and emits ``NaN`` (invalid
    JSON), so every API payload passes through here.
    """
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, np.ndarray):
        return [sanitize(v) for v in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [sanitize(v) for v in obj]
    return obj
