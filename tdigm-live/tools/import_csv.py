#!/usr/bin/env python3
"""Convert raw exchange CSVs into the chunked files ``scan_history.py`` reads.

The measurements in the README are made on **real** BTCUSDT 1-minute candles
from this public archive (Binance spot, no API key):

    https://github.com/Thaum-blip/TradinBot

    curl -sL -o /tmp/tradinbot.tgz https://codeload.github.com/Thaum-blip/TradinBot/tar.gz/refs/heads/main
    tar xzf /tmp/tradinbot.tgz -C /tmp

    # full year that the numbers below use
    python tools/import_csv.py /tmp/TradinBot-main/historical_data_BTCUSDT_1m_20220701_to_20250630.csv \\
        /tmp/TradinBot-main/temp_data --symbol BTCUSDT \\
        --start 2024-07-01 --end 2025-06-29 --out data/btc

    # the year before it, for the out-of-sample check
    python tools/import_csv.py /tmp/TradinBot-main/historical_data_BTCUSDT_1m_20220701_to_20250630.csv \\
        --symbol BTCUSDT --start 2023-07-01 --end 2024-06-30 --out data/btc_oos

Both raw formats are understood: ``timestamp,open,high,low,close,volume`` with an
ISO stamp, and the 12-column kline dumps whose first field is epoch
milliseconds/microseconds.

Output: one ``<SYMBOL>-<year>-<month>.csv`` per month, columns
``open_time_ms,open,high,low,close,volume``. ``data/`` is git-ignored - the
numbers are reproducible from the commands above, the candles are not committed.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _when(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    if raw[:4].isdigit() and "-" in raw:
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        value = int(raw)
    except ValueError:
        return None
    # epoch seconds / milliseconds / microseconds, whichever this dump uses
    for scale in (1_000_000, 1_000, 1):
        candidate = value / scale
        if 1_000_000_000 < candidate < 4_102_444_800:  # 2001 .. 2100
            return datetime.fromtimestamp(candidate, tz=timezone.utc)
    return None


def _paths(sources: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in sources:
        path = Path(raw)
        if path.is_dir():
            out.extend(sorted(path.glob("*.csv")))
        else:
            out.append(path)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="+", help="raw csv files and/or folders")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--start", default="", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", default="", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--out", default="data/btc")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else None
    stop = end + timedelta(days=1) if end else None

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{args.symbol}-*.csv"):
        old.unlink()

    handles: dict[str, tuple[object, object]] = {}
    seen: set[int] = set()
    kept = 0
    for source in _paths(args.sources):
        with source.open(newline="") as handle:
            for row in csv.reader(handle):
                if len(row) < 6:
                    continue
                when = _when(row[0])
                if when is None:
                    continue
                if start and when < start:
                    continue
                if stop and when >= stop:
                    continue
                stamp = int(when.timestamp() * 1000)
                if stamp in seen:
                    continue
                seen.add(stamp)
                name = f"{args.symbol}-{when:%Y-%m}.csv"
                entry = handles.get(name)
                if entry is None:
                    fresh = (out_dir / name).open("w", newline="")
                    entry = (fresh, csv.writer(fresh))
                    handles[name] = entry
                entry[1].writerow([stamp, row[1], row[2], row[3], row[4], row[5]])  # type: ignore[union-attr]
                kept += 1

    for fresh, _ in handles.values():
        fresh.close()  # type: ignore[union-attr]
    files = sorted(handles)
    span = f"{files[0]} .. {files[-1]}" if files else "nothing"
    print(f"{args.symbol}: {kept} bars -> {len(files)} files in {out_dir} ({span})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
