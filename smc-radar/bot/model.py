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
        """The exact feature vector the study used, in the stored order."""
        conds = zone.conds or {}
        family = _family(zone.kind)
        out: list[float] = []
        for name in self.features:
            if name == "bias":
                out.append(1.0)
            elif name == "side=high":
                out.append(1.0 if zone.side == "high" else 0.0)
            elif name in CONDITION_NAMES:
                out.append(1.0 if conds.get(name) else 0.0)
            elif name == "log(1+rr)":
                out.append(math.log1p(max(float((zone.plan or {}).get("rr") or 0.0), 0.0)))
            elif name.startswith("family="):
                out.append(1.0 if family == name.split("=", 1)[1] else 0.0)
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
    def fee_r(risk_pct: float | None, maker_bps: float = 2.0, taker_bps: float = 6.0) -> float | None:
        """The plan's round-trip cost in R (entry limit, exit limit, stop crosses)."""
        if not risk_pct or risk_pct <= 0:
            return None
        return round((maker_bps + taker_bps) / 1e4 / (risk_pct / 100.0), 3)

    def rate(self, zone, cfg: dict[str, Any]) -> None:
        """Attach probability, letter, cost and expectancy to a zone."""
        prob = self.probability(zone)
        zone.prob = round(prob, 4) if prob is not None else None
        plan = zone.plan or {}
        rr = plan.get("rr")
        risk_pct = plan.get("risk_pct")
        zone.fee_r = self.fee_r(risk_pct, float(cfg.get("fee_maker_bps", 2.0)),
                                float(cfg.get("fee_taker_bps", 6.0)))
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

    def grade_for(self, prob: float | None, cfg: dict[str, Any]) -> str:
        cuts = cfg.get("prob_cuts") or {}
        a_plus = float(cuts.get("a_plus", 0.85))
        a = float(cuts.get("a", 0.70))
        b = float(cuts.get("b", 0.55))
        if prob is None:
            return "C"
        if prob >= a_plus:
            return "A+"
        if prob >= a:
            return "A"
        if prob >= b:
            return "B"
        return "C"


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
