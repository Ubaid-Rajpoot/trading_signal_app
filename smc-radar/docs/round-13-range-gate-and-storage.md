# Round 13 — the five errors, the range gate, and the honest numbers

This round answers a list of concrete failures and one serious accusation. Every
number below is reproducible with the command shown next to it. Where something is
**not** verified here, it says so.

Environment: this sandbox has **no access to MEXC and no real browser**, so the
exchange paths are exercised against a fake exchange on a local socket (the exact
responses that failed are reproduced), and the page is exercised with jsdom. The
sentence "tested on my own PC" means *that*, not a live MEXC session.

---

## 1. `could not read the contract list: DecodingError: brotli: decoder process
called with data when 'can_accept_more_data()' is False`

**Cause.** Not the scanner's request — the exchange's *answer*. MEXC can serve a
plain JSON body under `content-encoding: br` (or a proxy has already decoded it).
Handed to a brotli decompressor, a body that is not brotli raises exactly that
message, because the decoder has nothing left it can accept. Asking for
`accept-encoding: gzip` does not help when the server answers `br` anyway.

**Fix.** The transport no longer lets httpx decode anything. It reads the body
**raw** (`resp.aiter_raw()`), then decides:

* a body that starts with `{` or `[` **is JSON** and is used as it is, whatever the
  header claims (that is the failing case);
* gzip and deflate are recognised by their magic bytes;
* brotli is tried if the `brotli` package happens to be installed;
* anything else becomes one readable `FeedError` naming the URL and the first
  bytes — never a third-party decoder's name.

Also in this round:

* **the contract list is retried** on a backoff instead of being given up for the
  whole session (that one failure is why "add symbol" sometimes had no list at
  all), and the last good list is kept and served with its age (`stale: true`);
* **timeouts**: the read timeout went 25 s → 45 s (the ticker endpoint is
  megabytes), the connect timeout stays 10 s.

Reproduce — a fake exchange that sends plain JSON labelled `br`, the real thing:

```bash
cd smc-radar && ../.venv/bin/python -m pytest tests/test_feed.py -q
# test_the_contract_list_survives_an_exchange_that_lies_about_brotli
```

## 2. `warm-up TON_USDT 1h: FeedError: MEXC futures returned no kline`

**Cause.** One timeframe with no history was recorded as a failure for the whole
instrument and left that map empty. A contract can genuinely have no `Min60`
candles in the window asked for (a fresh listing, a delisted series, a rate
limit), while its `Min1` series exists.

**Fix.** Three attempts per timeframe (full window → a much shorter window → a
retry after the pause), then a **roll-up from 1m**, which always exists on MEXC
futures. A rolled-up timeframe is no longer an error; only an instrument with no
1-*minute* history is reported, once, in `warm_problems` and the **Diagnose**
panel. The per-timeframe "thin" requirement is also honest now (an hourly map is
never demanded to hold 200 hourly candles).

Reproduce: `pytest tests/test_engine.py -q -k "higher_timeframe or really_has_no_history"`.

## 3. `re-requesting history …`, `sometimes a timeout`, `fee renewed, map restarted`

* **"re-requesting history"** repeated every two minutes for the whole session.
  The healer now speaks **only when the set of thin instruments changes**, and
  says what will happen next. The healer itself keeps working.
* **"timeouts"**: read timeout 45 s, one retry after a pause, and a shorter window
  on the second attempt (see §1, §2).
* **"…map restarted"**: that message only ever happens in a **replay** that loops
  back to the start of the recording. It now says so in words: *"the replay looped
  back to the start of the recording … a replay-only note, not a live error"*.

## 4. "A signal disappeared without being filled or invalidated" — the live half

This was a real bug, and it is not the frame cap from round 12.

**Cause.** A zone leaves `status == "active"` the moment price comes back into the
block. That is **exactly when its plan fills and the trade opens**. So the card
vanished at the instant the trade began: not completed, not invalidated, simply
no longer "active". Measured on the running app before the fix: **208 zones with an
open (filled) plan were absent from the live list**. In the same area, `_fill_plan`
also marked planless "weak" levels as filled, which would have pinned them to the
live list for ever once liveness followed the plan.

**Fix.** Liveness is the **plan**, not the block:

* a zone is live while its block is untouched, and **stays live while its trade is
  open** (`plan_state == "filled"`) until the target or the stop decides it;
* the journal agrees: entering the block no longer closes the row — the row stays
  **open** until target/stop (`OPEN_SQL`);
* `/api/zones?status=active`, the pushed frame, the cards, the tape and the
  "open now" count all use that one definition;
* `journal.count(status="open"/"closed")` was silently ignoring those two filters
  and returning the size of the whole archive — fixed.

## 5. "…and they should be properly saved" — the restart half

The rows were always in SQLite (`data/journal.sqlite3`); what was lost was the
*memory*. A restart threw the map away and marked every open plan `interrupted`.

**Fix.** At start-up the scanner now **adopts** the plans that were still open:
`journal.open_snapshots()` returns the stored zone snapshots (older than
`restore_hours: 12` are left to the reaper, at most `restore_max: 150`), and
`MarketMap.adopt()` puts them back with their entry/stop/target, so the arriving
candles keep deciding them. They are drawn with *"↺ adopted from the database at
start-up"*. Adoption happens **after** the history is loaded, so a plan is judged
on candles from now, not replayed against the pre-roll and expired on the way in
(the first attempt did exactly that, and the unit test pins it).

Measured on a real restart of the replay server: `restored: 150`, of which 49 were
still live a few seconds later (the rest hit their stop or target on the candles
that arrived — the fast replay loop makes that quick).

Reproduce: `pytest tests/test_engine.py -q -k "restart or journal_hands_back or filled_trade_stays_open"`,
then kill and restart the replay server and watch `/api/health` → `restored`.

---

## 6. "You give signals in a ranging market and claim 96 %" — the serious one

### 6a. What the PDF actually says (re-read, page by page)

The book is *Advanced ICT Institutional SMC Trading* (David Woods). The passages
that decide this question, verbatim:

| page | rule |
| --- | --- |
| 42 | *"Strong High is the High that Caused Manipulation and Structure Breaking … Weak High/Low is the Low that fail to break structure. For every strong LOW there is a weak HIGH."* |
| 38 | *"if the price breaks structure and fails to move to a discount or premium price … this low price is just liquidity and not an HL"* — a **deep retracement is not structure**. |
| 52 | *"external liquidity … is the leg's highest point"* — the target is the next liquidity pool, and a trend keeps making new ones. |
| 58 | *"**RANGES account for 80% of a price's swings**"* — the majority of the time there is no trend to trade. |
| 39 / 104 | *"go to the HTF and use the cycle to see where you are from the HTF structure"* — **top-down**: the bigger timeframe decides direction. |
| 42 / 110 | *"ABOVE THE BSL = PREMIUM, BELOW THE SSL = DISCOUNT"*, and the sell example demands *"extreme premium"* — **sells belong in premium, buys in discount**. |
| 87 | *"follow daily cycle > full fill FVG > mitigate LTF algo candle > reversal shift in market structure"* |

### 6b. What the code does now

`MarketMap.structure()` reads the market the way the book does, from confirmed
swing highs and lows only (nothing can see the future):

* **trend up** = higher high **and** higher low; **trend down** = lower high **and**
  lower low;
* **range** when the highs (or the lows) are equal within `structure_equal_atr`
  (0.15 ATR) — *liquidity being built, not structure* — or when the structure is
  mixed, or when price gave back more than `structure_deep_retrace` (79 %) of the
  last leg, or when the last major break points the other way;
* the **dealing range** and its middle come with it, so premium/discount is a
  number, not a vibe.

`MarketMap.trade_gate()` then decides whether a pattern may be called a **signal**:
its own timeframe must be trending, **the timeframe above must be trending in the
same direction** (the engine pushes the parent structure into every map on every
bar), and a sell must be in premium / a buy in discount. Weak levels are never
signals. `bot/model.py` now requires `gate.ok` **and** the measured probability
**and** the fee.

Every card says which test it failed, in words:

> *not a signal: 5m structure is not a trend (equal highs — liquidity being built,
> not structure (p.42, p.58)) · the block sits in discount — a sell belongs in
> premium (p.42)*

and a passing one says what it passed:

> *✓ trend aligned · trend_down · 15m trend down · premium*

### 6c. The measurement — and it does **not** flatter the gate

Held-out 90 days (Jan–Jun 2025, the model never saw this block), the plan exactly
as the app trades it (target = next liquidity pool ≥1 ATR away, stop beyond the
block), fee ≤ 0.30 R:

```bash
../.venv/bin/python tools/gate_study.py            # 90d holdout
```

| group | plans | /day | hit rate | 95 % CI | R:R | net R/plan |
| --- | --- | --- | --- | --- | --- | --- |
| p≥0.85, gate **off** | 1691 | 18.8 | 95.4 % | 94–96 % | 0.35 | +0.16 |
| p≥0.85, gate **on** | 44 | 0.49 | **97.7 %** | 88–100 % | 0.29 | +0.14 |
| p≥0.90, gate off | 426 | 4.7 | 97 % | 95–99 % | 0.35 | +0.16 |
| p≥0.90, gate on | 16 | 0.18 | 100 % | 81–100 % | 0.28 | +0.15 |
| p≥0.75, gate off | 3885 | 43.2 | 91 % | 91–92 % | 0.33 | +0.11 |
| p≥0.75, gate on | 95 | 1.06 | 95 % | 88–98 % | 0.30 | +0.10 |

Only **1.7 %** of every plan the map forms (346 of 20 717 on the holdout) passes the
full gate — the own-timeframe structure *and* the higher timeframe *and* the
premium/discount location. On one instrument that is 0.5–1 signal a day at the
shipped cut; across sixty contracts it is a live list, not a trickle.

So, straight: **the ranging-market signals were not, on this sample, the disaster
the complaint assumes.** They hit their (small) target 95.4 % of the time; the
gated ones 97.7 %. The gate removes **97 % of the signals** for **+2 points of hit
rate and −0.02 R of expectancy**. It is applied because it is *the rule the user
stated and the book states*, not because it is more profitable — and it can be
switched off with `"structure_gate": false` in the settings.

(A first pass of this measurement was wrong for a wiring reason worth recording:
the engine pushed the higher timeframe into `map.context`, but `_add_zone` graded
the zone with an empty context, so the "bigger trend must agree" test silently did
nothing while every unit test passed, because they passed the context by hand. The
live app showed `htf: null` on every card, which is how it was caught.
`test_a_zone_is_graded_with_the_timeframe_above_it` pins it now.)

### 6d. So why did the account empty? The arithmetic, not the range

The plan's own geometry does it. At the shipped cut the median plan is
**R:R 0.31**: the target is a third of the stop. A 97 % hit rate then looks like
this:

```
0.974 × (+0.31 R)  −  0.026 × (−1.00 R)  =  +0.28 R gross
                                   − 0.16 R fees  =  +0.12 R per trade
```

* **one stop costs 3.2 wins.** A 3 % outcome is not rare; it is one trade in 34.
* at "96 % accuracy" sizing (say 20–30 % of the account per trade, which is what a
  96 % number invites) a single stop is a wiped account. That is arithmetic, and it
  is the honest answer to *"I followed one signal and my account was wiped out"*:
  the probability was never a guarantee, the payoff was thin, and the position size
  did the rest.
* the bands below B are negative (−0.05 R at p≥0.60), so the page never calls them
  signals.

**What the page now enforces so this cannot be misread again:**

* every percentage carries its **sample size** (`97 % · n=151`) and the tooltip
  says it was measured on 90 days the model never saw;
* a plan with R:R < 1 prints, in amber: *"R:R 0.31 means one stop costs 3.2 wins —
  size the trade so a single loss is small, never as if the 97 % were a guarantee"*;
* the structure gate's verdict (pass or the reason it failed) is on every card;
* `docs/signals.md` and `/api/model` ship the same numbers.

**The lever I have not pulled, because it changes *your* rule:** the target is the
next liquidity level, and on 1-minute data the next pool is often a third of the
risk away. Requiring a farther pool (`plan_target_atr_min` 1.0 → 1.5/2.0) would
raise the payoff per win and lower the hit rate. Tell me which way you want to go
and I will measure both before touching the default.

---

## 7. Model, re-fitted on the plan the app actually trades

The old band table was fitted with `--stop-mode level` while the app ships
`plan_stop_mode: block` — i.e. the 97 % described a trade nobody was taking. Fixed:
the shipped `bot/signal_model.json` is fitted on the **block** plan, on 90 days,
validated on 90 more, and the bands are measured **with the structure gate on**:

| band | cut | measured hit rate (holdout) | n | 95 % CI | net R |
| --- | --- | --- | --- | --- | --- |
| A+ | ≥ 0.90 | 100 % | 16 | 81–100 % | +0.15 |
| A | ≥ 0.85 | 98 % | 44 | 88–100 % | +0.14 |
| B | ≥ 0.75 | 95 % | 95 | 88–98 % | +0.10 |
| C | ≥ 0.60 | 87 % | 186 | 82–91 % | −0.01 |

Calibration on the holdout is honest (predicted 91 % → actual 94 % in the top
bucket, 85 % → 86 % in the next), and every plan that is *not* in these bands has
a negative or near-zero expectancy. Note the **sample sizes**: A is 44 signals over
90 days, so its 95 % interval runs from 88 % to 100 %. That is what a number like
this honestly is — not the flat "97 %" the old page printed.

Reproduce (~3 min):

```bash
../.venv/bin/python tools/fit_model.py --stop-mode block --gate --out bot/signal_model.json
```

---

## 8. What is verified, and what is not

**Verified here**

* `pytest tests -q` → **115 passed** (transport bytes, warm-up roll-up, liveness,
  adoption, journal open/closed, the gate's rules, the model's signal rule).
* `tools/ui/layout.mjs`, `quality.mjs`, `algo1m.mjs`, `transports.mjs`, `perf.mjs`
  → all pass against the running replay server (frame ~63 KB, 0.19 MB/s, ~3/s).
* `tools/smoke.sh` → API + archive end to end.
* Real restart of the server: 150 open plans adopted, live list not empty.
* `tools/gate_study.py` and `tools/fit_model.py` → every number in §6c and §7.

**Not verified here**

* a live MEXC session: this sandbox cannot reach the exchange. The brotli case is
  reproduced against a local server that behaves the way MEXC did, and the
  `no kline` case against a feed double; both are the exact failures reported.
* pixels in a real browser (jsdom does not lay out). The page's behaviour, not its
  appearance, is what the harnesses cover.
* `tools/ui/chart.mjs` is flaky against the replay: its "the forming candle moves"
  check depends on a partial candle being present at the second it samples, and the
  replay emits partials only briefly. It is not part of the verified set above.
