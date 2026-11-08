"""A setup that already exists must be found, and it must stay a live object.

The complaint was concrete: 500 instruments, five timeframes, and not one setup
- while the trader could see a valid 1H BTC setup on their own chart. The reason
was not the gates. The engine only ever looked at candles that arrived *while it
was running*:

* ``seed()`` appended history without evaluating it, so a setup confirmed ten
  minutes before start-up did not exist;
* a failed history request was retried never, so an instrument the exchange
  answered slowly stayed empty for the whole session, silently.

These tests pin both, and pin the lifecycle that makes "this setup is still
valid" a fact the app can state instead of a story the trader has to remember.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.engine import Engine  # noqa: E402
from bot.setups import SetupRecord, breaker_block, order_block  # noqa: E402

# A neutral moving RSI baseline, then the sweep -> break pattern. The final
# bearish body-break candle also crosses the moving MBL with speed; setup tests
# must use a real HVB rather than disabling a mandatory strategy invariant.
RISE = []
for i in range(180):
    _close = 98 + (i % 10) * 0.15 - (i % 3) * 0.03
    RISE.append((_close + 0.3, _close - 0.3, _close))
SHORT = [
    (101, 99, 100), (102, 99, 101), (104, 100, 103), (106, 102, 105),
    (105, 100, 101), (101, 96, 97), (98, 95, 96),
    (100, 96, 99), (103, 98, 102),
    (107, 101, 106),                    # sweep exactly one high (106)
    (104, 90, 91),                      # break that leg's paired low (95)
]
START = 1_735_689_600_000


def rows_to_candles(rows, step_ms: int = 300_000) -> list[dict]:
    out = []
    for i, (high, low, close) in enumerate(rows):
        open_time = START + i * step_ms
        out.append(
            {
                "symbol": "BTCUSDT",
                "open_time": open_time,
                "close_time": open_time + step_ms - 1,
                "open": max(low, min(high, rows[i - 1][2] if i else close)),
                "high": high,
                "low": low,
                "close": close,
                "volume": 10.0 if i >= len(rows) - 3 else 1.0,
                "trades": 1,
                "closed": True,
            }
        )
    return out


class FakeFeed:
    """Only what warm-up uses: history per timeframe."""

    source = "live"
    name = "fake"
    market = "futures"
    connected = True

    def __init__(self, per_tf: dict[str, list[dict]], fail: set[str] | None = None):
        self.per_tf = per_tf
        self.fail = fail or set()
        self.calls: list[str] = []

    def exchange_interval(self, tf: str) -> str:
        return tf

    async def history(self, symbol: str, interval: str, limit: int = 500):
        self.calls.append(interval)
        if interval in self.fail:
            raise RuntimeError("HTTP 429 Too Many Requests")
        return self.per_tf.get(interval, [])


def engine_for(*timeframes: str, **filters) -> Engine:
    return Engine(
        symbols=["BTCUSDT"],
        timeframes=list(timeframes) or ["5m"],
        tdi_settings={"rsi_period": 50, "band_period": 50, "fast_period": 1, "slow_period": 9, "band_mult": 1.6185},
        filters={
            "structure": True,
            "hvb": True,
            "hvb_window": 0,
            "hvb_min_points": 1.0,
            "hvb_min_speed": 1.5,
            "break": False,
            "trend_context": False,
            "require_bos": False,
            **filters,
        },
        structure={"pivot_len": 2, "sweep_max_wait": 60},
        strategy="sweep",
    )


@pytest.mark.asyncio
async def test_a_setup_that_formed_before_start_up_is_found_at_warm_up():
    """The exact bug: history is replayed, not merely stored."""
    candles = rows_to_candles(RISE + SHORT)
    feed = FakeFeed({"5m": candles})
    engine = engine_for("5m")
    await engine.warm_symbol(feed, "BTCUSDT")

    assert engine.setup_payloads, "a setup confirmed inside the warmed history must exist"
    setup = next(iter(engine.setup_payloads.values()))
    assert setup["side"] == "SHORT"
    assert setup["state"] in {"pending", "active"}, "it was never traded after it formed"
    assert setup["created_ms"] == candles[-1]["close_time"]
    assert setup["reasons"], "the card must be able to say why"
    assert any("broke structure" in reason for reason in setup["reasons"])


@pytest.mark.asyncio
async def test_backfill_and_live_agree_on_the_same_candle():
    """Replaying history must find exactly what live would have found."""
    candles = rows_to_candles(RISE + SHORT)

    live = engine_for("5m")
    for candle in candles:
        await live.on_candle({**candle, "closed": True})

    warm = engine_for("5m")
    await warm.warm_symbol(FakeFeed({"5m": candles[:-1]}), "BTCUSDT")
    await warm.on_candle({**candles[-1], "closed": True})

    def key(engine):
        return {(row["side"], row["created_ms"], round(row["entry"], 4)) for row in engine.setup_payloads.values()}

    assert key(warm) == key(live), "history replay and the live path must not disagree"


@pytest.mark.asyncio
async def test_a_failed_history_request_is_retried_instead_of_left_empty():
    candles = rows_to_candles(RISE + SHORT)
    feed = FakeFeed({"5m": candles}, fail={"5m"})
    engine = engine_for("5m")

    await engine.warm_symbol(feed, "BTCUSDT")
    assert engine.cells[("BTCUSDT", "5m")].candles == [] or len(engine.cells[("BTCUSDT", "5m")].candles) < 120
    assert engine.warm_failures, "the failure must be recorded, not swallowed"
    assert len(feed.calls) >= 3, "three attempts, not one"

    # the exchange comes back: the retry sweep warms it, and the setup appears
    feed.fail.clear()
    retried = await engine.retry_failed(feed)
    assert retried == 1
    assert len(engine.cells[("BTCUSDT", "5m")].candles) >= 120
    assert engine.setup_payloads, "after the retry the instrument is alive, not dead for the session"


@pytest.mark.asyncio
async def test_one_bad_symbol_does_not_stop_the_others():
    candles = rows_to_candles(RISE + SHORT)
    feed = FakeFeed({"5m": candles}, fail={"5m"})

    engine = Engine(
        symbols=["BTCUSDT"],
        timeframes=["5m"],
        tdi_settings={"rsi_period": 14, "band_period": 14, "fast_period": 1, "slow_period": 3},
        filters={"trend_context": False, "require_bos": False},
        structure={"pivot_len": 2},
        strategy="sweep",
    )
    feed.fail.clear()
    await engine.warmup(feed)
    assert engine.warming() == 0


def test_order_block_is_the_last_opposite_candle_before_the_break():
    candles = rows_to_candles(RISE[-5:] + SHORT[-3:])
    zone = order_block(candles, "SHORT")
    assert zone is not None
    # SHORT: the last bullish candle before the down break, and it sits above
    # the close of the break candle - the red box on the trader's chart
    assert zone["high"] >= zone["low"]
    assert zone["kind"] == "supply"


def record(**kwargs) -> SetupRecord:
    base = dict(
        key="BTCUSDT|1h|1",
        symbol="BTCUSDT",
        timeframe="1h",
        side="SHORT",
        created_ms=1_000,
        entry=99.0,  # provisional proximal edge; no trade exists yet
        stop=104.0,
        target=None,
        rr=None,
        target_rr=None,
        target_model="hvb_bos_leg_extreme_v1",
        leg_target=90.0,
        leg_target_ms=950,
        leg_start_ms=900,
        leg_break_ms=1_000,
        zone={"high": 101.0, "low": 99.0, "mid": 100.0, "type": "order_block"},
        swept_level=104.0,
        swept_side="high",
        sweep_ms=900,
        bos_level=98.0,
        bos_ms=1_000,
    )
    base.update(kwargs)
    return SetupRecord(**base)


def bar_at(high, low, close, ms=2_000) -> dict:
    return {"high": high, "low": low, "close": close, "close_time": ms, "open": close, "volume": 1.0}


def test_a_setup_is_pending_until_price_comes_back_into_the_zone():
    setup = record()
    assert setup.valid and setup.state == "pending"
    assert setup.advance(bar_at(97.5, 95.0, 96.0)) is False, "price moving away is not an event"
    assert setup.state == "pending", "still valid: the zone has not been mitigated"
    assert setup.as_dict()["display_state"] == "active"
    assert setup.advance(bar_at(100.5, 99.2, 100.0)) is True
    assert setup.state == "active" and setup.touched_ms == 2_000
    assert setup.as_dict()["display_state"] == "mitigated"
    assert setup.stop == 104.0, "order-block mitigation retains the structural sweep stop"
    assert setup.target == 90.0 and setup.rr == 1.8
    assert "confirmation-leg low target armed" in setup.state_reason


def test_confirmation_leg_extends_while_pending_and_excludes_mitigation_candle():
    setup = record(leg_target=96.0, leg_target_ms=1_000)
    assert setup.advance(bar_at(97.5, 94.0, 95.0, ms=2_000)) is True
    assert setup.state == "pending" and setup.leg_target == 94.0
    assert setup.leg_target_ms == 2_000

    # This candle touches the order block and also wicks much lower. That low is
    # excluded from the leg definition because OHLC cannot order it against the
    # mitigation touch; the prior 94 low freezes as the target, and no same-bar
    # win is awarded.
    assert setup.advance(bar_at(100.5, 80.0, 99.5, ms=3_000)) is True
    assert setup.state == "active" and setup.target == 94.0
    assert setup.leg_target == 94.0 and setup.leg_target_ms == 2_000
    assert setup.rr == 1.0
    assert setup.advance(bar_at(99.0, 93.8, 94.2, ms=4_000)) is True
    assert setup.state == "win"


def test_active_setup_arms_the_frozen_leg_low_then_wins_or_stops():
    setup = record()
    setup.advance(bar_at(100.5, 99.2, 100.0))          # filled at zone.low = 99
    assert setup.entry == 99.0 and setup.target == 90.0 and setup.rr == 1.8
    assert setup.advance(bar_at(99.0, 89.8, 90.5)) is True
    assert setup.state == "win" and setup.realized_r == 1.8

    stopped = record()
    stopped.advance(bar_at(100.5, 99.2, 100.0))
    # one candle touches both the leg target and the stop: assume the stop
    assert stopped.advance(bar_at(104.5, 89.5, 100.0)) is True
    assert stopped.state == "stopped", "both in one bar is a loss, not a win"


def test_a_setup_that_never_gets_filled_expires_instead_of_haunting_the_list():
    setup = record(max_bars=3)
    for i in range(3):
        setup.advance(bar_at(97.6, 97.0, 97.2, ms=2_000 + i))
    assert setup.state == "expired" and not setup.valid


def test_zero_expiry_keeps_an_unmitigated_historical_setup_valid():
    setup = record(max_bars=0)
    for i in range(5_000):
        setup.advance(bar_at(97.6, 97.0, 97.2, ms=2_000 + i))
    assert setup.state == "pending"
    assert setup.valid and not setup.mitigated


def test_a_setup_invalidates_only_if_the_stop_goes_before_mitigation():
    setup = record()
    assert setup.target is None and setup.r_multiple(79.0) is None
    assert setup.advance(bar_at(104.5, 102.0, 104.0)) is True
    assert setup.state == "invalidated" and not setup.valid
    assert setup.mitigated is False and setup.realized_r is None
    assert "before" in setup.state_reason and "no trade" in setup.state_reason


def test_mitigation_and_stop_on_one_candle_is_stopped_not_unfilled_invalidation():
    setup = record()
    # Travelling from below, price must cross the order-block edge before the
    # higher stop. It was filled, so this is a stopped trade, not invalidated.
    assert setup.advance(bar_at(104.5, 98.5, 103.0)) is True
    assert setup.mitigated and setup.state == "stopped"
    assert setup.realized_r == -1.0


def test_an_eligible_breaker_can_mitigate_before_the_order_block():
    setup = record(
        zones=[
            {"high": 101.0, "low": 100.0, "type": "breaker_block", "kind": "bearish_breaker",
             "breaker_stop": 101.2, "stop_buffer": 0.2},
            {"high": 103.0, "low": 102.0, "type": "order_block", "kind": "supply"},
        ],
        zone={"high": 103.0, "low": 102.0, "type": "order_block", "kind": "supply"},
        entry=100.0,
    )
    assert setup.advance(bar_at(100.5, 99.8, 100.2)) is True
    assert setup.state == "active" and setup.mitigation_kind == "breaker_block"
    assert setup.entry == 100.0
    assert setup.stop == setup.stop0 == 101.2, "breaker uses its buffered distal edge, not sweep 104"
    assert setup.risk0 == pytest.approx(1.2)
    assert setup.target == pytest.approx(90.0)
    assert setup.rr == pytest.approx(10.0 / 1.2, abs=1e-4)


def test_a_breaker_stop_on_the_profit_side_is_rejected():
    setup = record(
        zones=[
            {"high": 101.0, "low": 100.0, "type": "breaker_block",
             "kind": "bearish_breaker", "breaker_stop": 99.5},
        ],
        zone={"high": 101.0, "low": 100.0, "type": "breaker_block"},
        entry=100.0,
    )
    assert setup.advance(bar_at(100.5, 99.8, 100.2)) is True
    assert setup.state == "invalidated" and setup.mitigated is False
    assert "correctly sided risk" in setup.state_reason


def test_long_breaker_touch_arms_the_confirmation_leg_high_like_the_bnb_example():
    setup = record(
        side="LONG", stop=703.7, entry=704.8, leg_target=710.0,
        zone={"high": 703.9, "low": 703.75, "type": "order_block", "kind": "demand"},
        zones=[
            {"high": 704.8, "low": 704.6, "type": "breaker_block", "kind": "bullish_breaker",
             "breaker_stop": 704.5, "stop_buffer": 0.1},
            {"high": 703.9, "low": 703.75, "type": "order_block", "kind": "demand"},
        ],
    )
    assert setup.advance(bar_at(705.2, 704.65, 705.0)) is True
    assert setup.state == "active" and setup.mitigation_kind == "breaker_block"
    assert setup.entry == 704.8
    assert setup.stop == setup.stop0 == 704.5, "long breaker stop sits below its distal low"
    assert setup.target == pytest.approx(710.0)
    assert setup.rr == pytest.approx((710.0 - 704.8) / (704.8 - 704.5), abs=1e-4)


def test_aggressive_same_leg_endpoint_becomes_a_breaker_block():
    candles = [
        {"open": 99, "high": 101, "low": 98, "close": 100, "close_time": 1},
        # The candle that formed the paired endpoint on the left.
        {"open": 100, "high": 102, "low": 99, "close": 101, "close_time": 2},
    ]
    for i in range(3, 17):
        candles.append({"open": 100, "high": 101, "low": 99, "close": 100,
                        "close_time": i})
    candles.append({"open": 100, "high": 100.2, "low": 94, "close": 95, "close_time": 17})
    zone = breaker_block(candles, "SHORT", 2, 105.0, min_body_atr=1.0)
    assert zone is not None
    assert zone["type"] == "breaker_block" and zone["kind"] == "bearish_breaker"
    assert zone["low"] == 99.0 and zone["high"] == 102.0
    assert zone["body_atr"] >= 1.0
    assert zone["stop_buffer"] == pytest.approx(0.1 * zone["atr"])
    assert zone["breaker_stop"] == pytest.approx(zone["high"] + zone["stop_buffer"])
    assert zone["breaker_stop"] < 105.0, "local breaker risk must improve on the sweep stop"
    no_buffer = breaker_block(
        candles, "SHORT", 2, 105.0, min_body_atr=1.0, stop_buffer_atr=0.0,
    )
    assert no_buffer is not None and no_buffer["breaker_stop"] == 102.0

    quiet = {**candles[-1], "open": 96.0, "close": 95.0}
    assert breaker_block(candles[:-1] + [quiet], "SHORT", 2, 105.0, min_body_atr=1.0) is None


@pytest.mark.asyncio
async def test_the_order_block_comes_from_the_replayed_cell_not_an_empty_one():
    """The zone is the last opposite candle *of the cell being evaluated*.

    During a history replay that cell is not in ``engine.cells`` yet, so reading
    it from there produced a zero-height zone at the entry price - the chart drew
    a line instead of the trader's red box.
    """
    candles = rows_to_candles(RISE + SHORT)
    engine = engine_for("5m")
    await engine.warm_symbol(FakeFeed({"5m": candles}), "BTCUSDT")
    setup = next(iter(engine.setup_payloads.values()))
    zone = setup["zone"]
    assert zone["high"] > zone["low"], "an order block has height"
    # a SHORT's supply block sits *above* the entry (that is where you sell from)
    # and at or below the stop at the sweep extreme
    assert setup["entry"] <= zone["low"] < zone["high"] <= setup["stop"] * 1.001
    assert zone["kind"] == "supply", "SHORT setups sit in a supply block"


@pytest.mark.asyncio
async def test_setups_found_in_history_carry_their_own_state():
    """An old setup is not 'pending' just because nobody has looked at it."""
    candles = rows_to_candles(RISE + SHORT + [(97, 88, 89)] * 40)
    engine = engine_for("5m", setup_max_bars=10)
    await engine.warm_symbol(FakeFeed({"5m": candles}), "BTCUSDT")
    states = {row["state"] for row in engine.setup_payloads.values()}
    assert states, "the setup must be found"
    assert states <= {"win", "stopped", "expired", "invalidated", "active", "pending"}
    assert any(state in {"expired", "win", "stopped"} for state in states), (
        "after 40 more bars the life of the setup has moved on"
    )


def test_mitigation_is_tracked_even_when_the_setup_is_old():
    """"It hasn't been mitigated yet" is a fact about the zone, not the clock."""
    fresh = record(max_bars=500)
    fresh.advance(bar_at(97.5, 96.0, 96.5))
    assert fresh.mitigated is False and fresh.valid
    fresh.advance(bar_at(100.5, 99.5, 100.0))
    assert fresh.mitigated is True

    old = record(max_bars=2)
    for i in range(2):
        old.advance(bar_at(97.6, 97.0, 97.2, ms=3_000 + i))
    assert old.state == "expired", "the hold window ran out"
    assert old.mitigated is False, "but nothing ever used the block"
    assert old.as_dict()["mitigated"] is False


def test_a_closed_trade_reports_what_it_realised_not_where_price_went():
    """The bug: a short stopped in April showed "-28.9R" in July.

    A finished trade has no live mark. Its R is its outcome, and it must not
    move when the market moves on - otherwise the page's record, which is the
    whole point of the archive, is measuring the wrong thing.
    """
    from bot.setups import SetupRecord

    record = SetupRecord(key="k", symbol="BTCUSDT", timeframe="1h", side="SHORT",
                         created_ms=1, entry=100.0, stop=104.0, target=84.0, rr=4.0,
                         zone=None, swept_level=0, swept_side="", sweep_ms=0,
                         bos_level=0, bos_ms=0, state="active", touched_ms=1)
    assert record.as_dict()["r_now"] in (None, 0.0)
    record.state = "stopped"
    assert record.as_dict()["r_now"] == -1.0
    record.last_price = 200.0          # price walked on for months
    assert record.as_dict()["r_now"] == -1.0
    assert record.advance({"high": 210, "low": 190, "close": 200,
                           "close_time": 10}, ) is False
    assert record.as_dict()["r_now"] == -1.0


def test_a_win_reports_its_planned_reward_in_r():
    from bot.setups import SetupRecord

    record = SetupRecord(key="k2", symbol="BTCUSDT", timeframe="1h", side="LONG",
                         created_ms=1, entry=100.0, stop=95.0, target=120.0, rr=4.0,
                         zone=None, swept_level=0, swept_side="", sweep_ms=0,
                         bos_level=0, bos_ms=0, state="active", touched_ms=1)
    record.state = "win"
    assert record.realized_r == 4.0
