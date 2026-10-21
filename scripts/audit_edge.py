#!/usr/bin/env python3
"""Does a rule actually have an edge? Ask the data, not the story.

This walks real candles once per variant and prints trade count, win rate,
profit factor and expectancy per trade - split into a first half and a second
half, because a rule that only works in one half is luck, not edge.

    # the audit that decided v3's honest verdict, on a real dataset
    python scripts/audit_edge.py --symbol BTCUSDT --timeframes 5m,15m

    # a specific entry/exit pair
    python scripts/audit_edge.py --entry tdi_break --exit tdi_cross

Entry variants:
  tdi_break     HVB + same-candle structure break (v1's rule)
  struct_trend  structure break with the external trend, no TDI
  struct_vol    the above + a volume spike on the break
  struct_tdi    the above + the purple line on the correct side of the base line

Exit variants:
  tdi_cross     close when the purple line crosses back through the base line
  struct_inval  close when price closes back through the broken structure
  either        whichever comes first
  bars:N        close after N bars (default 20)

Fees: 5 bps + 2 bps slippage per side (14 bps round trip).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import TIMEFRAMES  # noqa: E402
from app.core import market_structure as ms  # noqa: E402
from app.core.data.history import get_history  # noqa: E402
from app.core.indicators import crossed_above, crossed_below  # noqa: E402
from app.core.strategies import get_strategy  # noqa: E402

COST = (5 + 2) * 2 / 10_000  # round trip


def stats(df: pd.DataFrame, el, es, xl, xs) -> dict:
    close = df["close"].to_numpy()
    el, es, xl, xs = (s.to_numpy() for s in (el, es, xl, xs))
    pos, entry, out = 0, 0.0, []
    for i in range(len(close)):
        if pos == 0:
            if el[i]:
                pos, entry = 1, close[i]
            elif es[i]:
                pos, entry = -1, close[i]
        elif (pos == 1 and xl[i]) or (pos == -1 and xs[i]):
            out.append(((close[i] / entry - 1) if pos == 1 else (1 - close[i] / entry)) - COST)
            pos = 0
    if not out:
        return {"trades": 0}
    t = np.array(out)
    wins = t > 0
    gross_win = t[wins].sum()
    gross_loss = abs(t[~wins].sum())
    return {
        "trades": int(len(t)),
        "win_pct": round(100 * wins.mean(), 1),
        "pf": round(gross_win / gross_loss, 2) if gross_loss else None,
        "expectancy_pct": round(100 * t.mean(), 3),
        "total_pct": round(100 * t.sum(), 1),
    }


def build(prepared: pd.DataFrame, entry: str) -> tuple[pd.Series, pd.Series]:
    up = prepared["trend_dir"].fillna(0) > 0
    down = prepared["trend_dir"].fillna(0) < 0
    vol = (prepared["volume"] / prepared["volume"].rolling(20).mean()) >= 1.5
    tdi_up = prepared["tdi_fast"] > prepared["tdi_mbl"]

    if entry == "tdi_break":
        strategy = get_strategy("tdi_shift_v1")
        return strategy.rules(prepared, strategy.resolve_params({})).long_entry, strategy.rules(
            prepared, strategy.resolve_params({})
        ).short_entry
    if entry == "struct_trend":
        return prepared["shift_up"] & up, prepared["shift_down"] & down
    if entry == "struct_vol":
        return prepared["shift_up"] & up & vol, prepared["shift_down"] & down & vol
    if entry == "struct_tdi":
        return prepared["shift_up"] & up & tdi_up, prepared["shift_down"] & down & ~tdi_up
    raise SystemExit(f"unknown entry variant '{entry}'")


def build_exit(prepared: pd.DataFrame, exit_name: str) -> tuple[pd.Series, pd.Series]:
    if exit_name == "tdi_cross":
        return (
            crossed_below(prepared["tdi_fast"], prepared["tdi_mbl"]),
            crossed_above(prepared["tdi_fast"], prepared["tdi_mbl"]),
        )
    if exit_name == "struct_inval":
        return prepared["close"] < prepared["sw_lo_minor"], prepared["close"] > prepared["sw_hi_minor"]
    if exit_name == "either":
        a, b = build_exit(prepared, "tdi_cross")
        c, d = build_exit(prepared, "struct_inval")
        return a | c, b | d
    if exit_name.startswith("bars:"):
        n = int(exit_name.split(":")[1])
        idx = pd.Series(False, index=prepared.index)
        idx.iloc[n::n] = True  # a timed grid, not a per-trade timer
        return idx, idx
    raise SystemExit(f"unknown exit variant '{exit_name}'")


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure whether an entry/exit rule has an edge")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframes", default="5m,15m")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--strategy", default="tdi_shift_v1", help="used for indicator prep")
    ap.add_argument("--entries", default="tdi_break,struct_trend,struct_vol,struct_tdi")
    ap.add_argument("--exits", default="tdi_cross,struct_inval")
    ap.add_argument("--out", default="", help="write a markdown report here")
    args = ap.parse_args()

    strategy = get_strategy(args.strategy)
    params = strategy.resolve_params({})
    lines: list[str] = []
    rows: list[dict] = []

    for tf in [t.strip() for t in args.timeframes.split(",") if t.strip()]:
        if tf not in TIMEFRAMES:
            print(f"skip unknown timeframe {tf}", file=sys.stderr)
            continue
        frame, source = get_history(args.symbol, tf, args.days, allow_demo=False)
        prepared = strategy.prepare(frame, params)
        prepared = ms.regime_frame(
            prepared,
            minor_k=int(params["swing_minor"]),
            major_k=int(params["swing_major"]),
            atr_len=int(params["atr_len"]),
        )
        half = len(prepared) // 2
        print(f"\n=== {args.symbol} {tf} · {len(prepared)} bars · source={source} ===")
        for entry in [e.strip() for e in args.entries.split(",") if e.strip()]:
            el, es = build(prepared, entry)
            for exit_name in [x.strip() for x in args.exits.split(",") if x.strip()]:
                xl, xs = build_exit(prepared, exit_name)
                full = stats(prepared, el, es, xl, xs)
                h1 = stats(
                    prepared.iloc[:half].reset_index(drop=True),
                    el.iloc[:half].reset_index(drop=True),
                    es.iloc[:half].reset_index(drop=True),
                    xl.iloc[:half].reset_index(drop=True),
                    xs.iloc[:half].reset_index(drop=True),
                )
                h2 = stats(
                    prepared.iloc[half:].reset_index(drop=True),
                    el.iloc[half:].reset_index(drop=True),
                    es.iloc[half:].reset_index(drop=True),
                    xl.iloc[half:].reset_index(drop=True),
                    xs.iloc[half:].reset_index(drop=True),
                )
                print(f"  {entry:<13} {exit_name:<13} {json.dumps(full)}")
                print(f"  {'':<27} h1 {json.dumps(h1)}  h2 {json.dumps(h2)}")
                rows.append(
                    {"timeframe": tf, "entry": entry, "exit": exit_name, "full": full, "h1": h1, "h2": h2}
                )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines += [
            f"# Edge audit — {args.symbol}",
            "",
            "Every variant walks the same real candles once. `pf` is gross win / gross loss,",
            "`exp` is the average return per trade after 14 bps round-trip costs.",
            "A rule with an edge stays above pf 1 in **both** halves.",
            "",
            "| TF | entry | exit | trades | win % | PF | exp % | h1 PF | h2 PF |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            f, h1, h2 = r["full"], r["h1"], r["h2"]
            lines.append(
                f"| {r['timeframe']} | {r['entry']} | {r['exit']} | {f.get('trades', 0)} | "
                f"{f.get('win_pct', '-')} | {f.get('pf', '-')} | {f.get('expectancy_pct', '-')} | "
                f"{h1.get('pf', '-')} | {h2.get('pf', '-')} |"
            )
        out.write_text("\n".join(lines) + "\n")
        print(f"\nmarkdown written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
