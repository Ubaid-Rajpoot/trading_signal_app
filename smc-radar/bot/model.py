"""The measured probability behind every grade.

``tools/fit_model.py`` fits a small logistic model on real recorded candles and
writes ``signal_model.json`` next to this file. This module is the runtime side:
it turns a zone's formation-time facts into *P(the plan's target is reached
before its stop)* and turns that probability into the letter the card shows.

Three things matter here and are deliberately explicit:

* **Nothing is invented.** The features are the book's own conditions (grab, MS,
  major level, inducement, algo candle, displacement, order block, session,
  confluence, trend), the pattern family, the side, and the plan's own R:R - all
  known the moment the zone forms. No future candle is used.
* **The letter means a measured probability**, not a count of conditions. The
  count is shown next to it (``6/10 conditions``) because that is what the book
  talks about, but the measurement says a count alone does not rank outcomes
  (``docs/conditions.md``), so the letter is not allowed to pretend otherwise.
* **Costs are part of the plan.** A stop that is smaller than the round-trip fee
  cannot be traded however good the pattern is, so every zone also carries the
  fee expressed in R and a ``tradable`` flag.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .map import CONDITION_NAMES

MODEL_PATH = Path(__file__).with_name("signal_model.json")

# The timeframes the model was fitted on. A timeframe it has never seen (3m, 1h)
# is scored as the 1m baseline rather than pretending to know better.
KNOWN_TIMEFRAMES = ("1m", "5m", "15m", "30m")


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


class SignalModel:
    """The shipped weights, plus the grading and costing rules around them."""

    def __init__(self, path: str | Path = MODEL_PATH) -> None:
        self.path = Path(path)
        self.available = False
        self.problem: str | None = None
        self.weights: list[float] = []
        self.features: list[str] = []
        self.meta: dict[str, Any] = {}
        try:
            data = json.loads(self.path.read_text())
            self.features = list(data["features"])
            self.weights = [float(w) for w in data["weights"]]
            self.meta = data
            self.available = len(self.features) == len(self.weights)
            if not self.available:
                self.problem = "feature/weight length mismatch"
        except FileNotFoundError:
            self.problem = f"{self.path.name} is missing - run tools/fit_model.py"
        except Exception as exc:  # a broken model must never stop the scanner
            self.problem = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ scoring
    def vector(self, zone) -> list[float]:
        """The exact feature vector the study used, in the stored order.

        Round 8 added the plan's own geometry (``log(risk_pct)``, ``block_atr``)
        and the timeframe, because the measurement showed the same pattern is a
        different trade depending on how much room the plan gives it.
        """
        conds = zone.conds or {}
        family = _family(zone.kind)
        plan = zone.plan or {}
        risk_pct = float(plan.get("risk_pct") or 0.0)
        out: list[float] = []
        for name in self.features:
            if name == "bias":
                out.append(1.0)
            elif name == "side=high":
                out.append(1.0 if zone.side == "high" else 0.0)
            elif name in CONDITION_NAMES:
                out.append(1.0 if conds.get(name) else 0.0)
            elif name == "log(1+rr)":
                out.append(math.log1p(max(float(plan.get("rr") or 0.0), 0.0)))
            elif name == "log(risk_pct)":
                out.append(math.log(max(risk_pct, 1e-6)))
            elif name == "block_atr":
                out.append(min(float(zone.block_atr or 0.0), 3.0))
            elif name.startswith("family="):
                out.append(1.0 if family == name.split("=", 1)[1] else 0.0)
            elif name.startswith("tf="):
                out.append(1.0 if getattr(zone, "timeframe", "") == name.split("=", 1)[1] else 0.0)
            else:  # an unknown feature would silently mis-score everything
                out.append(0.0)
        return out

    def probability(self, zone) -> float | None:
        if not self.available:
            return None
        vec = self.vector(zone)
        return _sigmoid(sum(w * x for w, x in zip(self.weights, vec)))

    # ------------------------------------------------------------- costing
    @staticmethod
    def fee_r(risk_pct: float | None, maker_bps: float = 2.0, taker_bps: float = 6.0,
              prob: float | None = None) -> float | None:
        """The plan's round-trip cost in R.

        A winner is a limit on both sides (maker in, maker out); a loser crosses
        the spread on the stop (maker in, taker out). Given a probability, the
        cost reported is the *expected* one - which is the number that belongs in
        an expectancy, not the worst case.
        """
        if not risk_pct or risk_pct <= 0:
            return None
        win_bps = maker_bps + maker_bps
        loss_bps = maker_bps + taker_bps
        # without a probability, report the worst case (the stop crosses the
        # spread) - an unknown plan must never be costed optimistically
        bps = loss_bps if prob is None else prob * win_bps + (1.0 - prob) * loss_bps
        return round(bps / 1e4 / (risk_pct / 100.0), 3)

    def rate(self, zone, cfg: dict[str, Any]) -> None:
        """Attach probability, letter, cost and expectancy to a zone."""
        prob = self.probability(zone)
        zone.prob = round(prob, 4) if prob is not None else None
        plan = zone.plan or {}
        rr = plan.get("rr")
        risk_pct = plan.get("risk_pct")
        zone.fee_r = self.fee_r(risk_pct, float(cfg.get("fee_maker_bps", 2.0)),
                                float(cfg.get("fee_taker_bps", 6.0)), prob)
        if prob is not None and rr:
            gross = prob * float(rr) - (1.0 - prob)
            zone.net_r = round(gross - (zone.fee_r or 0.0), 3)
        else:
            zone.net_r = None
        limit = float(cfg.get("fee_max_r", 0.30))
        zone.tradable = bool(
            prob is not None and rr and zone.net_r is not None
            and zone.fee_r is not None and zone.fee_r <= limit and zone.net_r > 0
        )
        zone.grade = self.grade_for(prob, cfg)
        # a signal is a *measured* setup the fee also leaves worth taking
        # Round 13: the measured probability says how likely the plan is to pay; the
        # structure gate says whether this is a market the plan should be taken in at
        # all. Both have to agree - "a 96 % pattern in a range" is exactly the trade
        # that took the user's account, because a range is where the book says the
        # algorithm is building liquidity, not delivering it (p.58).
        # the rule can be switched off in the settings (``structure_gate: false``),
        # because it costs signals - the measurement is in docs/round-13
        gate_ok = not bool(cfg.get("structure_gate", True)) or bool((zone.gate or {}).get("ok", False))
        zone.signal = bool(
            zone.tradable and prob is not None
            and prob >= float(cfg.get("signal_prob_min", 0.85))
            and gate_ok
        )
        zone.measured = self.band(prob)

    def grade_for(self, prob: float | None, cfg: dict[str, Any]) -> str:
        cuts = cfg.get("prob_cuts") or {}
        a_plus = float(cuts.get("a_plus", 0.90))
        a = float(cuts.get("a", 0.85))
        b = float(cuts.get("b", 0.75))
        c = float(cuts.get("c", 0.60))
        if prob is None:
            return "-"
        if prob >= a_plus:
            return "A+"
        if prob >= a:
            return "A"
        if prob >= b:
            return "B"
        if prob >= c:
            return "C"
        return "-"

    def band(self, prob: float | None) -> dict[str, Any] | None:
        """The measured accuracy the letter stands for, from the model file.

        ``{"letter", "cut", "win", "n"}`` - so a card can print "97% (331/341
        out of sample)" instead of a bare promise. The bands are written by
        ``tools/fit_model.py`` from the held-out block, not typed in by hand.
        """
        if prob is None:
            return None
        table = self.meta.get("bands") or []
        chosen = None
        for entry in sorted(table, key=lambda e: float(e.get("cut") or 0.0)):
            if prob >= float(entry.get("cut") or 0.0):
                chosen = entry
        return dict(chosen) if chosen else None


def _family(kind: str) -> str:
    if kind.startswith("very_strong_algo"):
        return "vs_algo"
    if kind.startswith("rejection_block"):
        return "rej"
    if kind.startswith("very_strong"):
        return "strong"
    if kind.startswith("strong"):
        return "strong"
    if kind.startswith("algo"):
        return "algo"
    return "other"


_MODEL: SignalModel | None = None


def model() -> SignalModel:
    """One shared model object - the file is read once, not once per zone."""
    global _MODEL
    if _MODEL is None:
        _MODEL = SignalModel()
    return _MODEL
