"""Turn daily Binance-style 1m CSVs into the files the app replays.

The app expects ``open_time,open,high,low,close,volume`` with a millisecond
``open_time`` (see ``bot/feed.py``). The public daily files use microseconds and
carry extra columns, so they are normalised here once instead of at every read.

    python tools/make_data.py --source /tmp/TradinBot-main/temp_data

It writes:

* ``data/fut/BTC_USDT-1m-<year>-<month>.csv``  - one file per month, named like
  the futures contract the app trades (round 9: futures only)
* ``data/btc/BTCUSDT-1m-<year>-<month>.csv``   - the same candles as the spot
  pair, kept only for the studies that were written before the split
* ``study-data/BTCUSDT-1m-90d.csv``            - Jan-Mar (training block)
* ``study-data/BTCUSDT-1m-90d-holdout.csv``    - Apr-Jun (held-out block)

Nothing is downloaded here: give it a folder of real recorded candles.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# The app's reader (bot/feed.py) treats every line as a candle, exactly like the
# exchange's own kline CSV: no header row.
TRAIN_MONTHS = {"2025-01", "2025-02", "2025-03"}


def rows(path: Path):
    with path.open(newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 6:
                continue
            try:
                raw = int(row[0])
            except ValueError:
                continue          # a header line
            if raw > 10**14:
                raw //= 1000      # microseconds -> milliseconds
            elif raw < 10**12:
                raw *= 1000       # seconds -> milliseconds
            yield (raw, row[1], row[2], row[3], row[4], row[5])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="/tmp/TradinBot-main/temp_data")
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    source, root = Path(args.source), Path(args.root)
    files = sorted(source.glob("*BTCUSDT-1m-*.csv"))
    if not files:
        print(f"no daily CSVs in {source}", file=sys.stderr)
        return 1

    month_files: dict[str, list[Path]] = {}
    for path in files:
        month = "-".join(path.stem.split("-")[-3:-1])
        month_files.setdefault(month, []).append(path)

    for month, paths in sorted(month_files.items()):
        fut = root / "data" / "fut" / f"BTC_USDT-1m-{month}.csv"
        fut.parent.mkdir(parents=True, exist_ok=True)
        with fut.open("w", newline="") as handle:
            writer = csv.writer(handle)
            for path in paths:
                writer.writerows(rows(path))
        print(f"{month}: {fut} ({fut.stat().st_size // 1024} KB)")

    (root / "study-data").mkdir(parents=True, exist_ok=True)
    for label, wanted in (("90d", True), ("90d-holdout", False)):
        out = root / "study-data" / f"BTCUSDT-1m-{label}.csv"
        with out.open("w", newline="") as handle:
            writer = csv.writer(handle)
            for month, paths in sorted(month_files.items()):
                if (month in TRAIN_MONTHS) is not wanted:
                    continue
                for path in paths:
                    writer.writerows(rows(path))
        print(f"{out}: {out.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
