#!/usr/bin/env python3
"""SignalLab server.

    python app/server.py                     # uses config.json (live by default)
    python app/server.py --live              # force live
    python app/server.py --replay --speed 25 # replay the archive

Everything the page prints comes from `config.json`, `app/model.pkl`, the files in
`app/data/` (written by the research scripts) or from this engine's own recorded
trades. The command line only overrides; `config.json` is the source of truth and
a change made in the page is written back to it.

    GET  /                the page
    GET  /api/state       the whole picture, one JSON
    GET  /api/meta        the research numbers (bands, 100 % club, frontier, sweep)
    GET  /api/config      the live configuration and where it came from
    POST /api/config      change and persist a setting
    POST /api/scan        poll the exchange now instead of waiting for the clock
    POST /api/cut         change the recording cut
    POST /api/speed       change the replay speed
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

import config as appconfig  # noqa: E402
from engine import ALL_TFS, Engine  # noqa: E402

app = FastAPI(title="SignalLab")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ENGINE: Engine | None = None
CFG: appconfig.Config | None = None
LOOP = False
_runner: threading.Thread | None = None
_stop = threading.Event()
_wake = threading.Event()          # /api/scan sets this: poll now, don't wait
WEB = os.path.join(HERE, "web")

# keys the page may change without a restart, and how to coerce them
LIVE_KEYS = {"poll_seconds": float, "workers": int, "scan_bars": int, "cut_override": float,
             "risk_per_trade_pct": float, "equity_usd": float, "max_candidates": int,
             "fee_bp": float, "auto_top": int, "db": str, "port": int, "host": str,
             "mode": str, "timeframes": list, "symbols": list}


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


def _summary_rr(rr: dict) -> dict:
    """The reward:risk sweep, compact enough for the page."""
    rows = []
    for r in rr["rows"]:
        best = None
        for c, v in r["cuts"].items():
            cand = dict(cut=float(c), n=v["n"], win=v["win"], worst=v["worst"], mean=v["mean"],
                        folds=v["folds"])
            if best is None or cand["worst"] > best["worst"]:
                best = cand
        if not best:
            continue
        rows.append(dict(tf=r["tf"], stop=r["stop"], rr=r["rr"], base=r["base"],
                         break_even=r["be"], fee_r=r["fee_r"], timeout=r["timeout"], **best))
    rows.sort(key=lambda r: -r["worst"])
    return {"rows": rows, "survivors": rr["survivors"], "horizon": rr["horizon"],
            "fee_bp": rr["fee_bp"], "folds": rr["folds"], "chosen": rr.get("chosen")}


def _summary_timeframes() -> dict | None:
    """The round-16 sweep: what each of the eleven clocks measured. None if not run."""
    p = os.path.join(ROOT, "results", "timeframes.json")
    if not os.path.exists(p):
        return None
    with open(p) as fh:
        sweep = json.load(fh)
    rows = sorted(sweep.get("rows", []), key=lambda r: (ALL_TFS.index(r["tf"]), r["stop"]))
    return {"rows": rows, "best": sweep.get("best", {}), "fee_bp": sweep.get("fee_bp"),
            "horizon": 200, "stops": sorted({r["stop"] for r in rows}),
            "cut": sorted({r["cut"] for r in rows if r.get("cut") is not None}),
            "note": "research/08_timeframes.py - one entry per geometry, 2024-2026, "
                    "non-overlapping, fee priced in R per timeframe"}


@app.get("/api/health")
def health():
    e = ENGINE
    return {"ok": True, "mode": e.mode if e else None, "steps": e.steps if e else 0,
            "symbols": e.symbols if e else [], "tfs": e.tfs if e else [],
            "missing_tfs": getattr(e, "missing_tfs", []) if e else [],
            "poll": getattr(e, "last_poll", None), "error": e.error if e else None}


@app.get("/api/meta")
def meta():
    bands = _load("bands.json")
    hundred = _summary_hundred(_load("hundred.json"))
    frontier = _summary_frontier(_load("frontier.json"))
    rr = _summary_rr(_load("rr.json")) if os.path.exists(os.path.join(HERE, "data", "rr.json")) else None
    geoms = None
    gp = os.path.join(HERE, "data", "geoms.json")
    if os.path.exists(gp):
        with open(gp) as fh:
            geoms = json.load(fh).get("geoms")
    return {"bands": bands, "hundred": hundred, "frontier": frontier, "rr": rr,
            "timeframes": _summary_timeframes(), "geoms": geoms,
            "method": {
                "data": "1-minute archive, BTCUSDT + ETHUSDT, 2017-08-17 -> 2026-10",
                "bars": 9647677,
                "train": "expanding window; the years 2022, 2023 and 2024-2026 are only ever scored",
                "geometry": "reward:risk 1:4, long side; stop and target are ATR multiples "
                            "chosen per timeframe by research/08_timeframes.py",
                "fees": "5 bp round trip (MEXC futures taker 2 bp x2 + 1 bp slippage), "
                        "priced in R against each timeframe's stop",
                "non_overlap": "one entry per stream per horizon, as measured",
                "model": "replay scores with a validation model trained until 2023-12-31; "
                         "live scores with the production model",
            }}


@app.get("/api/state")
def state():
    if ENGINE is None:
        return JSONResponse({"error": "engine not started"}, status_code=503)
    return ENGINE.snapshot()


@app.get("/api/config")
def get_config():
    if ENGINE is None or CFG is None:
        return JSONResponse({"error": "engine not started"}, status_code=503)
    return {"config": CFG.as_dict(), "path": CFG.path,
            "editable": sorted(LIVE_KEYS), "timeframes_available": list(ALL_TFS),
            "note": "edits are written back to config.json; mode, port, symbols, "
                    "timeframes and workers apply on restart"}


@app.post("/api/config")
def set_config(body: dict):
    """Change a setting and persist it to config.json."""
    if ENGINE is None or CFG is None:
        return JSONResponse({"error": "engine not started"}, status_code=503)
    changed = {}
    for k, v in (body or {}).items():
        if k in LIVE_KEYS:
            if k == "cut_override" and v in (None, "", "null"):
                CFG.cut_override = None
                changed[k] = None
                continue
            try:
                setattr(CFG, k, LIVE_KEYS[k](v) if not isinstance(v, list) else v)
                changed[k] = getattr(CFG, k)
            except (TypeError, ValueError) as exc:
                return JSONResponse({"error": f"{k}: {exc}"}, status_code=400)
    if not changed:
        return JSONResponse({"error": "no known keys in the request",
                             "known": sorted(LIVE_KEYS)}, status_code=400)
    path = appconfig.save(CFG)
    if "cut_override" in changed:
        ENGINE.cut = changed["cut_override"] or None
    return {"changed": changed, "saved_to": path,
            "restart_required": sorted(set(changed) & {"mode", "port", "host", "symbols",
                                                       "timeframes", "workers"})}


@app.post("/api/scan")
def scan_now():
    """Poll the exchange immediately instead of waiting for the next tick."""
    if ENGINE is None:
        return JSONResponse({"error": "engine not started"}, status_code=503)
    if ENGINE.mode != "live":
        return {"mode": ENGINE.mode, "note": "the replay has its own clock"}
    _wake.set()
    return {"ok": True, "requested": True}


@app.post("/api/cut")
def set_cut(body: dict):
    """Change the recording cut live: fewer signals, each more reliable."""
    if ENGINE:
        ENGINE.cut = max(0.05, min(0.95, float(body.get("value", ENGINE.cut or 0.3))))
    return {"cut": ENGINE.cut if ENGINE else None}


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
                if not LOOP:
                    return
                engine.rewind()
                break
        dt = time.time() - t0
        sleep = max(0.0, 0.1 - dt)
        _stop.wait(sleep)


def _run_live(engine: Engine) -> None:
    poll = float(getattr(CFG, "poll_seconds", 60.0) or 60.0)
    while not _stop.is_set():
        try:
            engine.tick_live()
        except Exception as exc:  # never let one bad poll kill the loop
            engine.error = str(exc)
        _wake.wait(poll)
        _wake.clear()


def main() -> None:
    global ENGINE, _runner, CFG, LOOP
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, help="path to config.json (default: signal-lab/config.json)")
    ap.add_argument("--live", action="store_true", help="force live mode")
    ap.add_argument("--replay", action="store_true", help="force replay mode")
    ap.add_argument("--loop", action="store_true", help="start the replay again when it reaches the end")
    ap.add_argument("--speed", type=float, default=None)
    ap.add_argument("--cut", type=float, default=None, help="probability cut; overrides config.json")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--symbols", default=None, help="comma list; overrides config.json")
    ap.add_argument("--tfs", default=None, help="comma list of timeframes; overrides config.json")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--poll", type=float, default=None, help="seconds between live scans")
    args = ap.parse_args()

    CFG = appconfig.load(args.config)
    if args.live:
        CFG.mode = "live"
    if args.replay:
        CFG.mode = "replay"
    if args.symbols:
        CFG.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.tfs:
        CFG.timeframes = [t.strip() for t in args.tfs.split(",") if t.strip() in ALL_TFS]
    if args.port:
        CFG.port = args.port
    if args.host:
        CFG.host = args.host
    if args.workers:
        CFG.workers = args.workers
    if args.poll:
        CFG.poll_seconds = args.poll
    LOOP = args.loop

    engine_tfs = [t for t in CFG.timeframes if t in ALL_TFS] or ["1h"]
    ENGINE = Engine(mode="live" if CFG.mode == "live" else "replay",
                    speed=args.speed if args.speed is not None else 25.0,
                    cut=args.cut if args.cut is not None else CFG.cut_override,
                    symbols=CFG.symbols_final or None,
                    tfs=engine_tfs, config=CFG)
    ENGINE.prepare()
    _runner = threading.Thread(target=_run_live if CFG.mode == "live" else _run_replay,
                               args=(ENGINE,), daemon=True)
    _runner.start()

    import uvicorn
    uvicorn.run(app, host=CFG.host, port=CFG.port, log_level="warning")


if __name__ == "__main__":
    main()
