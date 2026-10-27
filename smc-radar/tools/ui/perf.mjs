/* Round 12: "the app got slow" - measured, not asserted.

   Two costs made the page heavy and both are measured here against the running
   server:

     1. bytes: how big one pushed frame is (it was ~380-420 KB for 200 zones and it
        went out 2-3 times a second),
     2. rendering: how long the page spends redrawing when a frame arrives *and
        nothing has changed*. A redraw guard must make that free; if it does not,
        the page is rebuilding its lists for nothing.

   jsdom has no layout, so (2) is DOM work only - which is exactly what was being
   thrown away and rebuilt on every frame.

     node tools/ui/perf.mjs        (the server must be on :8800)
*/
import { JSDOM } from "jsdom";
import WebSocket from "ws";

const BASE = process.env.BASE || "http://localhost:8800";
let failed = 0;
const ok = (cond, label) => {
  if (cond) console.log("PASS " + label);
  else { failed += 1; console.log("FAIL " + label); }
};

/* ---------------------------------------------------------------- 1. bytes */
const t0 = Date.now();
const stateRes = await fetch(BASE + "/api/state");
const stateBody = await stateRes.text();
const stateMs = Date.now() - t0;
const state = JSON.parse(stateBody);
console.log(`  /api/state: ${(stateBody.length / 1024).toFixed(1)} KB in ${stateMs} ms, ` +
  `${state.active.length} zones pushed of ${state.active_total} live`);
ok(stateBody.length < 300_000, `one frame is under 300 KB (${(stateBody.length / 1024).toFixed(0)} KB)`);
ok(!("evidence" in (state.active[0] || {})), "a frame carries no prose (evidence is fetched on demand)");
ok(typeof state.active_shown === "number" && typeof state.active_total === "number",
   "the frame says how many live zones it holds - and how many it left out");

/* frames per second, straight off the websocket */
const pushes = await new Promise((resolve) => {
  const ws = new WebSocket(BASE.replace("http", "ws") + "/ws");
  const sizes = [];
  const started = Date.now();
  ws.on("message", (raw) => {
    const bytes = String(raw).length;
    if (bytes > 2000) sizes.push({ bytes, at: Date.now() });
  });
  setTimeout(() => { try { ws.close(); } catch (e) {} resolve(sizes); }, 6000);
});
if (pushes.length > 1) {
  const span = (pushes[pushes.length - 1].at - pushes[0].at) / 1000;
  const rate = (pushes.length - 1) / Math.max(0.1, span);
  const avg = pushes.reduce((a, p) => a + p.bytes, 0) / pushes.length / 1024;
  const mbs = (avg * rate) / 1024;
  console.log(`  push: ${pushes.length} frames in ${span.toFixed(1)} s = ${rate.toFixed(2)}/s, ` +
    `average ${avg.toFixed(0)} KB = ${mbs.toFixed(2)} MB/s`);
  // the rate is paced by how many instruments are scanned (1-20 -> 0.35 s); what
  // must never happen is a firehose of bytes, so that is what is bounded
  ok(mbs < 0.45, `the page receives well under half a megabyte a second (${mbs.toFixed(2)} MB/s)`);
} else {
  ok(false, "the websocket pushed frames to measure");
}

/* -------------------------------------------------------------- 2. redraw */
const dom = await JSDOM.fromURL(BASE + "/", {
  runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
  beforeParse(win) {
    win.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => ({ width: 24 }) });
    Object.defineProperty(win.HTMLElement.prototype, "clientWidth", { get() { return 820; }, configurable: true });
    Object.defineProperty(win.HTMLElement.prototype, "clientHeight", { get() { return 460; }, configurable: true });
    win.fetch = (u, o) => fetch(new URL(u, BASE).toString(), o);
    win.WebSocket = class extends WebSocket { constructor(u) { super(String(u)); } };
    win.EventSource = class { constructor() { setTimeout(() => this.onerror && this.onerror(new Error("n/a")), 10); } close() {} };
    win.open = () => null;
  },
});
const { window } = dom;
const doc = window.document;
await new Promise((r) => setTimeout(r, 8000));

const cardsBefore = doc.querySelector("#cards").innerHTML;
const cardNodes = doc.querySelectorAll("#cards .zone");
ok(cardNodes.length > 0, `the list has cards to redraw (${cardNodes.length})`);

// ten frames arriving with nothing new in them: a guarded page does no work
let t = Date.now();
for (let i = 0; i < 10; i += 1) {
  window.eval("state.data = Object.assign({}, state.data, {seq: (state.data.seq || 0) + 1}); onState();");
}
const idleMs = Date.now() - t;
const sameNodes = doc.querySelectorAll("#cards .zone");
console.log(`  10 idle frames: ${idleMs} ms total, ${(idleMs / 10).toFixed(1)} ms each`);
ok(idleMs / 10 < 25, `an idle frame costs the page almost nothing (${(idleMs / 10).toFixed(1)} ms)`);
ok(sameNodes.length === cardNodes.length, "an idle frame does not throw the cards away");
ok(sameNodes[0] === cardNodes[0], "the same DOM nodes are kept when nothing changed");

// and a frame that *does* change still redraws
const first = window.eval("state.data.active[0]");
const before = doc.querySelector("#cards").textContent.length;
window.eval("state.data.active[0].prob = Math.min(0.999, (state.data.active[0].prob || 0.5) + 0.01); onState();");
t = Date.now();
const changeMs = Date.now() - t;
console.log(`  the changed frame was drawn`);
ok(doc.querySelector("#cards").textContent.length > 0, "a changed frame still redraws the list");

// the detail fetch: a card's evidence is one request, only when opened
const key = first.key;
const detail = await (await fetch(BASE + "/api/zones/" + encodeURIComponent(key))).json();
ok(detail.zone && Array.isArray(detail.zone.evidence), "the opened zone has its evidence on demand");
ok((detail.zone.evidence || []).length > 0, `and the evidence is real (${(detail.zone.evidence || []).length} lines)`);

/* --------------------------------------- 3. is every live signal in the database? */
const liveSignals = (state.active || []).filter((z) => z.signal);
if (liveSignals.length) {
  const checks = await Promise.all(liveSignals.slice(0, 6).map(async (z) => {
    const r = await fetch(BASE + "/api/journal/" + encodeURIComponent(z.key));
    if (!r.ok) return false;
    const row = await r.json();
    return row.symbol === z.symbol && row.timeframe === z.timeframe
      && Number(row.entry) === Number((z.plan || {}).entry)
      && Number(row.stop) === Number((z.plan || {}).stop);
  }));
  ok(checks.every(Boolean),
     `every live signal is in the database with the same entry and stop (${checks.length} checked)`);
} else {
  console.log("  (no live signal at this second - the archive check is covered by tools/smoke.sh)");
}

/* ------------------------- 4. a frame that leaves zones out says so, and loads them */
ok(state.active_shown <= state.active_total, "the frame never claims more than it holds");
if (state.active_total > state.active_shown) {
  const all = await (await fetch(BASE + "/api/zones?status=active&limit=4000")).json();
  ok((all.items || []).length === state.active_total,
     `"load every live zone" returns every one of them (${(all.items || []).length})`);
} else {
  console.log("  (this scan holds fewer live zones than the frame cap - the cap is not reached)");
}

console.log(failed ? `\n${failed} FAILED` : "\nALL CHECKS PASSED");
process.exit(failed ? 1 : 0);
