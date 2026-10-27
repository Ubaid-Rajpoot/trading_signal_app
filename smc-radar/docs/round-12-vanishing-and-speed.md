# Round 12 — why a live signal vanished, and why the app got slow

Two reports, one investigation. Both turned out to be real, and both were introduced
by the previous rounds.

> *"Sometimes a signal is generated and then it disappears, even though it was neither
> filled nor invalidated. Is it stored in the database? And since the last change the
> whole software has become very slow."*

## 1. "The signal disappeared although nothing happened to it"

**It was not the pattern engine, and it was not the database. It was the size of the
pushed frame.**

The scanner pushes everything the page draws as one JSON frame. That frame carried
**the top 200 active zones, ranked by probability**. With more than 200 zones live —
60 contracts × 5 timeframes reaches that easily — every zone below the cut simply was
not sent. The page had no way to know it existed, so the card vanished. Nothing had
been filled, nothing had been invalidated: the zone was still `active` on the server
and still in SQLite.

Measured on a synthetic 60-instrument scan (the same code path, no exchange needed):

| | before | after |
| --- | --- | --- |
| live zones on the scanner | 960 | 960 |
| zones in the pushed frame | 200 (by probability) | 200 **signals first** |
| signals in the frame | 85 of 85 *by luck of ranking* | **200 of 400 cap-limited, all signals first** |
| does the frame say what it left out? | no | `active_total`, `active_shown`, `signal_total`, `signals_shown` |
| can the page get the rest? | no | **yes** — "load every live zone" pulls `/api/zones?status=active` |

What changed:

* **Signals always lead the frame.** The list is partitioned — every signal first, then
  everything else, both already best-first. The cap can now only ever drop a
  *non-signal*; a signal is dropped only if there are more signals than the frame holds,
  and the frame *says* so.
* **The page is told the truth.** `active_total` / `active_shown` / `signal_total` /
  `signals_shown` are in every frame; the list header prints
  *"the frame carries 200 of 960 live zones — load the rest"* with a button that fetches
  all of them in one request.
* **Nothing was ever lost in the database.** Every signal is written when it forms,
  independent of the frame: in the replay running now, `journal.signals = 134`,
  `journal.rows = 4650`, and `tools/ui/perf.mjs` checks that every live signal it sees
  can be read back from `/api/journal/{key}` with the same entry and stop.

The three *legitimate* ways a card leaves the live list remain, and each is written down
in the page (Archive tab → "what the badges mean"):

| what you see | what happened |
| --- | --- |
| `TARGET HIT` | price reached the target printed on the card → the plan's R:R booked |
| `STOPPED` | price reached the stop printed on the card → −1 R |
| `MITIGATED` | price came back into the block; the target/stop then decided the R |
| `INVALIDATED` | a candle **closed** beyond the block — the level is broken; a filled plan took its stop, an unfilled one never traded |
| `INTERRUPTED` / `NOT WATCHED` | the scanner restarted, or the instrument was removed, while the plan was open — the row is kept, it is just no longer followed |

## 2. "The whole software became very slow"

Two costs, both measured with `tools/ui/perf.mjs`.

**a) Bytes.** A frame was ~1.3 KB per zone *including* the prose (evidence list, the
order-block candle, the confluence lines, the HTF detail) and went out 2–3 times a
second:

| | before | after |
| --- | --- | --- |
| per zone in a frame | ~1.3 KB | **~0.57 KB** |
| a 200-zone frame | ~419 KB | **~130 KB** |
| push cadence (60 instruments) | 0.35 s | **0.7 s** (1.2 s up to 200, 2 s beyond) |
| live frames per second of traffic | 0.8–1.2 MB/s | **≈0.1 MB/s** at 1 instrument, ≈0.2 MB/s at 200 zones |

How: the frame now carries **only what a card prints** (`Engine.row`): no evidence
sentences, no OB candle, no confluence/HTF lines, no null fields, conditions as a 10-bit
mask instead of a list of names, floats cut to screen precision. Those sentences are one
request away, for the single zone that is opened — `GET /api/zones/{key}`.

**b) Rendering.** The page rebuilt every pane on every frame — 200 cards, the strip, the
live rows, the tape — even when not one number had changed. Now each pane has a
**signature** (keys + states + probabilities) and is redrawn only when that signature
changes, and the list draws **60 cards at a time** with "show more", instead of 300 at
once.

Measured in jsdom (DOM work only, no layout):

| | before | after |
| --- | --- | --- |
| an idle frame with nothing new | rebuilt every list | **2.0 ms, and the same DOM nodes are kept** |
| the list's DOM nodes after 10 idle frames | all replaced | **identical objects** |

## Reproduce

```bash
cd smc-radar
../.venv/bin/python -m pytest tests -q                 # 95 passed
rm -f data/journal.sqlite3*
../.venv/bin/python -u run.py replay data/fut 60 --ticks 6 --port 8800 &
bash tools/smoke.sh
cd tools/ui && npm install --silent jsdom ws
node perf.mjs && node layout.mjs && node quality.mjs && node algo1m.mjs && node transports.mjs
```

`tools/ui/perf.mjs` is new: it measures the frame size, the frames/second and MB/s off
the websocket, the cost of ten idle frames, whether an idle frame keeps the same DOM
nodes, that a card's evidence is fetched on open, that every live signal is in the
database with the same entry and stop, and that "load every live zone" really returns
the whole list.

**Not verified here:** no browser engine and no exchange in this sandbox, so the pixel
layout and the live MEXC path are reasoned, not photographed. The frame contents, the
sizes, the cadence, the DOM behaviour and the API contract are measured on the running
server.
