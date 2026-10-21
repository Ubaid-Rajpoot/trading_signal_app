"""Tests for the v2 market-structure filter and the version registry.

The point of v2 is not "more signals" - it is "no signal that breaks the rule".
So the tests check the invariants:

* every v2 signal is also a v1 signal (v2 only removes, never invents),
* longs only in an uptrend, shorts only in a downtrend,
* every signal sits in internal structure built after the last major break,
* a ranging / choppy market produces nothing, even where v1 fires,
* the whole thing mirrors for the opposite direction,
* nothing peeks into the future.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core import market_structure as ms
from app.core.strategies import DEFAULT_STRATEGY
from app.core.strategies import get_strategy

# Relaxed TDI thresholds so the tests exercise the *filters*, not the rarity of
# the HVB pattern. The filter logic is independent of how HVB is tuned.
RELAXED = {"hvb_step": 0.5, "hvb_margin": 0.0, "confluence_window": 2}

# Golden numbers for the frozen v1 rules on ``trending(n=2000, seed=21)``.
# If these change, someone edited v1 and every old result is void.
EXPECTED_V1_LONGS = 13
EXPECTED_V1_SHORTS = 31


def _frame(close: np.ndarray, seed: int = 3, tf_ms: int = 900_000) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(close)
    high = close * (1 + abs(rng.standard_normal(n)) * 0.0012)
    low = close * (1 - abs(rng.standard_normal(n)) * 0.0012)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum.reduce([high, open_, close])
    low = np.minimum.reduce([low, open_, close])
    return pd.DataFrame(
        {
            "time": np.arange(n) * tf_ms,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(10, 50, n),
        }
    )


def trending(n: int = 4000, direction: int = -1, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    drift = 0.0006 * direction
    return _frame(100 * np.exp(np.cumsum(drift + 0.0035 * rng.standard_normal(n))), seed=seed)


def choppy(n: int = 4000, seed: int = 11) -> pd.DataFrame:
    """Pure chop: an Ornstein-Uhlenbeck walk - no drift, no persistence."""
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = x[i - 1] + 0.006 * rng.standard_normal() - 0.05 * x[i - 1]
    return _frame(100 + 3.0 * x, seed=seed)


def trend_then_range(n_trend: int = 900, n_chop: int = 5000, seed: int = 5) -> pd.DataFrame:
    """A real downtrend that dies, then chop - the classic "trend is over" case.

    v1 keeps firing in the chop; v2 must not.
    """
    rng = np.random.default_rng(seed)
    trend = np.cumsum(-0.0012 + 0.003 * rng.standard_normal(n_trend))
    base = 100 * np.exp(trend[-1])
    x = np.zeros(n_chop)
    for i in range(1, n_chop):
        x[i] = x[i - 1] + 0.006 * rng.standard_normal() - 0.05 * x[i - 1]
    return _frame(np.concatenate([100 * np.exp(trend), base * (1 + 0.01 * x)]), seed=seed)


@pytest.fixture(scope="module")
def v1():
    return get_strategy("tdi_shift_v1")


@pytest.fixture(scope="module")
def v2():
    return get_strategy("tdi_shift_v2")


def _run(strategy, df, params=None):
    resolved = strategy.resolve_params({**RELAXED, **(params or {})})
    prepared = strategy.prepare(df, resolved)
    return prepared, strategy.rules(prepared, resolved)


# --------------------------------------------------------------- versioning
def test_v1_is_frozen_and_v2_supersedes_it(v1, v2):
    assert v1.id == "tdi_shift_v1" and v1.frozen is True
    assert v1.version == "1.0"
    assert v2.id == "tdi_shift_v2" and v2.supersedes == v1.id
    assert v2.version == "2.0"
    # v1 keeps its original parameter set - nothing was added or removed.
    assert "require_trend" not in v1.default_params()
    assert "require_trend" in v2.default_params()


def test_old_id_resolves_to_the_latest_version():
    from app.core.strategies import DEFAULT_STRATEGY

    assert get_strategy("tdi_shift").id == DEFAULT_STRATEGY
    assert DEFAULT_STRATEGY == "tdi_shift_v5"
    # pinned versions never move
    assert get_strategy("tdi_shift_v1").id == "tdi_shift_v1"
    assert get_strategy("tdi_shift_v2").id == "tdi_shift_v2"
    assert get_strategy("tdi_shift_v3").id == "tdi_shift_v3"
    assert get_strategy("tdi_shift_v4").id == "tdi_shift_v4"


def test_v1_behaviour_is_unchanged(v1):
    """Golden values: if this breaks, v1 was edited and old results are void."""
    df = trending(n=2000, seed=21)
    _, triggers = _run(v1, df)
    assert int(triggers.long_entry.sum()) == EXPECTED_V1_LONGS
    assert int(triggers.short_entry.sum()) == EXPECTED_V1_SHORTS


# ------------------------------------------------------------------ filters
def test_v2_never_invents_a_signal(v1, v2):
    for df in (trending(3000, -1), trending(3000, +1, seed=8), trend_then_range()):
        _, t1 = _run(v1, df)
        _, t2 = _run(v2, df)
        assert not (t2.long_entry & ~t1.long_entry).any()
        assert not (t2.short_entry & ~t1.short_entry).any()


def test_downtrend_only_produces_shorts(v2):
    prepared, triggers = _run(v2, trending(4000, -1))
    assert int(triggers.short_entry.sum()) > 0
    assert int(triggers.long_entry.sum()) == 0
    bars = triggers.short_entry.to_numpy().nonzero()[0]
    assert (prepared["trend_dir"].to_numpy()[bars] < 0).all()


def test_uptrend_only_produces_longs(v2):
    prepared, triggers = _run(v2, trending(4000, +1, seed=8))
    assert int(triggers.long_entry.sum()) > 0
    assert int(triggers.short_entry.sum()) == 0
    bars = triggers.long_entry.to_numpy().nonzero()[0]
    assert (prepared["trend_dir"].to_numpy()[bars] > 0).all()


def test_every_signal_sits_in_structure(v2):
    prepared, triggers = _run(v2, trending(4000, -1))
    for short, bar in zip(
        triggers.short_entry.to_numpy(), range(len(prepared)), strict=False
    ):
        if not short:
            continue
        row = prepared.iloc[bar]
        assert row["regime"] == ms.TRENDING
        assert bool(row["internal_low_ok"])
        assert not bool(row["liq_below"])
        assert row["structure_atr"] == pytest.approx(row["structure_atr"])  # not NaN


def test_ranging_market_is_blocked_even_where_v1_fires(v1, v2):
    df = trend_then_range()
    _, t1 = _run(v1, df)
    prepared, t2 = _run(v2, df)

    chop = (prepared["regime"] != ms.TRENDING).to_numpy()
    fired_v1 = (t1.long_entry | t1.short_entry).to_numpy()
    fired_v2 = (t2.long_entry | t2.short_entry).to_numpy()

    assert int((fired_v1 & chop).sum()) > 0, "fixture must tempt v1 inside the chop"
    assert int((fired_v2 & chop).sum()) == 0, "v2 must never fire while ranging"
    assert int(fired_v2.sum()) > 0, "v2 must still take the trend"


def test_a_clean_range_has_low_efficiency():
    er = ms.efficiency_ratio(choppy(3000)["close"], 40).dropna()
    assert er.median() < 0.2
    up = ms.efficiency_ratio(trending(3000, +1)["close"], 40).dropna()
    assert up.median() > er.median()


def test_filters_are_switchable(v2):
    """Each filter can be turned off, and switching it off can only add signals."""
    df = trending(3000, -1)
    _, strict = _run(v2, df)
    _, no_trend = _run(v2, df, {"require_trend": False})
    _, no_range = _run(v2, df, {"reject_ranging": False})
    assert int(no_trend.short_entry.sum()) >= int(strict.short_entry.sum())
    assert int(no_range.short_entry.sum()) >= int(strict.short_entry.sum())


def test_filter_report_explains_the_gap(v2):
    df = trending(3000, -1)
    prepared, triggers = _run(v2, df)
    resolved = v2.resolve_params(RELAXED)
    report = v2.filter_report(prepared, resolved)
    assert report["raw"] >= report["taken"]
    assert report["taken"] == int(triggers.long_entry.sum() + triggers.short_entry.sum())
    if report["raw"] > report["taken"]:
        assert report["blocked"]


# ------------------------------------------------------------- no lookahead
@pytest.mark.parametrize("cut", [1200, 2000, 2600])
def test_v2_is_lag_safe(v2, cut):
    """Cutting the future must not change any past decision."""
    full = trending(2700, -1, seed=13)
    resolved = v2.resolve_params(RELAXED)
    _, t_full = _run(v2, full)

    head = full.iloc[:cut].reset_index(drop=True)
    _, t_head = _run(v2, head)

    # Compare the bars both runs could have decided on, ignoring the last few
    # where the head run has no confirmation window at all.
    window = slice(0, cut - 5)
    assert t_head.short_entry.to_numpy()[window].tolist() == t_full.short_entry.to_numpy()[window].tolist()
    assert t_head.long_entry.to_numpy()[window].tolist() == t_full.long_entry.to_numpy()[window].tolist()


def test_describe_reports_a_block_reason(v2):
    prepared, _ = _run(v2, choppy(1500))
    info = ms.describe(prepared.iloc[-1])
    assert info["regime"] in (ms.RANGING, ms.UNKNOWN)
    assert info["blocked"] == "ranging"
    assert info["trend"] in ("UP", "DOWN", "NONE")


# ------------------------------------------------------------------ presets
def test_presets_never_turn_the_rules_off(v2):
    for mode in ("strict", "balanced", "loose"):
        resolved = v2.resolve_params({"filter_mode": mode})
        assert resolved["require_trend"] and resolved["reject_ranging"]
        assert resolved["require_structure"] and resolved["reject_liquidity"]


def test_loose_is_more_permissive_than_strict(v2):
    df = trend_then_range()
    _, strict = _run(v2, df, {"filter_mode": "strict"})
    _, loose = _run(v2, df, {"filter_mode": "loose"})
    assert int(loose.short_entry.sum()) >= int(strict.short_entry.sum())


def test_explicit_threshold_beats_the_preset(v2):
    resolved = v2.resolve_params({"filter_mode": "loose", "adx_min": 30.0})
    assert resolved["adx_min"] == 30.0
    # an unknown preset name falls back to strict instead of crashing
    assert v2.resolve_params({"filter_mode": "nonsense"})["er_min"] == 0.2


# ------------------------------------------------------- v3: HVB on volume
def test_v3_requires_a_volume_spike():
    v3 = get_strategy("tdi_shift_v3")
    df = trending(4000, -1, seed=17)
    resolved = v3.resolve_params({**RELAXED, "hvb_volume_mult": 1.25})
    prepared = v3.prepare(df, resolved)

    # every HVB bar must be a volume spike
    for column in ("hvb_up", "hvb_down"):
        for bar in prepared[column].to_numpy().nonzero()[0]:
            assert prepared["hvb_volume_ratio"].iloc[bar] >= resolved["hvb_volume_mult"]

    # switching the volume requirement off can only add HVBs
    loose = v3.prepare(df, {**resolved, "hvb_use_volume": False})
    assert int(loose["hvb_up"].sum() + loose["hvb_down"].sum()) >= int(
        prepared["hvb_up"].sum() + prepared["hvb_down"].sum()
    )


def test_v3_is_v2_plus_volume():
    """v3 must keep every v2 gate: it only changes what counts as an HVB."""
    v2, v3 = get_strategy("tdi_shift_v2"), get_strategy("tdi_shift_v3")
    p2, p3 = v2.resolve_params({}), v3.resolve_params({})
    for key in ("require_trend", "reject_ranging", "require_structure", "reject_liquidity"):
        assert p3[key] == p2[key]
    assert v3.supersedes == v2.id
    # tdi_shift now means the newest version, which is v4
    assert get_strategy("tdi_shift").id == DEFAULT_STRATEGY


def test_v3_params_are_relaxed_but_explicit_values_win():
    v3 = get_strategy("tdi_shift_v3")
    assert v3.resolve_params({})["hvb_step"] == 2.0
    assert v3.resolve_params({"hvb_step": 4.0})["hvb_step"] == 4.0
    assert v3.resolve_params({})["hvb_volume_mult"] == 1.5
