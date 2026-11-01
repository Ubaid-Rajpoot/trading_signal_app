/* Run with: node --test web/notify.test.js
 *
 * The notification contract is the thing the trader complained about, so it is
 * tested rather than trusted: one toast at a time, never a duplicate, never
 * faster than once a minute, and everything still reachable in the inbox.
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const { createQueue, describe } = require("./notify.js");

function queueAt(now) {
  let clock = now;
  const queue = createQueue({ clock: () => clock, minGapMs: 60_000 });
  return { queue, advance: (ms) => { clock += ms; }, at: () => clock };
}

const signal = (key, extra) => Object.assign(
  { key, title: `signal ${key}`, severity: "action", category: "signal" }, extra || {},
);

test("a single actionable signal toasts once", () => {
  const { queue } = queueAt(1_000_000);
  const out = queue.push(signal("a"), { toast: true });
  assert.equal(out.reason, "toast");
  assert.ok(out.toast);
  assert.equal(queue.unread, 1);
});

test("the same key is never shown twice", () => {
  const { queue, advance } = queueAt(1_000_000);
  queue.push(signal("a"), { toast: true });
  advance(120_000);
  const again = queue.push(signal("a"), { toast: true });
  assert.equal(again.reason, "duplicate");
  assert.equal(again.toast, null);
  assert.equal(queue.unread, 1);
});

test("a second signal inside the minute goes to the inbox, not the screen", () => {
  const { queue, advance } = queueAt(1_000_000);
  const first = queue.push(signal("a"), { toast: true });
  advance(5_000);
  const second = queue.push(signal("b"), { toast: true });
  assert.equal(first.reason, "toast");
  assert.equal(second.reason, "rate-limited");
  assert.equal(second.toast, null, "only one box may be on screen at a time");
  assert.equal(queue.unread, 2, "but the bell still moves");
});

test("after the gap another toast is allowed", () => {
  const { queue, advance } = queueAt(1_000_000);
  queue.push(signal("a"), { toast: true });
  advance(60_001);
  const next = queue.push(signal("b"), { toast: true });
  assert.equal(next.reason, "toast");
});

test("info events never toast, whatever the caller asks for", () => {
  const { queue } = queueAt(1_000_000);
  const out = queue.push({ key: "x", title: "warm-up BTCUSDT", severity: "info" }, { toast: true });
  assert.equal(out.toast, null);
  assert.equal(out.reason, "inbox");
});

test("repeats of the same title merge into one row with a count", () => {
  const { queue, advance } = queueAt(1_000_000);
  queue.push({ key: "w1", title: "warm-up BTCUSDT", severity: "info", category: "system" });
  advance(1000);
  queue.push({ key: "w2", title: "warm-up BTCUSDT", severity: "info", category: "system" });
  const rows = queue.list();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].count, 2);
  assert.equal(queue.unread, 1);
});

test("two different signals with the same title are two rows, never one", () => {
  const { queue, advance } = queueAt(1_000_000);
  queue.push({ key: "s1", title: "LONG BTCUSDT 15m", severity: "info", category: "signal", detail: "entry 100" });
  advance(2_000);
  queue.push({ key: "s2", title: "LONG BTCUSDT 15m", severity: "info", category: "signal", detail: "entry 110" });
  const rows = queue.list();
  assert.equal(rows.length, 2, "a shared title must not hide a second trade");
  assert.equal(queue.unread, 2);
});

test("a coalesced repeat updates its count in place instead of adding a row", () => {
  const { queue, advance } = queueAt(1_000_000);
  queue.push({ key: "w1", title: "warm-up BTCUSDT", severity: "info", category: "system", count: 1 });
  advance(1_000);
  const again = queue.push({ key: "w1", title: "warm-up BTCUSDT", severity: "info", category: "system", count: 3 });
  assert.equal(again.reason, "merged");
  assert.equal(again.event.count, 3);
  assert.equal(queue.list().length, 1);
});

test("chatter with the same title merges only inside the window", () => {
  const { queue, advance } = queueAt(1_000_000);
  queue.push({ key: "k1", title: "retry XYZ", severity: "info", category: "system" });
  advance(10 * 60_000);
  queue.push({ key: "k2", title: "retry XYZ", severity: "info", category: "system" });
  assert.equal(queue.list().length, 2, "ten minutes apart is a new event, not a repeat");
});

test("the inbox is bounded so a long replay cannot grow the page", () => {
  const { queue, advance } = queueAt(1_000_000);
  for (let i = 0; i < 500; i += 1) {
    advance(10);
    queue.push({ key: `k${i}`, title: `event ${i}`, severity: "info", category: "c" });
  }
  assert.equal(queue.list().length, 200);
});

test("marking read clears the badge but keeps the history", () => {
  const { queue } = queueAt(1_000_000);
  queue.push(signal("a"));
  assert.equal(queue.markRead(), 1);
  assert.equal(queue.unread, 0);
  assert.equal(queue.list().length, 1);
});

test("the gap can be widened or narrowed without losing the inbox", () => {
  const { queue, advance } = queueAt(1_000_000);
  queue.push(signal("a"), { toast: true });
  advance(5_000);
  assert.equal(queue.push(signal("b"), { toast: true }).reason, "rate-limited");
  queue.setMinGap(1_000);
  advance(5_000);
  assert.equal(queue.push(signal("c"), { toast: true }).reason, "toast");
  assert.equal(queue.list().length, 3, "history survives the setting change");
});

test("describe keeps the wording in one place", () => {
  assert.equal(describe({ title: "SHORT BTC", detail: "entry 100 · draw 90", count: 2 }),
    "SHORT BTC · entry 100 · draw 90 · ×2");
  assert.equal(describe(null), "");
});
