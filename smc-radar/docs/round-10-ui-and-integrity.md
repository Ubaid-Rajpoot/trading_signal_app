# Round 10 — the page stops lying, and the archive stops drifting

Three complaints, one theme: **the screen and the database have to agree, and neither
may lose anything.**

1. *"Signals still vanish — did you fix it?"*
2. *"Adding a coin throws `DecodingError: brotli: …` in the toast."*
3. *"Content spills out of the cards, and a big screen has empty space at the sides."*

Everything below is verified with the suites in this folder; the exact commands are at
the bottom.

## 1. Signals that vanish — four separate causes, all closed

The round-9 journal stored signals. It was not enough, because four other paths could
still make a stored signal *disappear from the page* or *lie about itself*:

| # | what happened | fix |
| --- | --- | --- |
| 1 | The strip showed **only** the live map. A plan that reached its target left `active`, so its card vanished from the top of the page — the signal was safe in SQLite and invisible on screen. | The strip now merges the live map with `/api/journal` and renders the stored ones underneath, **with their outcome**: `TARGET HIT` / `STOPPED` / `MITIGATED`. |
| 2 | `sync_journal()` skipped any zone whose probability had fallen below `journal_prob_min`, so those rows froze at "waiting" while the scanner filled them and hit their target. | A row that exists is updated for its whole life, whatever its probability is now. |
| 3 | Removing a symbol dropped its live plans from memory and left their rows saying `active` for ever: the drawer's "open now" grew, and the signal never came back. | Removing an instrument writes `untracked` + a note to its live rows. The rows are kept; the count is honest. |
| 4 | A restart throws the in-memory map away, so every plan it held could never resolve — a graveyard of "open" rows. | On start-up, rows still open and untouched for `journal_reap_hours` (6 h) become `interrupted`, with a note. Nothing is deleted. |

A fifth, subtler one: `close_signal()` used `COALESCE(?, result)`, which protects a
result from being **blanked** but lets a *different* one overwrite it. Replaying the
same candles after a restart could turn "target, +1.6 R" into "stopped, −1 R" and move
the archive's own hit rate between runs. A decided outcome (`target` / `stopped`) is now
final; a row that has only reached `mitigated` may still go on to hit its target.

Measured — same database, two separate processes, ~35 s and ~25 s of the recorded
futures replay:

| | run 1 | run 2 (fresh process, same file) |
| --- | --- | --- |
| rows in the database | 348 | 348 (never deleted) |
| signals | 15 | 15 |
| finished signals | 6 | 6 |
| of those, targets hit | 5 | 5 |
| average R per finished signal | +0.245 | **+0.245** |
| rows reaped as interrupted | 0 | 3 |
| reported "open" | 10 | 8 |

The scoreboard is now idempotent: a restart cannot move it. And the page keeps a copy of
the last good archive poll — **a failed request never empties what is already on
screen** (`tools/ui/layout.mjs` asserts exactly that).

## 2. `DecodingError: brotli: decoder process called with data when 'can_accept_more_data()' is False`

The cause was in the scanner, not in the exchange. Every REST call was made with a
default `httpx` client that advertises whatever the library supports — including
`br` — and a body that is handed to a **reused** brotli decoder can throw that error
half-way through. Adding a symbol calls the contract list, which is a few hundred
kilobytes of JSON: exactly the request that trips it. The raw exception then travelled
to the browser inside `{"error": "DecodingError: brotli: …"}` and the toast printed it
verbatim.

Fixed at three layers:

* **Transport** (`bot/feed.py`): one `http_client()` used by every request, with
  `accept-encoding: gzip, deflate` — brotli is never advertised, and the scanner never
  wanted it (gzip is a third of the CPU and has no state machine that can stick).
  A decode or transport failure is retried once **with `identity`** (no compression at
  all), a `429`/`5xx` is retried, a `4xx` is not (retrying a "no" only burns the rate
  limit), and a body that still cannot be read becomes a `FeedError` that names the URL.
* **API**: a broken exchange list is returned as data (`HTTP 200`, `ok: false`, a
  readable error) — never a 500. The scan keeps running with the instruments it has.
* **Page**: `friendlyError()` translates decoder / network / timeout / 429 / 5xx text
  into what happened and what to do; the technical text moved to the console. Toasts
  now **stack** (three warnings used to overwrite one another in a single line), and an
  error is visibly different from a note.

Pinned by `tests/test_feed.py`: brotli is never advertised, a broken body is retried
with `identity`, a body that stays broken names the URL, a `404` costs one request, a
`429` is retried, and `POST /api/universe/add` answers `200 {ok: false, error: …}`
while the scanner keeps its symbols.

## 3. The page on a real screen

* **Width**: `max-width` 1560 px → **2400 px**, padding in `clamp()`, and the KPI band
  is `auto-fit` so ten counters fill the row instead of leaving a ragged gap.
* **A third column** from 1780 px: cards · chart + tape · the signal archive. The cards
  take whatever is left, so a wide monitor is used instead of being thrown away in two
  gutters. Below 1780 px the archive spans the page as a band under the map; on a phone
  everything stacks.
* **No nested scroller**: the card list was `max-height: 1120px; overflow-y: auto`, so a
  tall card was clipped inside the panel and the list scrolled inside a page that also
  scrolled. The page scrolls once now.
* **Nothing leaves a card**: the chips on the right of a card travel as one group so
  they wrap together, `entry / stop / target` use three equal shrinkable columns, and
  every numeric / text block is `overflow-wrap: anywhere` inside an `overflow: hidden`
  box. Long prices, long condition names and long target descriptions wrap or clip —
  they no longer push out of the box.
* **Fewer silent failures**: a script error or an unhandled promise now lands in the
  toast stack once (the page used to half-freeze with no explanation), each section
  draws inside `safe()` so one broken panel cannot take the page down, and
  `Symbols → Diagnose` prints **bars per timeframe, zones and the warm-up error for
  every instrument** — the answer to "why is this contract empty" without reading a log.

## Verify it yourself

```bash
cd smc-radar
../.venv/bin/python -m pytest tests -q                    # 90 passed
rm -f data/journal.sqlite3*
../.venv/bin/python -u run.py replay data/fut 60 --ticks 6 --port 8800 &
bash tools/smoke.sh                                       # 15 checks, incl. the archive
cd tools/ui
npm install --silent jsdom ws
node quality.mjs && node algo1m.mjs && node transports.mjs && node layout.mjs
```

`tools/ui/layout.mjs` is new: it guards the three rules that were actually broken (no
inner scroll box, nothing spills out of a card, a wide screen gets a third column) and
the behaviour that is not about pixels — a stored signal keeps its outcome badge, a
failed poll keeps the rows on screen, a decoder error reads like a sentence, two toasts
are two messages, and clicking a stored signal opens its full record.

**Not verified:** there is no browser engine in this sandbox, so the exact pixel layout
is reasoned, not photographed. Everything verifiable without one — the CSS rules, the
DOM the page builds, the data it draws from, the API contract, the database — is
covered by the suites above.
