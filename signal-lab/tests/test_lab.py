"""Tests for the lab and the app.

The three that matter most:

* ``test_features_do_not_look_ahead`` - every feature at bar i must be identical
  whether or not the bars after i exist. This is the only defence against a
  backtest that quietly reads the future.
* ``test_a_bar_that_touches_both_barriers_is_a_loss`` - the conservative rule.
* ``test_a_perfect_rule_can_still_lose_money`` - the arithmetic behind the whole
  "100 % accuracy" question, asserted rather than asserted-at.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "app")):
    if p not in sys.path:
        sys.path.insert(0, p)

from lab import data, features, outcomes, search  # noqa: E402


# --------------------------------------------------------------------- helpers
def random_walk(n=900, seed=0, start=100.0, vol=1.0):
    rng = np.random.default_rng(seed)
    c = start + np.cumsum(rng.normal(0, vol, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + np.abs(rng.normal(0, vol * 0.4, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, vol * 0.4, n))
    v = np.abs(rng.normal(10, 2, n))
    t = np.arange(n, dtype=np.int64) * 60 + 1_600_000_000 // 60
    trades = np.abs(rng.normal(100, 10, n))   # must vary, or its z-score is 0/0
    tb = v * rng.uniform(0.35, 0.65, n)       # same for the taker-buy ratio
    return data.Series("TEST", "1h", t, o, h, l, c, v, v * c, trades, tb)


# -------------------------------------------------------------------- features
def test_features_do_not_look_ahead():
    s = random_walk(900)
    full = features.features(s)
    cut = 700
    part = features.features(s.slice(0, cut))
    for name, arr in full.items():
        a, b = arr[cut - 1], part[name][-1]
        if np.isnan(a) and np.isnan(b):
            continue
        assert np.isfinite(a) and np.isfinite(b), f"{name} went non-finite at the cut"
        assert abs(a - b) < 1e-4, f"{name} changed when future bars were removed: {a} vs {b}"


def test_every_feature_is_populated_on_a_normal_series():
    f = features.features(random_walk(900))
    assert len(f) > 50
    for name, arr in f.items():
        assert np.isfinite(arr[-1]), f"{name} is not finite on the last bar"


# -------------------------------------------------------------------- outcomes
def test_target_first_is_a_win_and_stop_first_is_a_loss():
    h = np.array([10, 11, 10, 10, 10], dtype=float)
    l = np.array([9, 9, 8, 9, 9], dtype=float)
    idx = np.array([1, 0])
    up = np.array([10.5, 99.0])
    dn = np.array([8.5, 9.5])
    out = outcomes.first_touch(h, l, idx, up, dn, horizon=3)
    assert out[0] == 1, "target touched at 11 before the stop at 8.5"
    assert out[1] == -1, "the stop is touched on the first bar"


def test_a_bar_that_touches_both_barriers_is_a_loss():
    h = np.array([10, 12, 10], dtype=float)
    l = np.array([9, 8, 9], dtype=float)
    out = outcomes.first_touch(h, l, np.array([1]), np.array([11.0]), np.array([9.0]), horizon=2)
    assert out[0] == -1, "we cannot know the intrabar order, so it counts against us"


def test_neither_barrier_inside_the_horizon_times_out():
    h = np.array([10, 10.1, 10.1, 10.1], dtype=float)
    l = np.array([9.9, 9.9, 9.9, 9.9], dtype=float)
    out = outcomes.first_touch(h, l, np.array([0]), np.array([20.0]), np.array([1.0]), horizon=3)
    assert out[0] == 0


def test_the_entry_is_the_next_open_not_the_signal_bar_close():
    s = random_walk(300)
    s.o[10] = 500.0  # a bar with a wild open
    atr = outcomes.atr_series(s.h, s.l, s.c)
    res = outcomes.barrier(s, atr, 1.0, 1.0, 20, "long")
    i = int(np.flatnonzero(res["sig"] == 9)[0])
    assert res["entry"][i] == pytest.approx(500.0)


# ---------------------------------------------------------------------- search
def test_cell_table_matches_a_brute_force_count():
    rng = np.random.default_rng(1)
    B = rng.integers(0, 4, size=(500, 3)).astype(np.int8)
    win = rng.random(500) < 0.6
    loss = ~win
    W, L = search.cell_table(B, 0, 1, win, loss, nbin=4)
    for a in range(4):
        for b in range(4):
            m = (B[:, 0] == a) & (B[:, 1] == b)
            assert W[a, b] == win[m].sum()
            assert L[a, b] == loss[m].sum()


def test_the_beam_finds_a_rule_that_never_loses():
    """Plant a feature that perfectly marks the winners - the search must find it."""
    n = 4000
    rng = np.random.default_rng(2)
    F = rng.normal(size=(n, 5)).astype(np.float32)
    win = rng.random(n) < 0.25   # a quarter, so a quantile bin can isolate them
    # plant the answer in one feature: winners sit far above the cut
    F[:, 2] = np.where(win, 5.0, 0.0) + rng.normal(0, 1e-6, n)
    loss = ~win
    edges = search.bin_edges(F, np.ones(n, dtype=bool))
    B = search.apply_bins(F, edges)
    rules = search.beam_search(B, win, loss, np.ones(n, dtype=bool), min_n=20, depth=3, width=60)
    perfect = [r for r in rules if r["winrate"] == 1.0]
    assert perfect, "the planted rule should be found"
    assert max(r["n"] for r in perfect) >= 20


def test_bins_are_fitted_on_the_rows_they_are_given():
    F = np.arange(4000, dtype=np.float32).reshape(-1, 1)
    is_mask = np.zeros(4000, dtype=bool)
    is_mask[:1000] = True
    edges = search.bin_edges(F, is_mask, nbins=4)
    assert edges[0][-1] < 1000, "edges must come from the in-sample block, not the whole array"


# ---------------------------------------------------------------------- engine
def test_the_engine_fills_at_the_next_open_and_scores_conservatively(tmp_path):
    from engine import Engine, Stream

    class Always:
        def predict_proba(self, x):
            return np.array([[0.1, 0.95]])

    e = Engine(mode="replay", speed=1, cut=0.8, tfs=["1h"],
               db=str(tmp_path / "t.db"))
    s = random_walk(900)
    e.streams = {("TEST", "1h"): Stream("TEST", "1h", s)}
    e.streams[("TEST", "1h")].i = 799
    e.models = {"1h": Always()}
    e.memory = []
    for _ in range(3):
        e.step()
    st = e.streams[("TEST", "1h")]
    sigs = e.memory
    assert sigs, "a bar with p=0.95 above the cut must produce a signal"
    sig = sigs[0]
    assert sig["entry"] is not None and sig["status"] in ("open", "won", "lost", "expired")
    # signed at the close of bar 800 -> filled at the open of bar 801
    assert sig["entry"] == pytest.approx(float(s.o[801])), "filled at the next open"
    atr = sig["atr"]
    assert sig["stop"] == pytest.approx(sig["entry"] - e.geometry["stop"] * atr)
    assert sig["target"] == pytest.approx(sig["entry"] + e.geometry["target"] * atr)
    # and it is on disk, not just in memory
    saved = e._db.execute("SELECT count(*) FROM signals").fetchone()[0]
    assert saved == len(e.memory)


def test_a_restart_does_not_double_count_the_same_trade(tmp_path):
    from engine import Engine, Stream

    class Always:
        def predict_proba(self, x):
            return np.array([[0.1, 0.95]])

    db = str(tmp_path / "t.db")
    s = random_walk(900)
    for run in range(2):
        e = Engine(mode="replay", speed=1, cut=0.8, tfs=["1h"], db=db)
        e.streams = {("TEST", "1h"): Stream("TEST", "1h", s)}
        e.streams[("TEST", "1h")].i = 799
        e.models = {"1h": Always()}
        for _ in range(5):
            e.step()
        keys = [m["key"] for m in e.memory]
        assert len(keys) == len(set(keys))
        if run == 0:
            first = len(keys)


# ------------------------------------------------------------------ the thesis
def test_a_perfect_rule_can_still_lose_money():
    """The number the whole app hangs on, recomputed from the shipped files."""
    hundred = json.load(open(os.path.join(ROOT, "app", "data", "hundred.json")))
    k = hundred["rr"]
    fee_r = hundred["fee_r"]
    assert k - fee_r < 0, "even a 100 % hit rate must be negative at this geometry"
    assert fee_r > k, "because the fee is larger than the prize"
    perfect_ev = 1.0 * k - 0.0 * 1.0 - fee_r
    assert perfect_ev == pytest.approx(k - fee_r)


def test_the_shipped_bands_are_monotone_and_honest():
    bands = json.load(open(os.path.join(ROOT, "app", "data", "bands.json")))["bands"]
    bands = sorted([b for b in bands if b["cut"] > 0], key=lambda b: -b["cut"])
    wins = [b["win"] for b in bands]
    # the bands are measured with independent non-overlapping selections, so they
    # need not be perfectly ordered - but they must not be out of order by much
    assert wins[0] >= wins[-1] + 0.04, "the strictest band must beat taking everything"
    for a, b in zip(wins, wins[1:]):
        assert a >= b - 0.02, f"band accuracy jumped upwards ({a:.3f} -> {b:.3f})"
    for b in bands:
        assert 0.55 <= b["win"] <= 0.85, "no band may claim the impossible"
        assert b["ci"][0] <= b["win"] <= b["ci"][1]
        assert b["n"] > 100, "every shipped band has a sample worth quoting"


def test_the_frontier_says_what_the_page_says():
    rows = json.load(open(os.path.join(ROOT, "app", "data", "frontier.json")))
    high = [r for r in rows if r["win"] >= 0.98]
    assert len(high) >= 3, "the frontier must actually contain high-accuracy rows"
    assert all(r["ev_post"] < 0 for r in high), "and all of them must lose money after fees"
    assert any(r["ev_post"] > 0 for r in rows), "while some lower-accuracy row makes money"


def test_a_replay_is_scored_by_the_model_that_never_saw_the_replay(tmp_path):
    """The trap this app fell into once: replaying 2024 with a model trained on 2024.

    The first run of the replay showed 96.9 % accuracy. It was scoring bars the
    shipped model had been fitted on. Replay now uses the validation model
    (trained up to 2023-12-31); live uses the production one.
    """
    from engine import Engine

    e = Engine(mode="replay", speed=1, cut=0.8, tfs=["1h"], db=str(tmp_path / "t.db"))
    assert "validation" in e.model_label
    assert "2023" in e.model_label
    live = Engine(mode="live", speed=1, cut=0.8, tfs=["1h"], db=str(tmp_path / "t2.db"))
    assert "production" in live.model_label
