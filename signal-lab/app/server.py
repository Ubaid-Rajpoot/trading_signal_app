#!/usr/bin/env python3
"""SignalLab server.

    python app/server.py --replay --speed 25 --port 8811
    python app/server.py --live --port 8811            # needs exchange access

The page polls /api/state; every number it prints comes from a file produced by
the research scripts in ../research or from this engine's own recorded trades.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from engine import Engine  # noqa: E402

app = FastAPI(title="SignalLab")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ENGINE: Engine | None = None
_runner: threading.Thread | None = None
_stop = threading.Event()
WEB = os.path.join(HERE, "web")


def _load(name: str):
    p = os.path.join(HERE, "data", name)
    with open(p) as fh:
        return json.load(fh)


def _summary_hundred(h: dict) -> dict:
    rules = h["rules"]
    scored = [r for r in rules if r["oos_n"] >= 1]
    pool_n = sum(r["oos_n"] for r in scored)
    pool_w = sum(r["oos_w"] for r in scored)
    still = sum(1 for r in scored if r["oos_wr"] == 1.0)
    k = h["rr"]
    p = pool_w / pool_n if pool_n else 0
    return {
        "tf": h["tf"], "target": h["target"], "stop": h["stop"], "rr": k,
        "fee_r": h["fee_r"], "fee_bp": h["fee_bp"],
        "base_is": h["base_is"], "base_oos": h["base_oos"],
        "levels": h["levels"],
        "perfect_rules": len(rules), "scored": len(scored), "still_perfect": still,
        "pooled_n": pool_n, "pooled_w": pool_w, "pooled_win": p,
        "pooled_ev": p * k - (1 - p), "pooled_ev_post": p * k - (1 - p) - h["fee_r"],
        "perfect_ev_post": k - h["fee_r"],
        "fee_pct_of_prize": h["fee_r"] / k,
        "rules": sorted(rules, key=lambda r: -r["oos_n"])[:40],
        "is_rows": h["is_rows"], "oos_rows": h["oos_rows"],
    }


def _summary_frontier(rows: list[dict]) -> dict:
    hi = [r for r in rows if r["win"] >= 0.98]
    pos = [r for r in rows if r["ev_post"] > 0]
    return {
        "count": len(rows),
        "high_accuracy": len(hi),
        "high_accuracy_positive": sum(1 for r in hi if r["ev_post"] > 0),
        "positive": len(pos),
        "positive_accuracy": [min(r["win"] for r in pos), max(r["win"] for r in pos)] if pos else None,
        "rows": [{k: r[k] for k in ("symbol", "tf", "target", "stop", "rr", "n", "win",
                                    "ev_pre", "ev_post", "fee_r")} for r in rows],
    }


@app.get("/api/health")
def health():
    e = ENGINE
    return {"ok": True, "mode": e.mode if e else None, "steps": e.steps if e else 0,
            "symbols": e.symbols if e else [], "tfs": e.tfs if e else [],
            "error": e.error if e else None}


@app.get("/api/meta")
def meta():
    bands = _load("bands.json")
    hundred = _summary_hundred(_load("hundred.json"))
    frontier = _summary_frontier(_load("frontier.json"))
    return {"bands": bands, "hundred": hundred, "frontier": frontier,
            "method": {
                "data": "Binance 1-minute archive, BTCUSDT + ETHUSDT, 2017-08-17 -> 2026-10-24",
                "bars": 9647677,
                "train": "expanding window; the years 2022, 2023 and 2024-2026 are only ever scored",
                "geometry": "target 2 ATR / stop 4 ATR (0.50 R:R), 60-bar horizon, long side",
                "fees": "5 bp round trip (MEXC futures taker 2 bp x2 + 1 bp slippage)",
                "non_overlap": "one open trade per symbol and timeframe at a time",
                "model": "replay scores with a validation model trained until 2023-12-31; "
                         "live scores with the production model trained through 2026-10",
            }}


@app.get("/api/state")
def state():
    if ENGINE is None:
        return JSONResponse({"error": "engine not started"}, status_code=503)
    return ENGINE.snapshot()


@app.post("/api/speed")
def speed(body: dict):
    if ENGINE:
        ENGINE.speed = max(1.0, min(400.0, float(body.get("value", ENGINE.speed))))
    return {"speed": ENGINE.speed if ENGINE else None}


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB, "index.html"))


def _run_replay(engine: Engine) -> None:
    while not _stop.is_set():
        t0 = time.time()
        for _ in range(max(1, int(engine.speed / 10))):
            if not engine.step():
                return
        dt = time.time() - t0
        sleep = max(0.0, 0.1 - dt)
        _stop.wait(sleep)


def _run_live(engine: Engine) -> None:
    while not _stop.is_set():
        try:
            engine.tick_live()
        except Exception as exc:  # never let one bad poll kill the loop
            engine.error = str(exc)
        _stop.wait(45)


def main() -> None:
    global ENGINE, _runner
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--speed", type=float, default=25.0)
    ap.add_argument("--cut", type=float, default=0.80)
    ap.add_argument("--port", type=int, default=8811)
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--tfs", default="4h,1h")
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    ENGINE = Engine(mode="live" if args.live else "replay", speed=args.speed, cut=args.cut,
                    symbols=[s for s in args.symbols.split(",") if s],
                    tfs=[s for s in args.tfs.split(",") if s])
    ENGINE.prepare()
    _runner = threading.Thread(target=_run_live if args.live else _run_replay,
                               args=(ENGINE,), daemon=True)
    _runner.start()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
