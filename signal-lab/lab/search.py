"""Discretising features, and searching conjunctions of them for perfect rules."""
from __future__ import annotations

import numpy as np

NBINS = 8


def bin_edges(F: np.ndarray, mask: np.ndarray, nbins: int = NBINS) -> list[np.ndarray]:
    """Quantile edges per feature, computed on `mask` (the in-sample rows) only."""
    edges = []
    for j in range(F.shape[1]):
        x = F[mask, j]
        x = x[np.isfinite(x)]
        if len(x) < 1000:
            edges.append(np.array([]))
            continue
        q = np.quantile(x, np.linspace(0, 1, nbins + 1)[1:-1])
        edges.append(np.unique(q))
    return edges


def apply_bins(F: np.ndarray, edges: list[np.ndarray]) -> np.ndarray:
    """int8 bin per feature, -1 for a missing value."""
    B = np.full(F.shape, -1, dtype=np.int8)
    for j, e in enumerate(edges):
        if len(e) == 0:
            continue
        col = F[:, j]
        ok = np.isfinite(col)
        B[ok, j] = np.searchsorted(e, col[ok], side="right")
    return B


def cell_table(B: np.ndarray, ja: int, jb: int, win: np.ndarray, loss: np.ndarray,
               nbin: int = NBINS) -> tuple[np.ndarray, np.ndarray]:
    """win/loss counts for every pair of bins of features ja and jb.

    One bincount over the combined index — the whole pairwise search is
    O(rows) per feature pair, not O(rows * cells).
    """
    a, b = B[:, ja].astype(np.int32), B[:, jb].astype(np.int32)
    ok = (a >= 0) & (b >= 0)
    idx = a[ok] * nbin + b[ok]
    w = np.bincount(idx, weights=win[ok].astype(np.float64), minlength=nbin * nbin)
    l = np.bincount(idx, weights=loss[ok].astype(np.float64), minlength=nbin * nbin)
    return w.reshape(nbin, nbin), l.reshape(nbin, nbin)


def pairwise_scan(B: np.ndarray, win: np.ndarray, loss: np.ndarray, names: list[str],
                  min_n: int = 50, nbins: int = NBINS, rows_mask: np.ndarray | None = None):
    """Every pair of features x every pair of bins -> (wins, losses).

    Returns a dict with parallel arrays: feature i, bin i, feature j, bin j,
    wins, losses. `rows_mask` lets the caller scan a subset (the in-sample rows)
    while the caller keeps the full rows for later out-of-sample scoring.
    """
    n = B.shape[0]
    if rows_mask is None:
        rows_mask = np.ones(n, dtype=bool)
    w_rows = np.where(rows_mask, win, 0)
    l_rows = np.where(rows_mask, loss, 0)
    out = {k: [] for k in ("fi", "bi", "fj", "bj", "wins", "losses", "n")}
    F = B.shape[1]
    for ja in range(F):
        for jb in range(ja + 1, F):
            W, L = cell_table(B, ja, jb, w_rows, l_rows, nbins)
            tot = W + L
            sel = tot >= min_n
            if not sel.any():
                continue
            ai, bi = np.nonzero(sel)
            out["fi"].append(np.full(len(ai), ja))
            out["fj"].append(np.full(len(ai), jb))
            out["bi"].append(ai)
            out["bj"].append(bi)
            out["wins"].append(W[sel])
            out["losses"].append(L[sel])
            out["n"].append(tot[sel])
    cat = {k: (np.concatenate(v) if v else np.array([])) for k, v in out.items()}
    cat["winrate"] = np.where(cat["n"] > 0, cat["wins"] / np.maximum(cat["n"], 1), 0.0)
    cat["names"] = names
    return cat


def rule_mask(B: np.ndarray, conds: list[tuple[int, int]]) -> np.ndarray:
    """Rows where every (feature, bin) condition holds."""
    m = np.ones(B.shape[0], dtype=bool)
    for j, b in conds:
        m &= B[:, j] == b
    return m


def evaluate(B: np.ndarray, win: np.ndarray, loss: np.ndarray, conds: list[tuple[int, int]],
             rows: np.ndarray) -> dict:
    m = rule_mask(B, conds) & rows & ((win + loss) > 0)
    n = int(m.sum())
    w = int(win[m].sum())
    return {"n": n, "wins": w, "losses": n - w, "winrate": (w / n) if n else float("nan"),
            "conds": list(conds)}


# ---------------------------------------------------------------------------
# Deeper search: conjunctions of any length, grown greedily (beam search)
# ---------------------------------------------------------------------------
def beam_search(B: np.ndarray, win: np.ndarray, loss: np.ndarray, rows: np.ndarray,
                min_n: int = 50, depth: int = 4, width: int = 120,
                min_winrate: float = 0.0) -> list[dict]:
    """Grow rules by adding one (feature, bin) condition at a time.

    Beam = the `width` rules with the highest in-sample win rate (ties broken by
    more trades). A rule is a sorted int array of positions inside `rows`, which
    makes adding a condition an intersection of two sorted arrays - fast enough
    to explore hundreds of thousands of conjunctions.
    """
    universe = np.flatnonzero(rows)
    w = win[universe].astype(np.float64)
    l = loss[universe].astype(np.float64)
    nfeat = B.shape[1]
    # bins present per feature, with the positions (inside universe) of each
    bcols = []
    for j in range(nfeat):
        col = B[universe, j]
        d = {}
        for b in np.unique(col):
            if b < 0:
                continue
            d[int(b)] = np.flatnonzero(col == b)
        bcols.append(d)

    found: list[dict] = []

    def score(rule, conds):
        n = len(rule)
        if n < min_n:
            return None
        wins = w[rule].sum()
        wr = wins / n
        if wr < min_winrate:
            return None
        return {"n": n, "wins": wins, "losses": n - wins, "winrate": wr}

    # level 1
    beam = []
    for j in range(nfeat):
        for b, idxs in bcols[j].items():
            s = score(idxs, ((j, b),))
            if s:
                s["rule"] = idxs
                s["conds"] = ((j, b),)
                beam.append(s)
    beam.sort(key=lambda r: (-r["winrate"], -r["n"]))
    beam = beam[:width]
    found.extend(beam)

    for _ in range(depth - 1):
        cand = {}
        for r in beam:
            have = set(r["conds"])
            for j in range(nfeat):
                if any(j == c[0] for c in have):
                    continue
                for b, idxs in bcols[j].items():
                    inter = np.intersect1d(r["rule"], idxs, assume_unique=True)
                    if len(inter) < min_n:
                        continue
                    conds = tuple(sorted(have | {(j, b)}))
                    if conds in cand:
                        continue
                    s = score(inter, conds)
                    if s:
                        s["rule"] = inter
                        s["conds"] = conds
                        cand[conds] = s
        if not cand:
            break
        pool = sorted(cand.values(), key=lambda r: (-r["winrate"], -r["n"]))[:width]
        beam = pool
        found.extend(pool)
    found.sort(key=lambda r: (-r["winrate"], -r["n"]))
    return found
