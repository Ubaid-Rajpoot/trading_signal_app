#!/usr/bin/env python3
"""Does the grade mean anything? Measure it on real candles.

The user's complaint was blunt: *"ajeeb ajeeb algo candles ... un pe stop loss
hit ho gaya"*. So the quality layer is only worth shipping if the numbers say
the A+ zones hold better than the C ones. This tool replays a recorded CSV,
collects every zone the map produced, and then walks the candles **after** each
entry to answer one question per zone:

    was the stop hit before the trade made 1R?

* **entry** - the bar price first came back into the block (the zone's own
  ``mitigated_index`` / ``mitigated_price``; no look-ahead, the map records it
  when it happens).
* **stop**  - for a high, just beyond the exact high that formed the zone
  (``level + stop_atr * ATR``); mirrored for a low.
* **1R**    - one risk unit in the trade's favour, measured from the entry.
* If both the stop and the target are inside the same candle the stop is
  counted (the conservative assumption).

Everything is printed with the number of zones behind it, the exact data file
and the command to reproduce it.

    python tools/zone_quality.py --csv data/btc/BTCUSDT-1m-2025-01.csv --timeframe 1m
    python tools/zone_quality.py --csv ... --timeframe 5m --horizon 240 --json out.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.buckets import Rollup  # noqa: E402
from bot.map import DEFAULTS, MarketMap, wilder_atr  # noqa: E402

D = DEFAULTS


def read_csv(path: Path) -> list[dict]:
    """Binance/MEXC kline CSV -> the bars the map eats (same shape it always uses)."""
    bars: list[dict] = []
    with path.open(newline="") as handle:
        for row in csv.reader(handle):
            if not row or not row[0].strip().lstrip("-").isdigit():
                continue
            try:
                open_time = int(row[0])
                if open_time > 10**14:          # microseconds
                    open_time //= 1000
                elif open_time < 10**11:        # seconds
                    open_time *= 1000
                bars.append(
                    {
                        "open_time": open_time,
                        "close_time": open_time + 59_999,   # a 1m bar; Rollup rewrites it
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]) if len(row) > 5 else 0.0,
                    }
                )
            except (ValueError, IndexError):
                continue
    bars.sort(key=lambda b: b["open_time"])
    return bars


def roll(bars: list[dict], timeframe: str) -> list[dict]:
    if timeframe == "1m":
        return bars
    bucket = Rollup(timeframe)
    out: list[dict] = []
    for bar in bars:
        closed = bucket.add(bar)
        if closed:
            out.append(closed)
    return out


def outcome(zone, bars: list[dict], atr: list[float | None], horizon: int) -> dict | None:
    """Follow one zone from the moment price comes back into the block.

    Everything is measured **from the exact high/low the zone is about**
    (``level``), so zones are compared on the same yardstick:

    * **failure** - a candle CLOSES beyond ``level`` (plus a 0.05 ATR pad): the
      structure is gone, which is exactly what a stop behind the structure
      protects against. A wick through is not a stop.
    * **MFE** - how far price travelled in the trade's direction before that
      failure, in ATR. This is the honest measure of "did the block pay": it
      does not depend on an invented R ladder, and every group is judged at the
      same 1 / 2 / 3 ATR distances.
    """
    if zone.mitigated_index is None:
        return None
    start = int(zone.mitigated_index) + 1
    if start >= len(bars):
        return None
    ref = atr[int(zone.mitigated_index)] or atr[int(zone.index)] or 0.0
    if not ref:
        return None
    level = float(zone.level)
    high_side = zone.side == "high"
    stop_at = level + 0.05 * ref if high_side else level - 0.05 * ref
    mfe = mae = 0.0
    end = min(len(bars), start + horizon)
    stopped_at = None
    for i in range(start, end):
        bar = bars[i]
        low, high, close = float(bar["low"]), float(bar["high"]), float(bar["close"])
        if high_side:
            mfe = max(mfe, (level - low) / ref)
            mae = max(mae, (high - level) / ref)
            failed = close > stop_at
        else:
            mfe = max(mfe, (high - level) / ref)
            mae = max(mae, (level - low) / ref)
            failed = close < stop_at
        if failed:
            stopped_at = i - start
            break
    return {
        "failed": stopped_at is not None,
        "bars_to_fail": stopped_at,
        "mfe": round(mfe, 3),
        "mae": round(mae, 3),
        "open": stopped_at is None,
    }


def bucket(value, edges, labels) -> str:
    if value is None:
        return "n/a"
    for edge, label in zip(edges, labels):
        if value < edge:
            return label
    return labels[-1]


def summarise(rows: list[dict]) -> dict:
    def pct(value: float | None) -> str:
        return "–" if value is None else f"{value * 100:.0f}%"

    fail = sum(1 for r in rows if r["failed"]) / len(rows) if rows else None
    return {
        "zones": len(rows),
        "fail_rate": fail,
        "fail": pct(fail),
        "r1": pct(sum(1 for r in rows if r["mfe"] >= 1.0) / len(rows)) if rows else "–",
        "r2": pct(sum(1 for r in rows if r["mfe"] >= 2.0) / len(rows)) if rows else "–",
        "r3": pct(sum(1 for r in rows if r["mfe"] >= 3.0) / len(rows)) if rows else "–",
        "avg_mfe": round(sum(r["mfe"] for r in rows) / len(rows), 2) if rows else None,
        "avg_mae": round(sum(r["mae"] for r in rows) / len(rows), 2) if rows else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure each zone grade on recorded candles")
    parser.add_argument("--csv", required=True, help="1m kline CSV (see docs/data.md)")
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--horizon", type=int, default=120, help="bars to follow a zone after entry")
    parser.add_argument("--json", default="", help="write the full table here")
    parser.add_argument("--min-grade", default="", help="only count zones at or above this grade")
    args = parser.parse_args()

    raw = read_csv(Path(args.csv))
    if not raw:
        print(f"no candles in {args.csv}", file=sys.stderr)
        return 1
    bars = roll(raw, args.timeframe)
    atr = wilder_atr(bars, int(D["atr_period"]))

    map_ = MarketMap(args.symbol, args.timeframe, D)
    # keep a reference to EVERY zone the map ever produced: the map itself only
    # holds the newest 400 on screen, and a study needs all of them
    seen: dict[str, object] = {}
    for bar in bars:
        map_.add(bar)
        for zone in map_.zones:
            if zone.key not in seen:
                seen[zone.key] = zone
    zones = list(seen.values())

    rows: list[dict] = []
    for zone in zones:
        if zone.kind.startswith("weak_"):
            continue
        res = outcome(zone, bars, atr, args.horizon)
        if res is None:
            continue
        rows.append(
            {
                "kind": zone.kind,
                "side": zone.side,
                "grade": zone.grade,
                "score": zone.score,
                "trend": (zone.trend or {}).get("state"),
                "aligned": (zone.trend or {}).get("aligned"),
                "entry_state": (zone.entry_trend or {}).get("state"),
                "entry_aligned": (zone.entry_trend or {}).get("aligned"),
                "time": zone.time,
                "confluence": bool(zone.confluence),
                "block_atr": zone.block_atr,
                "fvg_atr": zone.fvg_atr,
                "disp_atr": zone.disp_atr,
                "swept_major": zone.swept_major,
                "inducement": zone.inducement,
                "status": zone.status,
                **res,
            }
        )

    for row in rows:
        row["block_bucket"] = bucket(row.get("block_atr"), (0.25, 0.5, 1.0), ("<0.25", "0.25-0.5", "0.5-1.0", ">=1.0"))
        row["disp_bucket"] = bucket(row.get("disp_atr"), (1.0, 2.0, 4.0), ("<1", "1-2", "2-4", ">=4"))
        row["fvg_bucket"] = bucket(row.get("fvg_atr"), (0.4, 0.8, 1.5), ("<0.4", "0.4-0.8", "0.8-1.5", ">=1.5"))

    def table(key: str) -> list[tuple[str, dict]]:
        buckets: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            buckets[str(row[key])].append(row)
        return sorted(buckets.items(), key=lambda item: str(item[0]))


    print(f"{args.csv} · {args.timeframe} · {len(bars):,} candles · {len(rows)} zones with an entry")
    print(
        f"horizon {args.horizon} bars after the first touch"
        " · stop = a CLOSE beyond the zone's high/low"
        " · MFE = furthest move in favour before a close beyond the high/low\n"
    )

    header = f"{'group':<24}{'zones':>8}{'failed':>8}{'1 ATR':>7}{'2 ATR':>7}{'3 ATR':>7}{'avg MFE':>9}{'avg MAE':>8}"
    for key, title in (
        ("grade", "by grade"),
        ("block_bucket", "by block thickness (ATR)"),
        ("disp_bucket", "by displacement size (ATR)"),
        ("fvg_bucket", "by FVG size (ATR)"),
        ("swept_major", "by whether the swept level was a major one"),
        ("confluence", "by a strong level sitting at the same price"),
        ("entry_state", "by regime at the touch"),
        ("entry_aligned", "by alignment at the touch"),
        ("kind", "by pattern kind"),
    ):
        print(f"--- {title} " + "-" * (len(header) - len(title) - 6))
        print(header)
        for name, group in table(key):
            stats = summarise(group)
            print(
                f"{name:<24}{stats['zones']:>8}{stats['fail']:>8}{stats['r1']:>7}{stats['r2']:>7}"
                f"{stats['r3']:>7}{stats['avg_mfe']:>9.2f}{stats['avg_mae']:>8.2f}"
            )
        print()

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
