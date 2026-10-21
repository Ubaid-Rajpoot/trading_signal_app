#!/usr/bin/env python3
"""Put REAL exchange candles on disk, then the whole app uses them.

Three ways in, in order of usefulness:

    # 1. Import Binance's own kline dumps you already have (CSV or the monthly ZIP)
    python scripts/fetch_real_data.py --import ~/Downloads/BTCUSDT-1m-2024-01.zip
    python scripts/fetch_real_data.py --import ~/data/BTCUSDT-1m-2025-01-01.csv

    # 2. Download from a GitHub mirror of the Binance public data (works without
    #    touching api.binance.com)
    python scripts/fetch_real_data.py --mirror BTCUSDT

    # 3. Straight from Binance's public data host (needs internet to binance.vision)
    python scripts/fetch_real_data.py --binance-vision BTCUSDT --months 2025-01,2025-02

    # see what is on disk
    python scripts/fetch_real_data.py --list

Everything lands in ``backend/data/cache/datasets/<SYMBOL>/`` as daily kline CSVs
in the official Binance format. The app resamples the 1-minute bars to every
timeframe, so one import gives you 1m/3m/5m/15m/30m/1h/4h/1d.
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.data import real  # noqa: E402

MIRROR_REPO = "Thaum-blip/TradinBot"
MIRROR_PATH = "temp_data"
VISION = "https://data.binance.vision/data/spot"


def _store(symbol: str, payload: bytes, name: str) -> Path:
    folder = real.dataset_dir(symbol)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_bytes(payload)
    return target


def import_path(path: Path) -> int:
    """Import a CSV or a ZIP of CSVs. Returns the number of files stored."""
    if path.suffix.lower() == ".zip":
        stored = 0
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                if not member.lower().endswith(".csv"):
                    continue
                symbol = Path(member).name.split("-")[0].upper()
                _store(symbol, archive.read(member), Path(member).name)
                stored += 1
        return stored

    symbol = path.name.split("-")[0].upper()
    _store(symbol, path.read_bytes(), path.name)
    return 1


def fetch_binance_vision(symbol: str, months: list[str]) -> int:
    stored = 0
    for month in months:
        url = f"{VISION}/monthly/klines/{symbol}/1m/{symbol}-1m-{month}.zip"
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                payload = response.read()
        except Exception as exc:
            print(f"  {month}: {type(exc).__name__}: {exc}")
            continue
        folder = real.dataset_dir(symbol)
        folder.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.namelist():
                if member.lower().endswith(".csv"):
                    _store(symbol, archive.read(member), Path(member).name)
                    stored += 1
        print(f"  {month}: ok")
    return stored


def fetch_mirror(symbol: str, url: str | None = None) -> int:
    """Download a GitHub mirror tarball and keep the files for ``symbol``."""
    url = url or f"https://codeload.github.com/{MIRROR_REPO}/tar.gz/refs/heads/main"
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=300) as response:
        payload = response.read()
    print(f"  {len(payload) / 1e6:.1f} MB")
    stored = 0
    with _tar(payload) as archive:
        for member in archive.getnames():
            name = Path(member).name
            if not name.lower().endswith(".csv") or not name.upper().startswith(symbol.upper()):
                continue
                _store(symbol, archive.extractfile(member).read(), name)
            stored += 1
    return stored


def _tar(payload: bytes):
    import tarfile

    return tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")


def main() -> int:
    ap = argparse.ArgumentParser(description="Import real exchange candles into the app")
    ap.add_argument("--import", dest="import_path", default="", help="CSV or ZIP of kline files")
    ap.add_argument("--mirror", default="", metavar="SYMBOL", help="download a GitHub mirror dataset")
    ap.add_argument("--mirror-url", default="", help="override the mirror tarball URL")
    ap.add_argument("--binance-vision", default="", metavar="SYMBOL", help="download from data.binance.vision")
    ap.add_argument("--months", default="", help="comma-separated YYYY-MM for --binance-vision")
    ap.add_argument("--list", action="store_true", help="show datasets on disk")
    args = ap.parse_args()

    if args.list:
        symbols = real.dataset_symbols()
        if not symbols:
            print(f"No datasets yet. They live in {real.DATASET_DIR}.")
            print("Try: python scripts/fetch_real_data.py --import <klines.zip>")
            return 0
        for symbol in symbols:
            info = real.coverage(symbol)
            print(
                f"{symbol:<10} {info['files']:>4} files  {info['bars_1m']:>8,} bars  "
                f"{info['first']} -> {info['last']}"
            )
        return 0

    stored = 0
    if args.import_path:
        stored += import_path(Path(args.import_path).expanduser())
    if args.mirror:
        stored += fetch_mirror(args.mirror.upper(), args.mirror_url or None)
    if args.binance_vision:
        months = [m.strip() for m in args.months.split(",") if m.strip()]
        if not months:
            print("--binance-vision needs --months 2025-01,2025-02", file=sys.stderr)
            return 2
        stored += fetch_binance_vision(args.binance_vision.upper(), months)

    if not stored:
        print("Nothing imported. Use --import, --mirror, or --binance-vision (see --help).", file=sys.stderr)
        return 1

    real.reload()
    print(f"\nstored {stored} file(s):")
    for symbol in real.dataset_symbols():
        info = real.coverage(symbol)
        print(f"  {symbol}: {info['bars_1m']:,} 1m bars, {info['first']} -> {info['last']}")
    print("\nThe app now uses these real candles automatically (source = dataset).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
