# Notifications: how the page stays quiet

The complaint this design answers, in the trader's words: *"so many toast
notifications appear on the screen that it remains constantly cluttered."*

The old page rendered every event it received, twice — once from the socket and
again from the poll that rediscovered the same signals — into a stack of boxes
that never stopped growing. There was no cap, no dedupe and no idea of what was
worth interrupting for. A notification system that fires constantly carries no
information: if everything is urgent, nothing is.

This is a rebuild of that subsystem, and the rule is simple:

> **At most one toast, at most one a minute. Everything else is counted, or
> waits in the inbox.**

---

## Three destinations, and every event goes to exactly one

| destination | what lands there | is it drawn? |
|---|---|---|
| **toast** | a *grade A* signal, or a real error | one box, replaces the last, 7 s, then gone |
| **inbox** | everything worth keeping: any signal, any warning, any state change | behind the bell and its unread badge |
| **drop** | chatter: the same line again, a filtered candidate, debug | never — only a counter moves |

The worst case for the trader is **"the bell has a number"**, never "the screen
is covered".

## The policy (server, `bot/notify.py`)

`NotificationBus` is the only place that decides. It is pure Python with an
injectable clock, so every rule below is a test in `tests/test_notify.py`.

| rule | value | why |
|---|---|---|
| the same key again within | **900 s** | a repeat is not news |
| the same *(category, title)* again within | **120 s** | merge into one row, `count += 1` |
| signals are **never** merged | — | two setups on one instrument share a title; folding them would hide a trade and swallow its toast |
| toast budget | **2 per 60 s** | a small burst for a genuine cluster, then silence |
| only severity `action` may toast | — | `info` / `warn` / `error` go to the inbox and the one banner |
| `debug` | dropped and counted | it is a log line, not a notification |
| inbox size | **400** | bounded, so a long replay cannot grow the process |

Counters — `unread`, `events`, `filtered` (setups the engine refused),
`suppressed` (duplicates and chatter) — are exposed at `/api/health` and
`/api/notifications`, and printed in the page's status bar as
*"N event(s) dropped by policy"*. Nothing is silently swallowed: a drop is
always visible as a number.

## The contract (client, `web/notify.js`)

The server decides what is worth *sending*; the page decides what is worth
*showing*, and is deliberately stricter, because a page that trusts the network
is one reconnect away from a wall of boxes.

1. **one toast on screen at a time** — a new one replaces the old, in place;
2. **never the same key twice**;
3. **never two toasts closer than 60 s** (a user-set "quiet off" lowers this to
   8 s; it never allows two on screen and never a duplicate);
4. everything else lands in the inbox and moves the unread badge;
5. the inbox is capped at **200** and remembers **1000** keys.

The page also refuses to render `log`, `alert` or `filtered` messages from the
socket at all. There is no code path from an engine log line to a box on the
screen — which is what makes cluttering impossible rather than merely unlikely.

`index.html` has exactly **one** `.toast` element. It is not a container and it
is not a list; a stack of them cannot exist.

## The rest of the page, and why it is quiet too

* **one banner** for real trouble (feed stopped, socket down) — a one-line strip
  under the header, not a pile of boxes;
* **the inbox** (bell, top right) is the full record: title, detail, grade, how
  long ago, grouped with `×N` counts;
* **counters in the status bar**: feed, frames, signals, stored, valid now, and
  how many events the policy stopped before they reached the screen;
* **the table** is fed by the archive (`/api/signals`, SQLite) overlaid with the
  live snapshot, so it survives a restart and never flickers empty between
  pushes;
* a dead socket falls back to HTTP polling and raises **the banner once** — it
  does not toast every retry.

## Endpoints

| route | returns |
|---|---|
| `GET /api/notifications?limit=` | the grouped events + counters |
| `POST /api/notifications/read` | clears the unread badge, keeps history |
| `GET /api/health` | `notifications: {unread, events, filtered, suppressed}` |

## Tests

```
node --test web/notify.test.js            # 13 assertions on the client contract
../.venv/bin/python -m pytest tests/test_notify.py -q   # 20 on the policy
node web/page.smoke.js                    # the real page in jsdom, server running
```

The page smoke test fires **fifty actionable notifications inside one second**
at the live page and asserts that exactly **one** toast was ever shown, that
never more than one box existed, that all fifty were on the badge and in the
inbox, and that the table was populated from the archive. That is the defect's
acceptance test, executed rather than promised.

## Limitations, stated plainly

* The policy lives **in the server process**: restarting the process clears the
  dedupe memory and the unread count. The *signals* are in SQLite and survive;
  the notification history does not (it is a glance, not a record).
* Toasts are the only thing the browser itself can show. If the tab is closed,
  nothing is delivered anywhere else — there is no email or push.
* The "quiet off" setting is per browser (`localStorage`), not per account.
* Replay time is historical, but the notification clock is wall-clock: two
  events that are days apart in the replay can arrive seconds apart and will be
  coalesced as if they were simultaneous. That is intentional (the screen cares
  about *now*), but it means replay counts are not the same as a live day's
  notification count.
