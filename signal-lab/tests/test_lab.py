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


def test_the_shipped_bands_are_honest_about_what_they_need_to_beat():
    """A 1:4 band wins ~1 time in 4, so the number that matters is break-even.

    Two things this deliberately does NOT assert:

    * monotonicity across cuts. With the pooled model the strictest band (0.50) is
      *less* accurate than the loosest (0.25) — 22.2 % against 23.8 % — because at
      these sample sizes the ordering is noise. The page shows every band with its
      interval instead of pretending otherwise.
    * a remembered percentage. Round 14's numbers were ~75 %; round 15's are ~24 %,
      and both were correct for their geometry.
    """
    pack = json.load(open(os.path.join(ROOT, "app", "data", "bands.json")))
    rr, stop = pack["rr"], pack["stop"]
    bands = sorted([b for b in pack["bands"] if b["cut"] > 0], key=lambda b: -b["cut"])
    assert rr >= 3.5, "the shipped geometry must be at least 1:4 reward:risk"
    for b in bands:
        assert 0.05 <= b["win"] <= 0.60, "a 1:4 band cannot claim more than 60 %"
        assert b["ci"][0] <= b["win"] <= b["ci"][1]
        assert b["n"] >= 40, "a band with fewer trades than that is not shown at all"
    # the cut is not a precision instrument: the whole family lands in a narrow band
    spread = max(b["win"] for b in bands) - min(b["win"] for b in bands)
    assert spread < 0.08, f"the cuts disagree by {100*spread:.1f} points - more than noise"

    rec = [b for b in bands if abs(b["cut"] - pack["recommended_cut"]) < 1e-9][0]
    assert rec["n"] >= 250, "the recorded band needs a sample worth quoting"
    assert rec["ev_post"] > 0, "and must be profitable after fees"
    assert rec["win"] > (1 + 0.03) / (1 + rr), "and clear its own break-even rate"
    for period, cell in rec["folds"].items():
        assert cell["win"] > (1 + 0.03) / (1 + rr), f"{period} fell below break-even"


def test_the_reward_risk_sweep_supports_the_shipped_choice():
    """The geometry the app trades is the only one that passed the worst-fold test."""
    rr = json.load(open(os.path.join(ROOT, "app", "data", "rr.json")))
    chosen = rr["chosen"]
    assert chosen["rr"] >= 3.5, "the app trades at least 1:4"
    survivors = [s for s in rr["survivors"] if s["n"] >= 100]
    assert survivors, "at least one geometry must survive every fold with a real sample"
    best = survivors[0]
    assert best["tf"] == chosen["tf"] and abs(best["rr"] - chosen["rr"]) < 1e-9
    assert best["worst"] > 0, "and its worst fold must still make money"
    assert best["win"] > best["be"], "because it wins more often than it needs to"
    # the point of the whole round: more reward per unit of risk, less fee drag
    for row in rr["rows"]:
        if row["rr"] == chosen["rr"] and row["stop"] == chosen["stop"] and row["tf"] == chosen["tf"]:
            assert row["fee_r"] < 0.05, "at a 2 ATR stop the fee is a small share of the reward"


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


def test_the_fee_is_expressed_in_r_and_matches_the_research_figure(tmp_path):
    """fee_r must be ~0.03 R at a 2 ATR stop, not 3e-7.

    The first build divided a *fraction of price* by a price distance, giving a
    number 100,000 times too small — the page then reported "fee 0.000 R" and the
    break-even line looked like a free lunch.
    """
    from engine import Engine, Stream

    class Always:
        def predict_proba(self, x):
            return np.array([[0.1, 0.95]])

    e = Engine(mode="replay", speed=1, tfs=["1h"], cut=0.5, db=str(tmp_path / "t.db"))
    e.streams = {("TEST", "1h"): Stream("TEST", "1h", random_walk(900))}
    e.streams[("TEST", "1h")].i = 799
    e.models = {"1h": Always()}
    e.memory = []
    for _ in range(3):
        e.step()
    sig = e.memory[0]
    assert 0.001 < sig["fee_r"] < 0.5, f"fee in R is off: {sig['fee_r']}"
    assert sig["break_even"] == pytest.approx((1 + sig["fee_r"]) / (1 + e.geometry["rr"]))
    assert sig["break_even"] < 0.30, "a 1:4 trade needs well under 30 % accuracy"


def test_a_second_signal_waits_out_the_horizon(tmp_path):
    """The app must trade what was measured: one entry per horizon, not one per exit.

    The measured bands block a new signal for `horizon` bars after each one. The
    engine originally unblocked on exit, which produced ~40 % more trades than the
    study, drawn from the same move - and the live accuracy fell to break-even.
    """
    from engine import Engine, Stream

    class Always:
        def predict_proba(self, x):
            return np.array([[0.1, 0.95]])

    e = Engine(mode="replay", speed=1, tfs=["1h"], cut=0.5, db=str(tmp_path / "t.db"))
    e.streams = {("TEST", "1h"): Stream("TEST", "1h", random_walk(900))}
    e.streams[("TEST", "1h")].i = 799
    e.models = {"1h": Always()}
    e.memory = []
    for _ in range(3):
        e.step()
    first = len(e.memory)
    assert first == 1
    st = e.streams[("TEST", "1h")]
    assert st.next_ok_bar == 800 + e.horizon
    for _ in range(min(20, e.horizon - 2)):
        e.step()
    assert len(e.memory) == 1, "no second signal inside the horizon, even if the trade closed"


def archive_available() -> bool:
    d = os.path.join(ROOT, "data")
    return os.path.exists(os.path.join(d, "BTCUSDT_1m.npy"))


def test_rewinding_a_replay_starts_clean(tmp_path):
    if not archive_available():
        pytest.skip("the 9.6 M bar archive is not built (run tools/build_dataset.py)")
    from engine import Engine

    e = Engine(mode="replay", speed=1, tfs=["1h"], db=str(tmp_path / "t.db"))
    e.prepare()
    for _ in range(30):
        e.step()
    n_before = e.steps
    e.rewind()
    assert e.steps == 0 and e.memory == [] and not e.finished
    assert n_before > 0
    assert e._db.execute("SELECT count(*) FROM signals").fetchone()[0] == 0


# ---------------------------------------------------------------- live mode
def _live_engine(tmp_path, symbols=("BTC_USDT",), tfs=("1h",)):
    from engine import Engine

    e = Engine(mode="live", speed=1, tfs=list(tfs), symbols=list(symbols),
               db=str(tmp_path / "live.db"))
    e.client = None
    return e


def test_mexc_names():
    from engine import mexc_symbol

    assert mexc_symbol("BTCUSDT") == "BTC_USDT"
    assert mexc_symbol("1000PEPEUSDT") == "1000PEPE_USDT"
    assert mexc_symbol("BTC_USDT") == "BTC_USDT"
    assert mexc_symbol("TEST") == "TEST"


def test_live_walks_every_bar_that_closed_between_polls(tmp_path, monkeypatch):
    """A stop touched in a skipped bar must still be seen.

    The first live loop scored only the newest closed bar every 45 s, so anything
    that happened in a bar which opened and closed between two polls was invisible,
    and the holding period counted polls instead of bars.
    """
    import feed
    from lab import data as labdata

    s = random_walk(400)
    rows = [[float(t) * 60, float(o), float(c), float(h), float(l), 1.0]
            for t, o, c, h, l in zip(s.t, s.o, s.c, s.h, s.l)]
    monkeypatch.setattr(feed, "klines", lambda *a, **k: rows)

    e = _live_engine(tmp_path)
    e.streams = {("BTC_USDT", "1h"): __import__("engine").Stream("BTC_USDT", "1h",
                                                               labdata.Series(
        "BTC_USDT", "1h", s.t[:300], s.o[:300], s.h[:300], s.l[:300], s.c[:300],
        s.v[:300], s.qv[:300], s.n[:300], s.tb[:300]))}
    st = e.streams[("BTC_USDT", "1h")]
    st.i, st.last_t = 298, int(s.t[298])
    e.tick_live()
    assert st.last_t == int(s.t[398]), "the stream caught up to the newest closed bar"
    assert st.i == 398


def test_a_trade_still_open_across_a_restart_is_adopted_and_resolved(tmp_path, monkeypatch):
    """Signals must survive a restart - the round-12 requirement, in live mode."""
    import feed
    from lab import data as labdata

    s = random_walk(600)
    rows = [[float(t) * 60, float(o), float(c), float(h), float(l), 1.0]
            for t, o, c, h, l in zip(s.t, s.o, s.c, s.h, s.l)]
    monkeypatch.setattr(feed, "klines", lambda *a, **k: rows)

    e = _live_engine(tmp_path)
    e.streams = {("BTC_USDT", "1h"): __import__("engine").Stream("BTC_USDT", "1h",
                                                                labdata.Series("BTC_USDT", "1h", s.t, s.o, s.h, s.l, s.c, s.v, s.qv, s.n, s.tb))}
    entry_i, entry = 150, float(s.o[150])
    target, stop = entry + 5.0, entry - 5.0
    key = "BTC_USDT:1h:test"
    e._db.execute("INSERT INTO signals(key, symbol, tf, side, band, prob, signal_ms, entry_ms,"
                  " entry, stop, target, atr, status, bars_held, pnl_r, exit_ms, mode)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (key, "BTC_USDT", "1h", "long", "C", 0.4, int(s.t[149]) * 60000,
                   int(s.t[150]) * 60000, entry, stop, target, 2.5, "open", 0, None, None, "live"))
    e._db.commit()
    e.memory = [dict(key=key, symbol="BTC_USDT", tf="1h", side="long", band="C", prob=0.4,
                     signal_ms=int(s.t[149]) * 60000, entry_ms=int(s.t[150]) * 60000,
                     entry=entry, stop=stop, target=target, atr=2.5, status="open",
                     bars_held=0, pnl_r=None, exit_ms=None)]
    e._adopt_live()

    row = [m for m in e.memory if m["key"] == key][0]
    assert row["status"] in ("won", "lost", "expired", "open")
    st = e.streams[("BTC_USDT", "1h")]
    if row["status"] == "open":
        assert any(x.key == key for x in st.open), "an unresolved trade is back under supervision"
    else:
        assert st.open == [] or all(x.key != key for x in st.open)


# ------------------------------------------------- round 16: config & the scanner
def test_config_json_is_the_source_of_truth(tmp_path):
    """A long symbol list and every setting live in a file, not in argv."""
    import config as appconfig

    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "_comment": ["kept as-is"],
        "mode": "live", "port": 8811, "timeframes": ["1m", "1h", "bogus"],
        "symbols": ["btcusdt", "BTCUSDT", "ethusdt"], "auto_top": 50,
        "poll_seconds": "45", "workers": 6,
    }))
    cfg = appconfig.load(str(p))
    assert cfg.poll_seconds == 45.0 and cfg.workers == 6
    assert cfg.symbols_final == ["BTCUSDT", "ETHUSDT"], "names are normalised and de-duplicated"
    assert cfg.timeframes == ["1m", "1h"], "an unknown timeframe is dropped, not passed on"
    cfg.poll_seconds = 30
    appconfig.save(cfg, str(p))
    again = json.loads(p.read_text())
    assert again["_comment"] == ["kept as-is"], "the hand-written comment survives a save"
    assert again["poll_seconds"] == 30
    assert appconfig.load(str(p)).poll_seconds == 30


def test_timeframes_mexc_does_not_serve_are_aggregated():
    """3m/10m/45m/2h/3h are built from the nearest native interval."""
    from engine import NATIVE_FOR, build_series

    assert NATIVE_FOR["3m"] == "1m" and NATIVE_FOR["45m"] == "15m" and NATIVE_FOR["2h"] == "1h"
    # six 5-minute candles -> two 15-minute candles
    rows = []
    for i in range(6):
        o, c = 100.0 + i, 100.5 + i
        rows.append([i * 300, o, c, c + 1, o - 1, 10.0])
    s = build_series("X", "5m", "15m", rows)
    assert len(s) == 2 and s.tf == "15m"
    assert s.o[0] == 100.0 and s.c[0] == 102.5      # first three candles
    assert s.h[0] == 103.5 and s.l[0] == 99.0
    assert s.o[1] == 103.0 and s.c[1] == 105.5
    assert s.v[0] == 30.0, "volume is summed across the aggregated candles"


def _fake_exchange(monkeypatch, symbols, minutes=1200, seed=7):
    """Serve synthetic candles for every native interval without a network."""
    import feed

    def rows_for(symbol, tf, bars):
        step = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "45m": 45,
                "1h": 60, "2h": 120, "3h": 180, "4h": 240}[tf]
        # 900 bars: enough history for the 500-bar feature windows, so a scored
        # bar is a real row and not a NaN
        n = max(900, min(bars, 900))
        s = random_walk(n, seed=seed + sum(map(ord, symbol)) % 97)
        return [[float(t) * 60 * step, float(o), float(c), float(h), float(l), float(v)]
                for t, o, c, h, l, v in zip(s.t, s.o, s.c, s.h, s.l, s.v)]

    monkeypatch.setattr(feed, "klines", lambda client, sym, tf, bars=400: rows_for(sym, tf, bars))
    monkeypatch.setattr(feed, "symbols", lambda client, top=60: list(symbols))


def test_many_symbols_and_timeframes_are_scanned_in_parallel_without_hanging(tmp_path, monkeypatch):
    """The round-16 complaint: a long universe must scan, not hang.

    Eight symbols x four timeframes, one of which the exchange does not serve
    natively (10m from 5m, 3h from 1h). The poll must finish, build a stream for
    every pair, and record what it found - including on a single fast poll.
    """
    import time as _time
    from engine import Engine

    symbols = [f"COIN{i}_USDT" for i in range(8)]
    _fake_exchange(monkeypatch, symbols)
    cfg = _cfg(tmp_path, symbols=symbols, tfs=["1m", "5m", "10m", "1h", "3h"], scan_bars=20)
    e = Engine(mode="live", tfs=cfg.timeframes, symbols=symbols, config=cfg,
               db=str(tmp_path / "scan.db"))
    e.universe = symbols
    _multi_tf(e, cfg.timeframes)
    e.cut = 0.10                     # synthetic candles: score them all
    t0 = _time.time()
    out = e.poll_live(first=True)
    took = _time.time() - t0
    assert took < 60, f"a 40-stream poll took {took:.1f}s"
    assert out["ok"] == len(symbols) and out["failed"] == 0
    assert len(e.streams) == len(symbols) * len(cfg.timeframes)
    assert out["bars"] > 0, "the start-up scan walked back over closed bars"
    assert e.error is None
    snap = e.snapshot()
    assert snap["universe"] == len(symbols)
    assert any(s["tf"] == "3h" for s in snap["streams"]), "the aggregated timeframe is live"


def test_a_signal_is_available_on_the_first_poll_not_after_hours(tmp_path, monkeypatch):
    """Start-up scans the last `scan_bars` closed bars, so the page is never empty."""
    from engine import Engine

    symbols = ["AAA_USDT", "BBB_USDT"]
    _fake_exchange(monkeypatch, symbols)
    cfg = _cfg(tmp_path, symbols=symbols, tfs=["15m", "1h"], scan_bars=30)
    e = Engine(mode="live", tfs=cfg.timeframes, symbols=symbols, config=cfg,
               db=str(tmp_path / "scan.db"))
    e.universe = symbols
    _multi_tf(e, cfg.timeframes)
    e.cut = 0.10
    e.poll_live(first=True)
    assert e.memory, "the first poll recorded signals from the bars it walked back over"
    assert all(m["signal_ms"] for m in e.memory)
    assert any(m["origin"] == "scan" for m in e.memory), "they are marked as a start-up scan"
    cands = e.candidates(10)
    assert all(c["status"] in ("pending", "open") for c in cands)
    if cands:
        # what the page prints on the card: size at the configured risk, and the
        # price the trade will be filled at (open of the next bar)
        c = cands[0]
        assert c["size"] is not None
        assert c["size"]["risk_usd"] == pytest.approx(
            cfg.equity_usd * cfg.risk_per_trade_pct / 100)
        if c["status"] == "pending":
            assert c["size"]["estimated_entry"] is True
        else:
            assert c["size"]["estimated_entry"] is False


class _Always:
    """A stand-in model: it always says yes, so the plumbing is what is tested."""

    def predict_proba(self, x):
        return np.array([[0.05, 0.95]] * len(x))


def _multi_tf(engine, tfs):
    """Give an engine a model for every timeframe the test wants to watch."""
    engine.models = {tf: _Always() for tf in tfs}
    engine.tfs = list(tfs)
    engine.missing_tfs = []
    # score on features a candle feed can actually produce (see lab.features.live_safe)
    engine.model_features = features.live_safe()


def _cfg(tmp_path, symbols=("BTC_USDT",), tfs=("1h",), scan_bars=40):
    import config as appconfig

    cfg = appconfig.Config()
    cfg.path = str(tmp_path / "config.json")
    cfg.db = str(tmp_path / "signals.db")
    cfg.symbols = list(symbols)
    cfg.timeframes = list(tfs)
    cfg.scan_bars = scan_bars
    cfg.workers = 4
    return cfg
