"""The book engine: the strategy of *ICT / Institutional SMC Trading*.

The old scanner asked one question per timeframe - "did this candle break
structure with momentum?" - and answered it in isolation. The book asks a
sequence of questions, in order, and only trades the last one:

    1. where is the liquidity, and which pool is the draw?      (p.5-19)
    2. which side of the dealing range are we on?               (p.49-55)
    3. has a pool just been raided and rejected?  (trap)        (p.11-13)
    4. did that print a displacement with an imbalance?         (p.67-93)
    5. has the imbalance been mitigated, and did structure turn?(p.87, p.106)
    6. is the stop behind the high-volume imbalance, and is the
       draw at least 1:4 away?                                  (p.106, p.109)

Each step is a module here; ``narrative.py`` runs the sequence and
``runner.py`` wires it to 1m candles so live and back-test share one path.

Nothing in this package reads the future: a pivot exists only after its right
side has closed, a fair value gap only after the candle *after* the displacement
has closed, and a level is only usable from the moment it became knowable.
"""

from .imbalance import Imbalance, algo_candle, candle_atr, fair_value_gap, premium_discount
from .liquidity import LiquidityMap, Pool, session_of
from .narrative import Armed, BookSettings, Narrative, Plan, Trap
from .runner import BookRunner
from .sessions import Session, cycle_index, ny_midnight, session_name, timing_tag
from .swings import Break, Pivot, Structure

__all__ = [
    "BookRunner",
    "BookSettings",
    "Break",
    "Imbalance",
    "LiquidityMap",
    "Narrative",
    "Pivot",
    "Plan",
    "Armed",
    "Pool",
    "Session",
    "Structure",
    "Trap",
    "algo_candle",
    "candle_atr",
    "cycle_index",
    "fair_value_gap",
    "ny_midnight",
    "premium_discount",
    "session_name",
    "session_of",
    "timing_tag",
]
