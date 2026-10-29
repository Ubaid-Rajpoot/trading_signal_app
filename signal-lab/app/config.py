"""Settings: `config.json`, with the command line winning over the file.

The user's complaint was concrete: too many symbols on the command line and the
program hangs. Two things fix that.

1. A config file, so a long universe is data rather than argv.
2. Bounded work: the universe is capped, requests go through a thread pool with a
   timeout, and a symbol that fails is skipped and reported - never a hang.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_PATH = os.path.join(ROOT, "config.json")

ALL_TFS = ("1m", "3m", "5m", "10m", "15m", "30m", "45m", "1h", "2h", "3h", "4h")


@dataclass
class Config:
    mode: str = "live"
    port: int = 8811
    host: str = "0.0.0.0"
    symbols: list = field(default_factory=list)
    auto_top: int = 120
    timeframes: list = field(default_factory=lambda: list(ALL_TFS))
    scan_bars: int = 40
    poll_seconds: float = 60.0
    workers: int = 12
    timeout_seconds: float = 20.0
    fee_bp: float = 5.0
    cut_override: float | None = None
    risk_per_trade_pct: float = 1.0
    equity_usd: float = 1000.0
    max_candidates: int = 40
    db: str = "app/data/signals.db"
    path: str = DEFAULT_PATH

    @property
    def symbols_final(self) -> list[str]:
        """Explicit list, de-duplicated and capped; empty means 'ask the exchange'."""
        out, seen = [], set()
        for s in self.symbols:
            k = str(s).strip().upper()
            if k and k not in seen:
                seen.add(k)
                out.append(k)
        return out[: self.auto_top if self.symbols else 0] or out

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("path", None)
        d["symbols"] = self.symbols_final
        return d


def _coerce(d: dict) -> dict:
    """Ignore unknown keys, coerce numbers, and keep the file forgiving."""
    out = {}
    for k, v in d.items():
        if k.startswith("_") or k not in Config.__dataclass_fields__:
            continue
        out[k] = v
    for num in ("port", "auto_top", "scan_bars", "workers", "max_candidates"):
        if num in out and out[num] is not None:
            out[num] = int(out[num])
    for fl in ("poll_seconds", "timeout_seconds", "fee_bp", "risk_per_trade_pct", "equity_usd"):
        if fl in out and out[fl] is not None:
            out[fl] = float(out[fl])
    if out.get("cut_override") in ("", "null", 0):
        out["cut_override"] = None
    if "timeframes" in out:
        out["timeframes"] = [t for t in out["timeframes"] if t in ALL_TFS]
    return out


def load(path: str | None = None) -> Config:
    p = path or os.environ.get("SIGNAL_LAB_CONFIG") or DEFAULT_PATH
    data: dict = {}
    if p and os.path.exists(p):
        with open(p) as fh:
            data = _coerce(json.load(fh))
    cfg = Config(**data)
    cfg.path = p or ""
    if not cfg.timeframes:
        cfg.timeframes = list(ALL_TFS)
    return cfg


def save(cfg: Config, path: str | None = None) -> str:
    p = path or cfg.path or DEFAULT_PATH
    with open(cfg.path or DEFAULT_PATH) as fh:
        current = json.load(fh)  # keep the _comment block and any hand edits
    current.update({k: v for k, v in asdict(cfg).items() if k not in ("path", "symbols")})
    current["symbols"] = cfg.symbols
    with open(p, "w") as fh:
        json.dump(current, fh, indent=2)
    return p
