#!/usr/bin/env python3
"""Build the compact dataset the lab reads.

Source: the open Binance 1-minute archives mirrored on GitHub
(``parshant231234-eng/{BTC,ETH}USDT-Historical-Data``, monthly CSVs,
2017-08 -> present). Two files are written per symbol into ``data/``:

    <SYMBOL>_1m.npy   float32 [open high low close volume quote_volume trades taker_buy]
    <SYMBOL>_t.npy    int64 minutes since epoch

Usage
-----
    # download + build everything (needs internet)
    python tools/build_dataset.py

    # build from an already extracted copy (no internet)
    python tools/build_dataset.py --raw-dir /path/with/BTCUSDT-Historical-Data-main

The raw archive is ~700 MB per symbol; the compact form is ~150 MB per symbol.
Both live outside git.
"""
from __future__ import annotations

import argparse
import glob
import io
import os
import subprocess
import sys
import tarfile
import tempfile

import numpy as np
import pandas as pd

REPOS = {
    "BTCUSDT": "parshant231234-eng/BTCUSDT-Historical-Data",
    "ETHUSDT": "parshant231234-eng/ETHUSDT-Historical-Data",
}
USE = ["timestamp", "open", "high", "low", "close", "volume", "quote_volume",
       "trade_count", "taker_buy_volume"]
OUT = ["open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy"]


def download(symbol: str, dest: str) -> str:
    repo = REPOS[symbol]
    url = f"https://codeload.github.com/{repo}/tar.gz/refs/heads/main"
    print(f"  downloading {repo} ...")
    tgz = os.path.join(dest, f"{symbol}.tgz")
    subprocess.run(["curl", "-sL", "-o", tgz, url], check=True)
    print(f"  extracting ({os.path.getsize(tgz) / 1e6:.0f} MB) ...")
    with tarfile.open(tgz) as tf:
        tf.extractall(dest)
    os.remove(tgz)
    return os.path.join(dest, os.listdir(os.path.join(dest))[0])


def find_month_dir(raw: str) -> str:
    hits = glob.glob(os.path.join(raw, "**", "1m", "*", "*"), recursive=True)
    hits = [h for h in hits if os.path.isdir(h) and glob.glob(os.path.join(h, "*.csv"))]
    if not hits:
        raise SystemExit(f"no monthly CSVs under {raw}")
    return os.path.dirname(os.path.dirname(hits[0]))


def build(symbol: str, raw: str, outdir: str) -> None:
    base = find_month_dir(raw)
    files = sorted(glob.glob(os.path.join(base, "*", "*", "*.csv")))
    print(f"  {symbol}: {len(files)} monthly files")
    frames = [pd.read_csv(f, usecols=USE) for f in files]
    df = pd.concat(frames, ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
    t = (df.timestamp.to_numpy(np.int64) // 60000).astype(np.int64)
    arr = df[USE[1:]].to_numpy(np.float64)
    # a duplicate/missing minute cannot be repaired; report it and move on
    gaps = np.diff(t) - 1
    print(f"  {symbol}: {len(df)} bars  {pd.to_datetime(t[0] * 60000, unit='ms')} -> "
          f"{pd.to_datetime(t[-1] * 60000, unit='ms')}  missing minutes: {int(gaps[gaps > 0].sum())}")
    np.save(os.path.join(outdir, f"{symbol}_1m.npy"), arr.astype(np.float32))
    np.save(os.path.join(outdir, f"{symbol}_t.npy"), t)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", help="directory with extracted <SYMBOL>-Historical-Data-main folders")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--symbols", default=",".join(REPOS))
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    for sym in args.symbols.split(","):
        sym = sym.strip()
        if not sym:
            continue
        print(sym)
        if args.raw_dir:
            cand = [p for p in glob.glob(os.path.join(args.raw_dir, f"{sym}*")) if os.path.isdir(p)]
            if not cand:
                print(f"  skip: nothing matching {sym}* in {args.raw_dir}")
                continue
            build(sym, cand[0], out)
        else:
            tmp = tempfile.mkdtemp(prefix=f"raw_{sym}_")
            raw = download(sym, tmp)
            build(sym, raw, out)
            subprocess.run(["rm", "-rf", tmp], check=False)
    print("done ->", out)


if __name__ == "__main__":
    sys.exit(main())
