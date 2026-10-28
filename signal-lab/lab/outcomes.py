"""Barrier outcomes: what happened after a bar, measured the conservative way.

A trade is opened at the **open of the bar after the signal bar** and runs until
one of two price barriers is touched:

    win  (target) : up  = entry + side * target_frac * atr
    loss (stop)   : dn  = entry - side * stop_frac   * atr

If a single bar touches both barriers the bar is counted as a **loss** — we
cannot know the intrabar order, so we assume the worst. If neither is touched
inside ``horizon`` bars the row is a time-out (0) and is excluded from the win
rate but reported: a rule is not allowed to hide its timeouts.
"""
from __future__ import annotations

import numpy as np


def atr_series(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    import pandas as pd

    pc = np.r_[np.nan, c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False, min_periods=n).mean().to_numpy()


def first_touch(h: np.ndarray, l: np.ndarray, idx: np.ndarray, up: np.ndarray,
                dn: np.ndarray, horizon: int) -> np.ndarray:
    """+1 if `up` is reached first, -1 if `dn` is (or both in one bar), 0 neither."""
    n = len(h)
    m = len(idx)
    out = np.zeros(m, dtype=np.int8)
    valid = (idx >= 0) & (idx < n) & np.isfinite(up) & np.isfinite(dn) & (up > dn)
    run_hi = np.full(m, -np.inf)
    run_lo = np.full(m, np.inf)
    alive = valid.copy()
    for k in range(horizon):
        j = idx + k
        inside = alive & (j < n)
        if not inside.any():
            break
        jj = np.clip(j, 0, n - 1)
        run_hi = np.where(inside, np.maximum(run_hi, h[jj]), run_hi)
        run_lo = np.where(inside, np.minimum(run_lo, l[jj]), run_lo)
        hit_up = inside & (run_hi >= up)
        hit_dn = inside & (run_lo <= dn)
        out[hit_dn & alive] = -1            # a bar that touches both is a loss
        out[hit_up & alive & ~hit_dn] = 1
        alive = inside & (out == 0)
    return out


def barrier(s, atr: np.ndarray, target_frac: float, stop_frac: float, horizon: int,
            side: str = "long") -> dict[str, np.ndarray]:
    """Outcome for every bar as a signal bar, entering at the next bar's open."""
    n = len(s)
    sig = np.arange(n - 1)
    ent = sig + 1
    entry = s.o[ent].astype(np.float64)
    a = atr[sig]
    if side == "long":
        out = first_touch(s.h, s.l, ent, entry + target_frac * a, entry - stop_frac * a, horizon)
    else:
        # mirror the market: for a short, the win barrier is below
        out = first_touch(-s.l, -s.h, ent, -(entry - target_frac * a), -(entry + stop_frac * a), horizon)
    return {"sig": sig, "entry_idx": ent, "entry": entry, "atr": a, "out": out}


def rr(target_frac: float, stop_frac: float) -> float:
    return target_frac / stop_frac


def pnl_r(out: np.ndarray, target_frac: float, stop_frac: float, fee_frac: float = 0.0,
          atr_frac_of_price: np.ndarray | None = None) -> np.ndarray:
    """Return the P&L in units of the stop distance (R), net of round-trip fees.

    ``fee_frac`` is a fraction of price (0.0005 = 5 bp round trip). Fees are
    converted to R using the stop distance in price terms.
    """
    win = target_frac / stop_frac
    r = np.where(out == 1, win, np.where(out == -1, -1.0, 0.0))
    if fee_frac and atr_frac_of_price is not None:
        r = r - fee_frac / (stop_frac * atr_frac_of_price + 1e-12)
    return r
