"""Causal features.

No feature at bar *i* may use any bar after *i*. Everything below is a rolling
or expanding statistic of the past, computed on the closed bar; the trade is
entered at the open of bar *i+1*. That is the only discipline that keeps a
backtest honest, and it is enforced by ``tests/test_lab.py``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .data import Series


def _atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    pc = np.r_[np.nan, c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False, min_periods=n).mean().to_numpy()


def _rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.r_[np.nan, np.diff(c)]
    up = pd.Series(np.where(d > 0, d, 0.0))
    dn = pd.Series(np.where(d < 0, -d, 0.0))
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return (100 - 100 / (1 + au / ad.replace(0, np.nan))).to_numpy()


def _roll(x: np.ndarray, n: int, fn: str) -> np.ndarray:
    s = pd.Series(x)
    r = getattr(s.rolling(n, min_periods=max(2, n // 2)), fn)()
    return r.to_numpy()


def features(s: Series) -> dict[str, np.ndarray]:
    """Return name -> float32 array, same length as the series."""
    o, h, l, c = s.o.astype(np.float64), s.h.astype(np.float64), s.l.astype(np.float64), s.c.astype(np.float64)
    v, qv, nt, tb = s.v.astype(np.float64), s.qv.astype(np.float64), s.n.astype(np.float64), s.tb.astype(np.float64)
    eps = 1e-12
    logc = np.log(np.maximum(c, eps))
    atr = _atr(h, l, c, 14)
    atr_s = np.where(atr > 0, atr, np.nan)
    f: dict[str, np.ndarray] = {}

    def put(name: str, x) -> None:
        f[name] = np.asarray(x, dtype=np.float32)

    # --- momentum / mean reversion -------------------------------------------------
    for k in (1, 2, 3, 5, 10, 20, 60, 240):
        if k < len(c):
            put(f"ret{k}", np.r_[np.full(k, np.nan), logc[k:] - logc[:-k]] * 1e4)  # basis points
    r1 = np.r_[np.nan, np.diff(logc)]
    sd20 = _roll(r1, 20, "std")
    put("zret1", r1 / np.where(sd20 > 0, sd20, np.nan))
    put("zret5", (logc - np.r_[np.full(5, np.nan), logc[:-5]]) / np.where(sd20 * np.sqrt(5) > 0, sd20 * np.sqrt(5), np.nan))
    put("rsi14", _rsi(c, 14))
    put("rsi2", _rsi(c, 2))
    ma20 = _roll(c, 20, "mean")
    ma50 = _roll(c, 50, "mean")
    ma200 = _roll(c, 200, "mean")
    put("dist_ma20", (c - ma20) / atr_s)
    put("dist_ma50", (c - ma50) / atr_s)
    put("dist_ma200", (c - ma200) / atr_s)
    put("ma20_slope", (ma20 - np.r_[np.full(5, np.nan), ma20[:-5]]) / atr_s)

    # --- position inside recent ranges --------------------------------------------
    for n in (20, 100, 500):
        hi, lo = _roll(h, n, "max"), _roll(l, n, "min")
        width = np.where((hi - lo) > 0, hi - lo, np.nan)
        put(f"pos{n}", (c - lo) / width)
        put(f"to_hi{n}", (hi - c) / atr_s)
        put(f"to_lo{n}", (c - lo) / atr_s)
    put("range20_atr", (_roll(h, 20, "max") - _roll(l, 20, "min")) / atr_s)

    # --- volatility regime ---------------------------------------------------------
    a5, a20, a100 = _atr(h, l, c, 5), atr, _atr(h, l, c, 100)
    put("atr5_20", a5 / np.where(atr_s > 0, atr_s, np.nan))
    put("atr20_100", a20 / np.where(a100 > 0, a100, np.nan))
    put("atr_pct500", pd.Series(atr).rolling(500, min_periods=100).rank(pct=True).to_numpy())
    put("range_atr", (h - l) / atr_s)
    put("body_atr", (c - o) / atr_s)
    put("wick_up_atr", (h - np.maximum(o, c)) / atr_s)
    put("wick_dn_atr", (np.minimum(o, c) - l) / atr_s)
    rng = np.where((h - l) > 0, h - l, np.nan)
    put("clv", ((c - l) - (h - c)) / rng)  # close location value, -1..1
    put("body_ratio", (c - o) / rng)
    put("clv3", _roll(f["clv"].astype(np.float64), 3, "mean"))
    put("gap_atr", (o - np.r_[np.nan, c[:-1]]) / atr_s)

    # --- streaks ------------------------------------------------------------------
    up = np.r_[np.nan, (np.diff(c) > 0).astype(np.float64)]
    dn = np.r_[np.nan, (np.diff(c) < 0).astype(np.float64)]
    # length of the current run of same-direction closes
    sgn = np.sign(np.r_[np.nan, np.diff(c)])
    run = np.zeros(len(c))
    for i in range(1, len(c)):
        run[i] = run[i - 1] + 1 if sgn[i] == sgn[i - 1] and sgn[i] != 0 else (1 if sgn[i] != 0 else 0)
    put("streak", run * sgn)
    put("up_frac20", _roll(up, 20, "mean"))

    # --- volume / microstructure ---------------------------------------------------
    put("vol_z20", (v - _roll(v, 20, "mean")) / np.where(_roll(v, 20, "std") > 0, _roll(v, 20, "std"), np.nan))
    put("vol_r5_20", _roll(v, 5, "mean") / np.where(_roll(v, 20, "mean") > 0, _roll(v, 20, "mean"), np.nan))
    put("trades_z20", (nt - _roll(nt, 20, "mean")) / np.where(_roll(nt, 20, "std") > 0, _roll(nt, 20, "std"), np.nan))
    size = qv / np.maximum(nt, 1)
    put("avg_size_z20", (size - _roll(size, 20, "mean")) / np.where(_roll(size, 20, "std") > 0, _roll(size, 20, "std"), np.nan))
    tbr = tb / np.maximum(v, eps)
    put("taker_buy", tbr)
    put("taker_buy_z20", (tbr - _roll(tbr, 20, "mean")) / np.where(_roll(tbr, 20, "std") > 0, _roll(tbr, 20, "std"), np.nan))
    put("taker_buy_r5_20", _roll(tbr, 5, "mean") / np.where(_roll(tbr, 20, "mean") > 0, _roll(tbr, 20, "mean"), np.nan))
    dvol = np.where(c > np.r_[np.nan, c[:-1]], v, -v)
    put("cvd_z20", (pd.Series(dvol).rolling(20, min_periods=10).sum().to_numpy()) /
        np.where(_roll(v, 20, "sum") > 0, _roll(v, 20, "sum"), np.nan))
    put("quote_z20", (qv - _roll(qv, 20, "mean")) / np.where(_roll(qv, 20, "std") > 0, _roll(qv, 20, "std"), np.nan))

    # --- clock --------------------------------------------------------------------
    mod = (s.t % 1440).astype(np.float64)
    put("hour", (mod // 60).astype(np.float64))
    put("hour_sin", np.sin(2 * np.pi * mod / 1440))
    put("hour_cos", np.cos(2 * np.pi * mod / 1440))
    put("dow", ((s.t // 1440 + 3) % 7).astype(np.float64))
    put("weekend", (((s.t // 1440 + 3) % 7) >= 5).astype(np.float64))

    # --- bars since the extreme ----------------------------------------------------
    hi20, lo20 = _roll(h, 20, "max"), _roll(l, 20, "min")
    put("since_hi20", _bars_since(h >= hi20))
    put("since_lo20", _bars_since(l <= lo20))

    # --- context: what the previous bar did (1- and 2-bar patterns) ----------------
    for lag in (1, 2):
        put(f"body_atr_l{lag}", np.r_[np.full(lag, np.nan), f["body_atr"][:-lag]])
        put(f"clv_l{lag}", np.r_[np.full(lag, np.nan), f["clv"][:-lag]])

    return f


def _bars_since(flag: np.ndarray) -> np.ndarray:
    """Bars since the flag was last true (capped at 500)."""
    idx = np.arange(len(flag))
    last = np.where(flag, idx, -1)
    last = np.maximum.accumulate(last)
    out = (idx - last).astype(np.float32)
    out[last < 0] = np.nan
    return np.minimum(out, 500)


def feature_matrix(f: dict[str, np.ndarray], names: list[str] | None = None) -> tuple[np.ndarray, list[str]]:
    names = names or sorted(f)
    m = np.empty((len(f[names[0]]), len(names)), dtype=np.float32)
    for j, n in enumerate(names):
        m[:, j] = f[n]
    return m, names


def feature_segments(s: Series, seg: int = 250_000, pad: int = 700,
                     keep_mask: np.ndarray | None = None):
    """Yield (start_index, {name: values}) for the rows of `keep_mask`, in chunks.

    A 1-minute archive is 4.8 M bars: building all 59 features for all of them at
    once is ~1.1 GB per symbol and takes the machine down. Rolling windows never
    look further back than 500 bars, so the series can be cut into overlapping
    segments and only the rows the caller needs are kept.
    """
    n = len(s)
    start = 0
    while start < n:
        a = max(0, start - pad)
        b = min(n, start + seg)
        sub = s.slice(a, b)
        f = features(sub)
        if keep_mask is None:
            lo = start - a
            yield start, {k: v[lo:lo + (b - start)] for k, v in f.items()}
        else:
            local = np.flatnonzero(keep_mask[start:b])
            if len(local):
                idx = local + (start - a)
                yield start + int(local[0]), {k: v[idx] for k, v in f.items()}
        start = b


def live_safe(names=None) -> list[str]:
    """The subset of features that can be computed from exchange candles alone.

    MEXC's kline endpoint returns time/open/close/high/low/volume and nothing else:
    no trade count, no taker-buy split. Those two columns are therefore constant in
    live mode, and every feature built on them (their z-scores, the taker-buy ratio,
    the CVD) comes out NaN. The engine drops any bar with a NaN feature, so a model
    trained on all 59 features can never emit a live signal - it silently waits
    forever. This function is what `tools/train_scanner.py` uses to pick the feature
    list, so training and live scoring see exactly the same inputs.

    It is measured, not hand-listed: build a series that looks like an exchange
    candle feed (n = 1, taker-buy = half the volume) and keep the features that
    come out finite.
    """
    rng = np.random.default_rng(0)
    n = 900
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + np.abs(rng.normal(0, 1, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, 1, n))
    v = np.abs(rng.normal(10, 2, n))
    s = Series(symbol="LIVE", tf="1h", t=np.arange(n, dtype=np.int64) * 60,
               o=o, h=h, l=l, c=c, v=v, qv=v * c, n=np.ones(n), tb=v * 0.5)
    f = features(s)
    keep = [k for k, arr in f.items() if np.isfinite(arr[-1])]
    if names is not None:
        keep = [k for k in keep if k in set(names)]
    return sorted(keep)
