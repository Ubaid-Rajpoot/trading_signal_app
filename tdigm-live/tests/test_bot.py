"""Tests that the numbers are right before any of it is pointed at a market."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.buckets import Rollup, bucket_start  # noqa: E402
from bot.engine import Engine, sweep_chain_errors  # noqa: E402
from bot.indicators import break_signal, centre_of, hvb_signal, rsi, sma, stdev, tdi  # noqa: E402


# ------------------------------------------------------------------ helpers
def bar(open_time: int, price: float, high=None, low=None, symbol="BTCUSDT") -> dict:
    return {
        "symbol": symbol,
        "open_time": open_time,
        "close_time": open_time + 59_999,
        "open": price,
        "high": high if high is not None else price + 0.5,
        "low": low if low is not None else price - 0.5,
        "close": price,
        "volume": 1.0,
        "trades": 1,
        "closed": True,
    }


# ------------------------------------------------------------------- sma
def test_sma_matches_hand_calculation():
    values = [1, 2, 3, 4, 5]
    assert sma(values, 2) == [None, 1.5, 2.5, 3.5, 4.5]
    assert sma(values, 5)[-1] == 3.0
    assert sma(values, 1) == [1, 2, 3, 4, 5]


def test_stdev_is_population_stdev():
    values = [2, 4, 4, 4, 5, 5, 7, 9]
    assert stdev(values, 8)[-1] == pytest.approx(2.0)


# ------------------------------------------------------------------- rsi
def test_rsi_is_100_on_a_straight_line_up():
    closes = [float(i) for i in range(1, 60)]
    series = rsi(closes, 14)
    assert series[14] == pytest.approx(100.0)
    assert series[13] is None


def test_rsi_stays_inside_0_100():
    closes = [100 + math.sin(i / 3) * 5 for i in range(200)]
    series = [v for v in rsi(closes, 50) if v is not None]
    assert series and all(0 <= v <= 100 for v in series)


def test_rsi_warmup_aligns_with_period():
    closes = [float(i % 7 + 10) for i in range(80)]
    series = rsi(closes, 50)
    assert series[49] is None
    assert series[50] is not None


# ------------------------------------------------------------------- tdi
def test_tdi_uses_the_traders_setting():
    closes = [100 + (i % 11) * 0.4 for i in range(300)]
    points = tdi(closes, rsi_period=50, band_period=50, fast_period=1, slow_period=9)
    last = points[-1]
    assert last.fast is not None and last.mbl is not None
    # fast_period = 1 means the purple line IS the RSI
    assert last.fast == pytest.approx(rsi(closes, 50)[-1])
    # bands are the base line +/- 1.6185 stdev
    r = [v for v in rsi(closes, 50) if v is not None]
    expected_band = 1.6185 * stdev(r, 50)[-1]
    assert last.upper == pytest.approx(last.mbl + expected_band)
    assert last.lower == pytest.approx(last.mbl - expected_band)


def test_hvb_fires_only_on_the_crossing_bar():
    points = tdi([100 + (i % 13) for i in range(400)], 50, 50, 1, 9)
    ups = [i for i in range(1, len(points)) if hvb_signal(points, i) == "up"]
    downs = [i for i in range(1, len(points)) if hvb_signal(points, i) == "down"]
    assert ups or downs
    for i in ups:
        assert points[i - 1].fast <= points[i - 1].mbl and points[i].fast > points[i].mbl
    for i in downs:
        assert points[i - 1].fast >= points[i - 1].mbl and points[i].fast < points[i].mbl


def test_hvb_min_points_filter():
    points = tdi([100 + (i % 13) for i in range(400)], 50, 50, 1, 9)
    strict = [i for i in range(1, len(points)) if hvb_signal(points, i, min_points=5.0)]
    loose = [i for i in range(1, len(points)) if hvb_signal(points, i)]
    assert set(strict) <= set(loose)


# ------------------------------------------------------------------ break
def test_break_needs_a_close_beyond_the_window():
    candles = [bar(i * 60_000, 100.0, 101.0, 99.0) for i in range(30)]
    assert break_signal(candles, 25, lookback=20) is None
    candles[25] = bar(25 * 60_000, 102.0, 102.0, 100.0)
    assert break_signal(candles, 25, lookback=20) == "high"
    candles[25] = bar(25 * 60_000, 98.0, 100.0, 97.5)
    assert break_signal(candles, 25, lookback=20) == "low"


def test_break_ignores_wick_only():
    candles = [bar(i * 60_000, 100.0, 101.0, 99.0) for i in range(30)]
    # high pokes above the window but the close comes back inside
    candles[25] = bar(25 * 60_000, 100.5, 105.0, 99.5)
    assert break_signal(candles, 25, lookback=20) is None


# ----------------------------------------------------------------- bucket
def test_bucket_alignment():
    assert bucket_start(1_700_000_000_000, "15m") % 900_000 == 0
    assert bucket_start(1_700_000_000_000, "1h") % 3_600_000 == 0


def test_rollup_builds_a_correct_5m_candle():
    roll = Rollup("5m")
    start = 1_700_000_000_000 - (1_700_000_000_000 % 300_000)
    done = []
    for i in range(5):
        done += roll.add(bar(start + i * 60_000, 100 + i, high=101 + i, low=99 + i))
    assert done == []  # the bucket is not complete yet
    done += roll.add(bar(start + 5 * 60_000, 200))
    assert len(done) == 1
    candle = done[0]
    assert candle["open"] == 100
    assert candle["close"] == 104
    assert candle["high"] == 105
    assert candle["low"] == 99
    assert candle["open_time"] == start
    assert candle["close_time"] == start + 300_000 - 1
    assert candle["volume"] == 5.0


def test_rollup_flush_closes_the_last_candle():
    roll = Rollup("15m")
    start = 1_700_000_000_000 - (1_700_000_000_000 % 900_000)
    roll.add(bar(start, 100))
    flushed = roll.flush()
    assert len(flushed) == 1 and flushed[0]["closed"] is True


# ----------------------------------------------------------------- engine
def fast_engine(**filters):
    """Small indicator periods, so a test needs 40 bars instead of 110."""
    return Engine(
        symbols=["BTCUSDT"],
        timeframes=["5m"],
        tdi_settings={"rsi_period": 14, "band_period": 14, "fast_period": 1, "slow_period": 3},
        break_lookback=5,
        # the trend/depth gates have their own file of tests; these fixtures are
        # monotone ramps that contain no swing structure at all
        filters={"structure": True, "hvb": True, "trend_context": False,
                 "require_bos": False, **filters},
        structure={"pivot_len": 2, "sweep_max_wait": 60},
        # these tests pin the previous sweep engine, which is still selectable
        strategy="sweep",
    )


def test_engine_alerts_when_both_conditions_hit():
    engine = fast_engine()
    events = []

    async def broadcast(message):
        events.append(message)

    engine.broadcast = broadcast

    async def run():
        start = 1_700_000_000_000 - (1_700_000_000_000 % 300_000)
        price = 100.0
        # feed enough candles to warm the indicators, oscillating
        for i in range(400):
            price += 0.6 if i % 9 < 5 else -0.55
            await engine.on_candle(bar(start + i * 60_000, price))
        cell = engine.cells[("BTCUSDT", "5m")]
        return cell

    cell = asyncio.run(run())
    assert len(cell.candles) > 20, "5m candles must be aggregated"
    alerts = [e for e in events if e.get("type") == "alert"]
    # at least one HVB must have been detected in 400 oscillating bars
    assert alerts, "expected at least one alert"
    for event in alerts:
        alert = event["alert"]
        assert alert["kind"] in {"signal", "hvb", "break", "sweep"}
        if alert["kind"] == "signal":
            # a signal is only ever a complete structure setup
            assert alert["structure"] is True
            assert alert["setup"]["stop"] != alert["setup"]["entry"]


def test_engine_dedupes_and_warms_up():
    engine = fast_engine()

    async def run():
        start = 1_700_000_000_000 - (1_700_000_000_000 % 300_000)
        for i in range(60):  # fewer bars than the warm-up needs
            await engine.on_candle(bar(start + i * 60_000, 100 + i * 0.1))
        cell = engine.cells[("BTCUSDT", "5m")]
        return cell

    cell = asyncio.run(run())
    assert cell.alerts == 0
    assert cell.as_dict()["warm"] is False


def test_engine_ignores_forming_candles_for_alerts():
    engine = make_engine()

    async def run():
        start = 1_700_000_000_000 - (1_700_000_000_000 % 300_000)
        for i in range(200):
            await engine.on_candle(bar(start + i * 60_000, 100 + (i % 5)))
        forming = bar(start + 200 * 60_000, 500.0)
        forming["closed"] = False
        before = len(engine.alerts)
        await engine.on_candle(forming)
        return before, len(engine.alerts)

    before, after = asyncio.run(run())
    assert after == before, "a forming candle must never raise an alert"


# --------------------------------------------------------------- structure
from bot.filters import Filters  # noqa: E402
from bot.structure import Structure  # noqa: E402


def series(rows, symbol="BTCUSDT"):
    return [
        bar(i * 60_000, close, high=high, low=low, symbol=symbol)
        for i, (high, low, close) in enumerate(rows)
    ]


def run_structure(rows, settings=None):
    st = Structure(settings or {"pivot_len": 2, "sweep_max_wait": 30})
    pivots, sweeps, setups, invalid = [], [], [], []
    for i, item in enumerate(series(rows)):
        events = st.add(item)
        if events["pivot"]:
            pivots.append((i, events["pivot"]))
        if events["sweep"]:
            sweeps.append((i, events["sweep"]))
        if events["setup"]:
            setups.append((i, events["setup"]))
        if events["invalidated"]:
            invalid.append(i)
    return st, pivots, sweeps, setups, invalid


# a swing high at 106, a swing low at 95, then the sweep of the high and the
# break back through the low - the drawing, as numbers
SHORT_PATTERN = [
    (101, 99, 100), (102, 99, 101), (104, 100, 103), (106, 102, 105),   # high 106
    (105, 100, 101), (101, 96, 97), (98, 95, 96),                      # low 95
    (100, 95, 99), (103, 98, 102), (107, 101, 106),                    # high 107
    (106, 101, 102), (102, 97, 98), (99, 95, 96),                      # low 95 again
    (100, 97, 98), (101, 98, 99), (102, 99, 100), (103, 100, 101),
    (104, 101, 102), (108, 102, 104),  # sweep the 107 high (high 108 > 107)
    (104, 90, 91),                     # break below the 95 low -> SHORT setup
]


def test_sweep_then_opposite_break_makes_a_short_setup():
    st, pivots, sweeps, setups, _ = run_structure(SHORT_PATTERN)
    assert sweeps, "the 106 high should have been swept"
    side, sweep = sweeps[0]
    assert sweep.side == "high" and sweep.level == 106.0
    assert setups, "breaking the opposite low should complete the setup"
    index, setup = setups[0]
    assert setup.side == "SHORT"
    assert setup.entry == 91.0
    assert setup.stop == 108.0, "the stop is just beyond the sweep extreme"
    assert setup.broke.price == 95.0, "the opposite swing low is what broke"
    assert setup.target is None, "no pre-sweep liquidity was left below the entry"
    proof = setup.as_dict()
    assert proof["sweep_time"] < proof["break_time"]
    assert proof["sweep_high"] > proof["swept_level"]
    assert proof["sweep_low"] == 101.0


def test_signal_integrity_requires_a_real_preceding_sweep_and_same_candle_hvb():
    valid = {
        "kind": "signal", "side": "SHORT", "time": 20_000, "hvb": "down", "hvb_lag": 0,
        "setup": {
            "side": "SHORT", "swept_side": "high", "swept_level": 107.0,
            "sweep_time": 10_000, "sweep_high": 108.0, "sweep_low": 102.0,
            "broke_level": 95.0, "break_time": 20_000,
        },
    }
    assert sweep_chain_errors(valid) == []

    missing = {**valid, "setup": {**valid["setup"], "sweep_time": 0}}
    assert any("sweep time" in reason for reason in sweep_chain_errors(missing))
    wrong_hvb = {**valid, "hvb": "up"}
    assert any("down HVB" in reason for reason in sweep_chain_errors(wrong_hvb))
    fake_wick = {**valid, "setup": {**valid["setup"], "sweep_high": 106.9}}
    assert any("did not take" in reason for reason in sweep_chain_errors(fake_wick))


@pytest.mark.asyncio
async def test_publication_boundary_refuses_an_hvb_and_break_without_a_sweep():
    engine = make_engine(hvb=True)
    cell = engine.cells[("BTCUSDT", "5m")]
    candle = bar(20_000, 95.0, high=100.0, low=94.0)
    candle["timeframe"] = "5m"
    malformed = {
        "kind": "signal", "symbol": "BTCUSDT", "timeframe": "5m",
        "side": "SHORT", "time": candle["close_time"], "hvb": "down", "hvb_lag": 0,
        "setup": {"side": "SHORT", "broke_level": 96.0, "break_time": candle["close_time"]},
    }
    await engine._publish(cell, malformed, candle)
    assert not engine.signals and not engine.setup_payloads
    assert any("preceding high sweep" in reason for reason in engine.filters.rejections)


def test_a_candle_that_only_remains_beyond_the_level_is_not_a_body_break():
    # The sweep candle itself closes below the opposite low.  Because the rule
    # requires the break *subsequently*, a following candle that merely stays
    # below 95 must not be mislabelled as the body-break candle.
    rows = list(SHORT_PATTERN[:19])
    rows[9] = (105.5, 101, 104)  # do not sweep the original 106 high early
    rows[-1] = (108, 90, 91)     # sweep high + close below low on this one bar
    rows.append((94, 89, 90))
    _st, _pivots, sweeps, setups, _invalid = run_structure(rows)
    assert sweeps and not setups


def test_sweep_is_invalidated_when_price_keeps_going():
    # sweep the 107 high and then CLOSE above it: no reversal, no setup
    rows = SHORT_PATTERN[:19] + [(110, 103, 109)]
    st, _, sweeps, setups, invalid = run_structure(rows)
    assert sweeps and not setups
    assert invalid, "closing beyond the sweep extreme must drop the setup"


def test_no_setup_without_the_opposite_break():
    rows = SHORT_PATTERN[:19] + [(104, 96, 97)]  # sweeps the high, stays inside
    _, _, sweeps, setups, _ = run_structure(rows)
    assert sweeps and not setups


def test_long_mirror_of_the_pattern():
    rows = [(100 - h + l, l, c) for h, l, c in SHORT_PATTERN]
    # mirror prices around 100 and flip high/low (a low sweep, then a high break)
    flipped = [(200 - low, 200 - high, 200 - close) for high, low, close in SHORT_PATTERN]
    st, pivots, sweeps, setups, _ = run_structure(flipped)
    assert sweeps and setups
    assert sweeps[0][1].side == "low"
    assert setups[0][1].side == "LONG"
    assert setups[0][1].stop == 92.0


def test_target_is_the_next_liquidity_level_beyond_the_entry():
    from bot.structure import Level

    st = Structure({"target_mode": "next"})
    st.levels.extend(
        [
            Level("low", 85.0, 0, 0),
            Level("low", 90.0, 5, 0),
            Level("low", 95.0, 10, 0),
            Level("high", 107.0, 12, 0),
        ]
    )
    assert st.next_target("SHORT", entry=94.0, before=15).price == 90.0
    assert st.next_target("LONG", entry=100.0, before=15).price == 107.0


def test_range_target_reaches_the_far_side_of_the_range():
    from bot.structure import Level

    st = Structure({"target_mode": "range"})
    st.levels.extend([Level("low", 85.0, 0, 0), Level("low", 90.0, 5, 0), Level("low", 95.0, 10, 0)])
    assert st.next_target("SHORT", entry=94.0, before=15).price == 85.0


def test_target_ignores_levels_printed_after_the_sweep():
    from bot.structure import Level

    st = Structure({"target_mode": "next"})
    st.levels.extend([Level("low", 90.0, 5, 0), Level("low", 80.0, 20, 0)])
    # the 80 low printed after the sweep is part of the move, not a target
    assert st.next_target("SHORT", entry=94.0, before=15).price == 90.0


# ----------------------------------------------------------------- filters
# A long setup that satisfies the round-17 trend rules, so a filter test that is
# about something *else* (sides, sessions, RR) is not silently blocked by them.
# The trend gates themselves are tested in tests/test_trend_gates.py.
TREND_OK = {
    "external": {"direction": 1, "clean": True, "bos_direction": 1, "bos_age": 12,
                 "source": "major", "bos_level": 100.0},
    "internal": {"direction": 1, "bos_direction": 1, "bos_age": 3},
}


def candidate(**kwargs):
    base = {"structure": True, "hvb": "up", "hvb_points": 3.0, "break": "high", "side": "LONG",
            "rr": 2.0, "context": json.loads(json.dumps(TREND_OK)),
            "depth": {"value": 2, "label": "internal", "internal_shifts": 0},
            "volume_ratio": 1.8}
    base.update(kwargs)
    return base


def test_filters_pass_a_complete_setup():
    f = Filters({"structure": True, "hvb": True})
    ok, reasons = f.apply(candidate(), trend=1)
    assert ok and reasons == []
    assert f.summary()["passed"] == 1


def test_hvb_is_required_and_counted():
    f = Filters({"structure": True, "hvb": True})
    ok, reasons = f.apply(candidate(hvb=None))
    assert not ok
    assert "HVB" in reasons[0]
    assert f.summary()["rejected"] == 1


def test_hvb_min_points_filter():
    f = Filters({"hvb": True, "hvb_min_points": 5})
    assert not f.apply(candidate(hvb_points=2.0))[0]
    assert f.apply(candidate(hvb_points=6.0))[0]


def test_hvb_must_point_the_same_way_as_the_structure_break():
    f = Filters({
        "hvb": True, "hvb_min_points": 0, "hvb_min_speed": 0,
        "trend_context": False, "require_bos": False, "structure_direction": False,
    })
    ok, reasons = f.apply(candidate(side="SHORT", hvb="up"))
    assert not ok
    assert any("direction up opposes the short" in reason for reason in reasons)
    assert f.apply(candidate(side="SHORT", hvb="down"))[0]


def test_min_rr_filters_small_targets():
    f = Filters({"min_rr": 1.5})
    assert not f.apply(candidate(rr=0.4))[0]
    assert not f.apply(candidate(rr=None))[0]
    assert f.apply(candidate(rr=1.6))[0]


def test_sides_filter():
    f = Filters({"sides": ["short"], "trend_context": False, "require_bos": False})
    assert not f.apply(candidate())[0]
    assert f.apply(candidate(side="SHORT", hvb="down"))[0]


def test_trend_filter_with_and_against():
    with_trend = Filters({"trend": "with"})
    assert with_trend.apply(candidate(), trend=1)[0]
    assert not with_trend.apply(candidate(), trend=-1)[0]
    against = Filters({"trend": "against"})
    assert against.apply(candidate(), trend=-1)[0]


def test_session_filter():
    f = Filters({"sessions": ["07:00-11:00"]})
    assert f.apply(candidate(), minute_of_day=10 * 60)[0]
    assert not f.apply(candidate(), minute_of_day=20 * 60)[0]


def test_max_bars_since_sweep():
    f = Filters({"max_bars_since_sweep": 10})
    assert not f.apply(candidate(bars_since_sweep=40))[0]
    assert f.apply(candidate(bars_since_sweep=4))[0]


# ------------------------------------------------------------ config store
from bot import config_store  # noqa: E402


def test_filters_validation_ignores_junk_and_clamps(tmp_path):
    got = config_store.validate_filters(
        {"hvb": 0, "min_rr": -3, "trend": "sideways", "sides": ["long", "banana"], "nonsense": 1}
    )
    assert got["hvb"] is False
    assert got["min_rr"] == 0.0
    assert got["trend"] == "off"
    assert got["sides"] == ["long"]
    assert "nonsense" not in got


def test_symbols_are_cleaned_and_written(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"symbols": [], "timeframes": ["1m"]}')
    config = config_store.read(path)
    config_store.set_symbols(path, config, ["btc/usdt", " ETHUSDT ", "", "BTCUSDT"])
    import json

    saved = json.loads(path.read_text())
    assert saved["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert saved["timeframes"] == ["1m"]


# -------------------------------------------------------------------- feed
from bot.feed import BaseFeed, MexcFeed, ReplayFeed, make_feed  # noqa: E402


def test_feed_splits_symbols_across_sockets():
    feed = BaseFeed([f"S{i}USDT" for i in range(250)], chunk_size=100)
    chunks = feed._chunks()
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_adding_and_removing_symbols_bumps_the_generation():
    feed = BaseFeed(["BTCUSDT"])
    before = feed._generation
    assert feed.add_symbols(["ethusdt"]) == ["ETHUSDT"]
    assert feed.add_symbols(["ETHUSDT"]) == []
    assert feed._generation > before
    assert feed.remove_symbols(["BTCUSDT"]) == ["BTCUSDT"]
    assert feed.symbols == ["ETHUSDT"]


def test_mexc_has_no_3m_candle_so_the_engine_must_aggregate():
    feed = MexcFeed(["BTCUSDT"])
    assert feed.exchange_interval("3m") is None
    assert feed.exchange_interval("1h") == "1h", "the feed translates to 60m inside history()"
    assert make_feed("binance", ["BTCUSDT"]).exchange_interval("1h") == "1h"
    with pytest.raises(Exception):
        make_feed("ftx", ["BTCUSDT"])


@pytest.mark.asyncio
async def test_replay_feed_yields_real_candle_shape(tmp_path):
    path = tmp_path / "BTCUSDT-1m-2025-01-01.csv"
    path.write_text("1735689600000000,100,101,99,100.5,12\n1735689660000000,100.5,102,100,101,9\n")
    feed = ReplayFeed(tmp_path, ["BTCUSDT"], speed=1000, loop=False)
    got = [c async for c in feed.candles()]
    assert len(got) == 2
    assert got[0]["symbol"] == "BTCUSDT"
    assert got[0]["close"] == 100.5
    assert got[1]["open_time"] - got[0]["open_time"] == 60_000


# ------------------------------------------------ engine: the whole product
def push_sequence(engine, rows, symbol="BTCUSDT"):
    async def run():
        for item in series(rows, symbol):
            await engine.on_candle(item)

    return run()


def make_engine(**filters):
    return Engine(
        symbols=["BTCUSDT"],
        timeframes=["5m"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9, "band_mult": 1.6185},
        filters={"structure": True, "hvb": False, "break": False,
                 "trend_context": False, "require_bos": False, **filters},
        structure={"pivot_len": 2, "sweep_max_wait": 60},
        strategy="sweep",
    )


def market_rows():
    """A monotone rise (no swing points at all, so the fixture is unambiguous)
    followed by the pattern. Rows are 5 minutes apart, so each row IS one 5m
    candle; a candle only closes when the next one opens, hence the last row."""
    rows = []
    for i in range(160):
        base = 90 + i * 0.06
        rows.append((base + 0.3, base - 0.3, base))
    return rows + SHORT_PATTERN + [(95, 93, 94)]


async def push_5m(engine, rows):
    for i, (high, low, close) in enumerate(rows):
        await engine.on_candle(bar(1_735_689_600_000 + i * 300_000, close, high=high, low=low))


@pytest.mark.asyncio
async def test_engine_emits_a_signal_for_the_complete_setup():
    engine = make_engine(hvb=False)
    await push_5m(engine, market_rows())
    signals = [a for a in engine.alerts if a["kind"] == "signal"]
    assert signals, "the sweep + opposite break is a complete setup"
    alert = signals[-1]
    assert alert["side"] == "SHORT"
    assert alert["structure"] is True
    assert alert["setup"]["stop"] == 108.0, "the stop sits just beyond the swept high"
    assert alert["setup"]["entry"] < alert["setup"]["stop"]
    assert alert["filtered"] == []


@pytest.mark.asyncio
async def test_production_tdigm_settings_confirm_the_break_on_the_same_candle():
    # A neutral, moving RSI baseline followed by the trader's sweep/body-break
    # fixture. The final drop crosses the MBL down with speed on exactly the
    # candle that closes below the opposite low.
    prefix = []
    for i in range(180):
        close = 98 + (i % 10) * 0.15 - (i % 3) * 0.03
        prefix.append((close + 0.3, close - 0.3, close))
    engine = make_engine(
        hvb=True, hvb_window=0, hvb_min_points=1.0, hvb_min_speed=1.5,
        regime=False, structure_direction=False, block_liquidity_pool=False,
    )
    await push_5m(engine, prefix + SHORT_PATTERN + [(95, 93, 94)])
    signals = [row for row in engine.setup_payloads.values() if row["side"] == "SHORT"]
    assert signals
    signal = signals[-1]
    assert signal["hvb_side"] == "down"
    assert signal["hvb_ms"] == signal["created_ms"]
    assert signal["bos_ms"] <= signal["created_ms"]


@pytest.mark.asyncio
async def test_engine_requires_hvb_when_the_filter_is_on():
    engine = make_engine(hvb=True)
    seen = []

    async def collect(message):
        seen.append(message)

    engine.broadcast = collect
    await push_5m(engine, market_rows())
    assert not [a for a in engine.alerts if a["kind"] == "signal"]
    filtered = [m for m in seen if m["type"] == "filtered"]
    assert filtered, "a setup that fails a filter must be reported, not dropped silently"
    assert any("HVB" in reason for reason in filtered[0]["alert"]["filtered"])


@pytest.mark.asyncio
async def test_forming_candles_never_alert():
    engine = make_engine(hvb=False)
    await push_5m(engine, market_rows()[:-1])
    before = len(engine.alerts)
    forming = bar(9_999_999 * 60_000, 1.0, high=1e6, low=0.5)
    forming["closed"] = False
    await engine.on_candle(forming)
    assert len(engine.alerts) == before


@pytest.mark.asyncio
async def test_adding_a_symbol_builds_its_cells():
    engine = make_engine()
    assert engine.add_symbols(["ethusdt"]) == ["ETHUSDT"]
    assert ("ETHUSDT", "5m") in engine.cells
    assert engine.remove_symbols(["ETHUSDT"]) == ["ETHUSDT"]
    assert ("ETHUSDT", "5m") not in engine.cells


# ------------------------------------------------- the centre band of the HVB
def test_hvb_centre_defaults_to_the_middle_of_the_bands():
    closes = [100 + (i % 11) * 0.4 for i in range(400)]
    points = tdi(closes, 50, 50, 1, 9, 1.6185)
    last = points[-1]
    assert last.mbl == pytest.approx((last.upper + last.lower) / 2)
    assert centre_of(last, "mbl") == last.mbl
    assert centre_of(last, "fifty") == 50.0


def test_hvb_fifty_reference_crosses_the_50_line():
    closes = [100 + (i % 7) * 1.5 for i in range(400)]  # swings well either side of 50
    points = tdi(closes, 50, 50, 1, 9, 1.6185)
    fifty = [i for i in range(1, len(points)) if hvb_signal(points, i, 0, "fifty")]
    mbl = [i for i in range(1, len(points)) if hvb_signal(points, i, 0, "mbl")]
    assert fifty, "the price line must cross 50 in this series"
    for i in fifty:
        assert (points[i - 1].fast - 50) * (points[i].fast - 50) <= 0
    assert fifty != mbl, "different centre lines give different crosses"


def test_the_aggressive_break_threshold_rejects_grazes():
    closes = [100 + (i % 13) * 0.2 for i in range(600)]
    points = tdi(closes, 50, 50, 1, 9, 1.6185)
    loose = [i for i in range(1, len(points)) if hvb_signal(points, i, 0.0, "mbl")]
    strict = [i for i in range(1, len(points)) if hvb_signal(points, i, 1.0, "mbl")]
    assert loose and strict
    assert set(strict) < set(loose), "a threshold can only remove events"
    for i in strict:
        assert abs(points[i].fast - points[i].mbl) >= 1.0


@pytest.mark.asyncio
async def test_signal_carries_the_centre_band_and_a_chart_tail():
    engine = make_engine(hvb=False)
    await push_5m(engine, market_rows())
    signals = [a for a in engine.alerts if a["kind"] == "signal"]
    assert signals
    alert = signals[-1]
    assert alert["centre_reference"] in {"mbl", "fifty"}
    assert alert["centre"] is not None
    assert len(alert["tdi_tail"]) >= 5
    assert all(len(pair) == 2 for pair in alert["tdi_tail"])


@pytest.mark.asyncio
async def test_old_signal_chart_is_fetched_around_the_setup_not_around_today():
    focus = 1_769_644_800_000  # 2026-01-29 UTC
    step = 86_400_000

    class HistoricalFeed:
        def __init__(self):
            self.end_time = None

        def exchange_interval(self, timeframe):
            return timeframe

        async def history(self, symbol, interval, limit=500, end_time=None):
            self.end_time = end_time
            start = end_time - limit * step
            return [
                {
                    "symbol": symbol, "open_time": start + i * step,
                    "close_time": start + (i + 1) * step - 1,
                    "open": 2.0, "high": 2.1, "low": 1.9, "close": 2.0,
                    "volume": 10.0, "closed": True,
                }
                for i in range(limit)
            ]

    engine = Engine(
        symbols=["XRPUSDT"], timeframes=["1d"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9},
        filters={}, structure={"pivot_len": 5}, strategy="sweep",
    )
    feed = HistoricalFeed()
    candles = await engine.chart_history(feed, "XRPUSDT", "1d", bars=600, at=focus)
    assert len(candles) == 600
    assert feed.end_time > focus, "the window leaves post-signal context"
    assert candles[0]["close_time"] < focus < candles[-1]["close_time"]

    selected = {
        "key": f"XRPUSDT|1d|{focus}", "symbol": "XRPUSDT", "timeframe": "1d",
        "side": "SHORT", "created_ms": focus, "hvb_side": "down", "hvb_ms": focus,
        "hvb_lag": 0, "swept_side": "high", "swept_level": 2.05,
        "sweep_ms": focus - 3 * step, "bos_level": 1.95,
        "setup": {"side": "SHORT", "swept_side": "high", "swept_level": 2.05,
                  "sweep_time": focus - 3 * step, "sweep_high": 2.1,
                  "broke_level": 1.95, "break_time": focus},
    }
    body = engine.chart("XRPUSDT", "1d", 600, candles_override=candles, selected=selected)
    assert body["selected"]["chain_valid"] is True
    assert 0 < body["focus_index"] < len(body["candles"]) - 1


@pytest.mark.asyncio
async def test_switching_the_centre_reference_is_live():
    engine = make_engine(hvb=False)
    await push_5m(engine, market_rows())
    assert engine.set_centre("fifty") == "fifty"
    assert engine.centre_reference == "fifty"
    assert engine.tdi_settings["hvb_reference"] == "fifty"
    assert engine.set_centre("nonsense") == "mbl"


# ------------------------------------------------------------------ regime
def _synthetic_candles(step: float, noise: float, n: int = 400, seed: int = 3) -> list[dict]:
    """A made-up market: ``step`` is the drift per bar, ``noise`` the chop."""
    import random

    rng = random.Random(seed)
    price, out = 100.0, []
    for i in range(n):
        price += step + rng.gauss(0, noise)
        high = price + abs(rng.gauss(0, noise * 0.6))
        low = price - abs(rng.gauss(0, noise * 0.6))
        out.append(bar(1_700_000_000_000 + i * 60_000, price, high, low))
    return out


def test_regime_reads_a_trend_and_a_range_differently():
    from bot.regime import regime_series

    trending = regime_series(_synthetic_candles(step=1.2, noise=0.5))
    ranging = regime_series(_synthetic_candles(step=0.0, noise=2.0))
    last_trend, last_range = trending[-1], ranging[-1]
    assert last_trend.adx > last_range.adx
    assert last_trend.er > last_range.er
    assert last_trend.state(20, 0.10) == "trend"
    assert last_range.state(20, 0.10) == "range"


def test_regime_tracker_agrees_with_the_batch_maths():
    from bot.regime import RegimeTracker, regime_series

    candles = _synthetic_candles(step=0.3, noise=1.0, n=500)
    batch = regime_series(candles)
    tracker = RegimeTracker()
    live = [tracker.add(c) for c in candles]
    for i in range(1, len(candles)):
        for name in ("adx", "plus_di", "minus_di", "er", "ema_fast", "ema_slow", "atr"):
            a, b = getattr(batch[i], name), getattr(live[i], name)
            if a is None or b is None:
                continue
            assert math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12), f"{name} at {i}"


def test_filters_reject_a_ranging_market():
    f = Filters({"structure": True, "hvb": True, "regime": True, "regime_adx_min": 20, "regime_er_min": 0.1})
    ok, reasons = f.apply(candidate(), regime={"state": "range", "adx": 14.2, "er": 0.05, "direction": 1})
    assert not ok
    assert any("ranging market" in r for r in reasons)
    ok, reasons = f.apply(candidate(), regime={"state": "trend", "adx": 28.4, "er": 0.42, "direction": 1})
    assert ok, reasons


def test_filters_skip_setups_when_the_market_state_is_unknown():
    f = Filters({"structure": True, "hvb": True, "regime": True})
    ok, reasons = f.apply(candidate(), regime={"state": None, "adx": None, "er": None, "direction": None})
    assert not ok and any("not known yet" in r for r in reasons)


def test_regime_direction_with_blocks_counter_trend_setups():
    cfg = {"structure": True, "hvb": True, "regime": True, "regime_direction": "with"}
    up = {"state": "trend", "adx": 30.0, "er": 0.4, "direction": 1}
    assert Filters(cfg).apply(candidate(side="SHORT"), regime=up)[0] is False
    assert Filters(cfg).apply(candidate(side="LONG"), regime=up)[0] is True


def test_hvb_never_fires_on_an_outer_band_touch():
    """Only the centre band counts - a ride along the upper/lower band is not an HVB."""
    from bot.indicators import TdiPoint

    def point(fast, centre, mult=1.0):
        return TdiPoint(fast=fast, slow=fast, mbl=centre, upper=centre + mult, lower=centre - mult)

    # price line leaves the upper band and comes back to it, never touching the centre
    points = [point(62, 50), point(61.5, 50), point(61, 50), point(61.9, 50)]
    assert [hvb_signal(points, i) for i in range(1, len(points))] == [None, None, None]
    # and the centre cross still fires on the same shape
    points = [point(48, 50), point(51, 50)]
    assert hvb_signal(points, 1) == "up"


def test_hvb_needs_speed_when_the_speed_gate_is_on():
    from bot.indicators import TdiPoint, line_speed

    # 20 quiet steps of 0.1 around the centre, then one big jump across it
    points = [TdiPoint(49.9 + (0.05 if i % 2 else -0.05), 50, 50, 52, 48) for i in range(20)]
    points.append(TdiPoint(52.0, 50, 50, 54, 48))
    speed = line_speed(points, len(points) - 1)
    assert speed and speed > 5
    assert hvb_signal(points, len(points) - 1, 0, "mbl", min_speed=2.0) == "up"
    quiet = [TdiPoint(49.9, 50, 50, 52, 48), TdiPoint(50.05, 50, 50, 52, 48)]
    assert hvb_signal(quiet, 1, 0, "mbl", min_speed=2.0) is None


@pytest.mark.asyncio
async def test_engine_publishes_regime_with_every_signal():
    engine = make_engine(hvb=False)
    await push_5m(engine, market_rows())
    signals = [a for a in engine.alerts if a["kind"] == "signal"]
    assert signals
    for alert in signals:
        regime = alert["regime"]
        assert regime["state"] in {"trend", "range"}
        assert regime["adx"] is not None and regime["er"] is not None
        assert regime["adx_min"] == pytest.approx(20.0)


# ------------------------------------------------- the book's confluences
def _candle(i: int, high: float, low: float, close: float | None = None) -> dict:
    return {
        "symbol": "BTCUSDT",
        "open_time": 1_700_000_000_000 + i * 300_000,
        "close_time": 1_700_000_000_000 + i * 300_000 + 299_999,
        "open": close if close is not None else (high + low) / 2,
        "high": high,
        "low": low,
        "close": close if close is not None else (high + low) / 2,
        "volume": 1.0,
        "trades": 1,
        "closed": True,
    }


def test_fvg_is_a_three_candle_gap_and_nothing_else():
    from bot.confluence import fair_value_gaps

    # bar 3's low is above bar 1's high -> bullish imbalance between them
    candles = [
        _candle(0, high=100, low=98),
        _candle(1, high=105, low=99),
        _candle(2, high=110, low=104),
        _candle(3, high=111, low=106),  # overlapping, no gap
    ]
    gaps = fair_value_gaps(candles, 0, len(candles) - 1)
    # two bullish gaps: 100-104 (bar 0 high .. bar 2 low) and 105-106
    assert [(g.side, g.bottom, g.top) for g in gaps] == [
        ("bullish", 100.0, 104.0),
        ("bullish", 105.0, 106.0),
    ]
    # price then trades back down into the gap -> it is being filled
    candles.append(_candle(4, high=107, low=102))
    assert gaps[0].filled_fraction(candles, 4) == pytest.approx(0.5)
    candles.append(_candle(5, high=103, low=97))
    assert gaps[0].filled_fraction(candles, 5) == 1.0


def test_liquidity_pool_counts_equal_extremes_not_every_candle():
    from bot.confluence import liquidity_pools, pools_ahead

    candles = [_candle(i, high=100.0, low=90.0) for i in range(3)]      # 3 equal lows
    candles += [_candle(i, high=130.0, low=120.0) for i in range(3, 6)]  # 3 equal highs
    candles += [_candle(i, high=240 + i, low=220 + i) for i in range(6, 10)]
    pools = liquidity_pools(candles, lookback=10, tolerance=0.25, atr=4.0)
    assert max(count for _, count in pools["lows"]) == 3
    assert max(count for _, count in pools["highs"]) == 3
    # a short aims down: the equal LOWS below 125 are in the way
    assert pools_ahead(pools, "SHORT", 125.0) == 3   # equal lows below the entry
    assert pools_ahead(pools, "SHORT", 80.0) == 0    # nothing left below
    assert pools_ahead(pools, "LONG", 125.0) == 3    # equal highs above it


def test_dealing_range_separates_premium_and_discount():
    from bot.confluence import dealing_range, position_in_range, zone_of

    bounds = dealing_range(120.0, 80.0)
    assert bounds == {"high": 120.0, "low": 80.0, "equilibrium": 100.0}
    assert zone_of(position_in_range(118.0, bounds)) == "premium"
    assert zone_of(position_in_range(82.0, bounds)) == "discount"
    assert zone_of(position_in_range(100.0, bounds)) == "equilibrium"
    assert position_in_range(130.0, bounds) == 1.0  # clamped


def test_failure_swing_is_a_swing_that_fails_to_extend():
    from bot.confluence import failing_swings

    prices = [100, 96, 104, 99, 108, 103, 106, 101, 109]
    candles = []
    for i, price in enumerate(prices):
        candles.append(_candle(i, high=price + 0.5, low=price - 0.5))
    swings = failing_swings(candles, pivot_len=1)
    assert swings is not None
    assert swings["kind"] in {"bullish", "bearish"}


def test_structure_labels_strong_and_weak_levels_and_the_break_kind():
    from bot.structure import Structure, DEFAULTS

    assert DEFAULTS["major_len"] == 20
    st, _pivots, _sweeps, setups, _events = run_structure(SHORT_PATTERN)
    assert setups, "the fixture has to produce a setup"
    _index, setup = setups[0]
    # p.42: the swept level caused the manipulation and the break -> STRONG;
    # the level that the reversal broke -> WEAK.
    assert setup.sweep.side == "high"  # buy-side liquidity taken = strong high
    assert setup.broke.side == "low"   # the low that failed = weak low
    # p.53: this break is a MINOR swing break, i.e. a SHIFT, not an MS
    assert setup.break_kind == "SHIFT"


def test_book_gates_reject_and_explain():
    book = {
        "break_kind": "SHIFT", "external_trend": -1, "swept_label": "strong high",
        "broke_label": "weak low", "position": 0.3, "sweep_position": 0.9,
        "zone": "discount", "sweep_zone": "premium", "fvgs_in_leg": 0, "fvg_filled": 0.0,
        "equal_highs": 2, "equal_lows": 5, "pools_ahead": 5, "structure_atr": 1.2,
        "failure_swing": None,
    }
    cand = candidate(book=book)

    assert not Filters({"structure_direction": True}).apply(cand)[0]          # against structure
    assert Filters({"structure_direction": True}).apply(
        candidate(book={**book, "external_trend": 1})
    )[0] is True  # unrelated book gates are optional unless explicitly enabled
    open_filters = {"structure_direction": False, "premium_discount": False,
                    "block_liquidity_pool": False, "require_fvg": False, "require_ms": False}
    assert Filters(open_filters).apply(candidate(book={**book, "external_trend": -1}))[0] is True

    ok, reasons = Filters({"require_ms": True}).apply(cand)
    assert not ok and any("SHIFT, not an MS" in r for r in reasons)
    ok, reasons = Filters({"require_fvg": True}).apply(cand)
    assert not ok and any("no FVG" in r for r in reasons)
    ok, reasons = Filters({"block_liquidity_pool": True, "pool_count": 3}).apply(cand)
    assert not ok and any("liquidity pool" in r for r in reasons)
    ok, reasons = Filters({"structure_atr_min": 2.0}).apply(cand)
    assert not ok and any("ATR tall" in r for r in reasons)
    # premium/discount reads the SWEEP by default and the entry when asked
    short = candidate(side="SHORT", book={**book, "sweep_position": 0.2, "sweep_zone": "discount"})
    ok, reasons = Filters({"premium_discount": True}).apply(short)
    assert not ok and any("not in premium" in r for r in reasons)
    ok, _ = Filters({"premium_discount": True, "premium_on_entry": True}).apply(short)
    assert not ok


@pytest.mark.asyncio
async def test_every_signal_carries_the_book_labels():
    engine = make_engine(hvb=False, filters={"structure_direction": False, "block_liquidity_pool": False})
    await push_5m(engine, market_rows())
    signals = [a for a in engine.alerts if a["kind"] == "signal"]
    assert signals
    book = signals[-1]["book"]
    for key in ("break_kind", "swept_label", "broke_label", "zone", "sweep_zone", "pools_ahead", "structure_atr"):
        assert key in book
    assert book["swept_label"] in {"strong high", "strong low"}
    assert book["broke_label"] in {"weak low", "weak high"}
    assert book["break_kind"] in {"SHIFT", "MS"}
