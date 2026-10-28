# SignalLab

A second, separate app — built because the first one could not honestly promise what
was asked of it. It answers the question **"can a pattern be found that wins 98–100 %
of the time?"** with 9.6 million one-minute bars instead of an opinion, and then ships
the best system that survived the attempt.

It does **not** touch `smc-radar/` or `tdigm-live/`. Nothing was deleted.

```
signal-lab/
├── lab/          the research library: data, 59 causal features, barrier outcomes, search
├── research/     the four experiments (frontier, 100 % hunt, walk-forward, bands)
├── app/          the running signal app (FastAPI + one page + SQLite)
├── tools/        dataset builder, UI checks
├── tests/        14 tests, including "features cannot look ahead"
├── data/         9.6 M bars as float32 (git-ignored, built by tools/build_dataset.py)
└── REPORT.md     ← the full result, with every table and every caveat
```

## Quick start

```bash
cd signal-lab
../.venv/bin/python tools/build_dataset.py          # ~700 MB download, ~2 min
../.venv/bin/python app/server.py --replay --speed 40 --port 8811
#  -> http://localhost:8811          (replay of 2024-01 → 2026-10, in arrival order)
#  -> python app/server.py --live    (polls MEXC futures instead)
```

The page has four tabs: the signal tape, the measured accuracy per band, **the 100 %
club** (the rules that never lost, and what happened to them next), and the method.

## The three numbers to take away

| claim | value |
|---|---|
| rules mined that never lost in sample (2017–2023) | **480** — largest fired 596 times |
| their pooled accuracy on 2024–2026, which the search never saw | **98.48 %** (42,641 / 43,300) |
| what a literal 100 % rule returns at that geometry, after fees | **−0.0016 R per trade** |

The 100 % is real and it is worthless: the target (0.0125 R) is smaller than the
round-trip fee (0.0141 R). Of 240 timeframe × target × stop configurations on the whole
archive, 4 reach ≥ 98 % accuracy and **none of them makes money after fees**; the 6
that do are 50.7 %–81.0 % accurate.

What the app actually trades, measured on three walk-forward folds with non-overlapping
trades: **75.4 % accuracy (95 % CI 67.7–81.7) at +0.120 R per trade, every fold
positive**, at a 0.50 R:R where one loss costs two wins.

## Reproduce

```bash
../.venv/bin/python research/01_frontier.py                     # 240 configurations
../.venv/bin/python research/04_hundred.py --tf 1h --target 0.05 --stop 4
../.venv/bin/python research/05_walkforward.py                  # worst-fold test
../.venv/bin/python research/06_bands.py                        # bands + model
../.venv/bin/python -m pytest tests -q
cd tools/ui && npm install && node ui.mjs                       # page checks
```

Every figure on the page comes from `app/data/*.json`, which those scripts write.
No number is typed into the HTML.

See **[REPORT.md](REPORT.md)** for the method, the full tables and the limits — read
section 8 before trusting any of it.
