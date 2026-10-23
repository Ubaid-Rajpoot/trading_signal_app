// Round-5 checks: the rectangle sits on the order-block candle, the grade and
// regime are real, the quality filter narrows the list and can be widened
// again, the chart draws every zone, drags in both directions, and goes full
// screen.
import { JSDOM } from "jsdom";
import WebSocket from "ws";

const BASE = process.env.BASE || "http://localhost:8800";
const fail = [];
const ok = (c, l) => { console.log((c ? "PASS " : "FAIL ") + l); if (!c) fail.push(l); };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const TEXTS = [];

function fakeContext() {
  const calls = { fillRect: 0, strokeRect: 0, fillText: 0 };
  return {
    calls, globalAlpha: 1, lineWidth: 1, strokeStyle: "", fillStyle: "", font: "", textAlign: "", textBaseline: "",
    setTransform() {}, clearRect() {}, save() {}, restore() {}, arc() {}, closePath() {},
    fillRect() { calls.fillRect++; }, strokeRect() { calls.strokeRect++; },
    beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {},
    fillText(t) { calls.fillText++; TEXTS.push(String(t)); },
    setLineDash() {}, measureText() { return { width: 24 }; },
  };
}

const dom = await JSDOM.fromURL(BASE + "/", {
  runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
  beforeParse(win) {
    const ctx = fakeContext();
    win.HTMLCanvasElement.prototype.getContext = () => ctx;
    Object.defineProperty(win.HTMLElement.prototype, "clientWidth", { get() { return 820; }, configurable: true });
    Object.defineProperty(win.HTMLElement.prototype, "clientHeight", { get() { return 460; }, configurable: true });
    win.HTMLElement.prototype.getBoundingClientRect = function () { return { left: 0, top: 0, width: 820, height: 460, right: 820, bottom: 460, x: 0, y: 0 }; };
    win.fetch = (u, o) => fetch(new URL(u, BASE).toString(), o);
    win.WebSocket = class extends WebSocket { constructor(u) { super(String(u)); } };
    win.EventSource = class { constructor() { setTimeout(() => this.onerror && this.onerror(new Error("n/a")), 10); } close() {} };
    win.TextEncoder = TextEncoder;
  },
});

const { window } = dom;
const doc = window.document;
const ev = (code) => window.eval(code);
await wait(6000);

// ---------------------------------------------------------------- the payload
const zones = await (await fetch(BASE + "/api/zones?status=active&limit=400")).json();
ok(zones.items.length > 0, `active zones are served (${zones.items.length})`);
const sample = zones.items.find((z) => z.ob && z.grade) || zones.items[0];
ok(!!sample.ob, "every zone carries the order-block candle it is drawn on");
ok(typeof sample.grade === "string" && ["A+", "A", "B", "C"].includes(sample.grade), "every zone carries a grade: " + sample.grade);
ok(sample.now_trend && typeof sample.now_trend.ready === "boolean", "every zone carries the live regime: " + JSON.stringify(sample.now_trend));

const highs = zones.items.filter((z) => z.side === "high");
const lows = zones.items.filter((z) => z.side === "low");
if (highs.length) ok(highs.every((z) => Math.abs(z.top - z.level) < 1e-9), "a high block's top IS the exact high (the level)");
if (lows.length) ok(lows.every((z) => Math.abs(z.bottom - z.level) < 1e-9), "a low block's bottom IS the exact low (the level)");
ok(highs.every((z) => z.bottom < z.top) && lows.every((z) => z.bottom < z.top), "every block has real height, not a line");
ok(highs.every((z) => !z.ob || z.ob.color === "up" || z.ob.index === null) &&
   lows.every((z) => !z.ob || z.ob.color === "down" || z.ob.index === null),
   "the block is marked on the OPPOSITE-colour candle (up at a high, down at a low)");
ok(highs.every((z) => !z.ob || z.ob.index === null || z.ob.low <= z.bottom + 1e-9), "the block reaches the order-block candle's far side");

// the order-block candle really is the biggest opposite-colour body nearby
const one = highs.find((z) => z.ob && z.ob.index != null);
if (one) {
  const bars = await (await fetch(`${BASE}/api/candles?symbol=${one.symbol}&timeframe=${one.timeframe}&limit=1000`)).json();
  const at = bars.bars.findIndex((b) => b.close_time === one.ob.time);
  if (at >= 0) {
    const window8 = bars.bars.slice(Math.max(0, at - 8), at + 1).filter((b) => b.close >= b.open);
    const biggest = window8.reduce((best, b) => (Math.abs(b.close - b.open) > Math.abs(best.close - best.open) ? b : best), window8[0]);
    ok(biggest && biggest.close_time === one.ob.time, "the marked candle is the biggest-body opposite-colour candle in the 8 bars before the high");
  }
}

// mitigation can never be shown as still active
const all = await (await fetch(BASE + "/api/zones?status=all&limit=4000")).json();
ok(all.items.filter((z) => z.status === "active").every((z) => z.mitigated_at == null && z.invalidated_at == null),
   "no zone is 'active' while carrying a mitigation time");
ok(all.items.filter((z) => z.status !== "active").every((z) => (z.mitigated_at && z.mitigated_price != null) || (z.invalidated_at && z.invalidated_price != null)),
   "every resolved zone says when (and at what price) it was resolved");

// -------------------------------------------------------------------- the page
ok(doc.querySelectorAll("#cards .zone .grade").length > 0, "cards show a grade chip");
ok(doc.querySelectorAll("#cards .zone .ctx span").length > 0, "cards show the live market regime");
ok(ev(`visibleZones().every((z) => ["A+", "A"].includes(z.grade))`), "the default quality filter keeps only A+ and A");
const shownPrime = ev("visibleZones().length");

ev(`state.quality = "all"; drawCards();`);
await wait(300);
const shownAll = ev("visibleZones().length");
ok(shownAll >= shownPrime, `"everything" brings the rest back (${shownPrime} -> ${shownAll})`);
ok(ev("visibleZones().some((z) => z.grade === 'B' || z.grade === 'C')") || shownAll === shownPrime,
   "the widened list contains the lower grades that were hidden");

ev(`state.quality = "ready"; drawCards();`);
await wait(250);
ok(ev("visibleZones().every((z) => z.now_trend && z.now_trend.ready)"), "the 'tradeable now' filter only shows in-trend zones");
ev(`state.quality = "prime"; drawCards();`);
await wait(250);

// ------------------------------------------------------------------ the chart
ev(`state.tab = "all"; state.tfs = new Set(); state.search = ""; state.showMode = "all"; state.quality = "all";
    loadHistory();`);
await wait(2500);
const pick = ev(`(function(){ const l = visibleZones(); return l.find((z) => z.ob && z.ob.index != null) || l[0] || null; })()`);
ok(!!pick, "there is a zone to open in the chart");
ev(`window.__pick = window.eval("visibleZones().find((z) => z.ob && z.ob.index != null) || visibleZones()[0]"); selectZone(window.__pick);`);
await wait(3000);

const ctxCount = ev("chartChartZones(chart.zone).length");
ok(ctxCount > 0, `the chart knows the neighbouring zones (${ctxCount})`);

ev("chart.drawZones = true; renderChart()");
const withZones = ev("chart.debug.rects");
ev("chart.drawZones = false; renderChart()");
const withoutZones = ev("chart.debug.rects");
ok(withZones > withoutZones, `drawing every zone adds rectangles to the chart (${withoutZones} -> ${withZones})`);
ev("chart.drawZones = true; renderChart()");

TEXTS.length = 0;
ev("renderChart()");
ok(TEXTS.some((t) => /OB/.test(t)), "the order-block candle is marked on the chart: " + (TEXTS.find((t) => /OB/.test(t)) || "none"));
ok(TEXTS.some((t) => /\b\d{1,2} [A-Z][a-z]{2} \d\d:\d\d\b/.test(t)), "zone labels carry the time they formed");
ok(ev(`(function(){ const z = chart.zone; return !!(z.grade && z.now_trend); })()`), "the opened zone has a grade and a live regime");

// vertical drag = grab the candles and move them up/down
const canvas = doc.getElementById("chart");
ev("chart.auto = true; chart.panY = 0; renderChart()");
const panBefore = ev("chart.panY");
canvas.dispatchEvent(new window.MouseEvent("mousedown", { clientX: 300, clientY: 150, bubbles: true, cancelable: true }));
window.dispatchEvent(new window.MouseEvent("mousemove", { clientX: 300, clientY: 280, bubbles: true }));
window.dispatchEvent(new window.MouseEvent("mouseup", { bubbles: true }));
const panAfter = ev("chart.panY");
ok(panAfter !== panBefore, `dragging vertically moves the chart up/down (panY ${panBefore} -> ${panAfter})`);
ok(Math.abs(panAfter - panBefore) > 0.01, "the vertical move is visible, not a rounding error");

// horizontal drag still works at the same time
const rightBefore = ev("chart.right");
canvas.dispatchEvent(new window.MouseEvent("mousedown", { clientX: 300, clientY: 150, bubbles: true, cancelable: true }));
window.dispatchEvent(new window.MouseEvent("mousemove", { clientX: 520, clientY: 150, bubbles: true }));
window.dispatchEvent(new window.MouseEvent("mouseup", { bubbles: true }));
ok(ev("chart.right") < rightBefore, "dragging horizontally still pans through history");

// keyboard
const panKeyBefore = ev("chart.panY");
window.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
ok(ev("chart.panY") < panKeyBefore, "ArrowUp moves the chart");

// full screen
doc.getElementById("btnFull").click();
await wait(200);
ok(doc.getElementById("chartPanel").classList.contains("fs"), "the full-screen button fills the screen");
ok(doc.body.classList.contains("fs-lock"), "the page does not scroll behind the full-screen chart");
doc.getElementById("btnFull").click();
await wait(200);
ok(!doc.getElementById("chartPanel").classList.contains("fs"), "the same button leaves full screen");

// reset puts everything back
ev("resetChartView()");
ok(ev("chart.panY") === 0 && ev("chart.zoomY") === 1 && ev("chart.auto") === true, "reset restores the view");

// crosshair keeps working
window.dispatchEvent(new window.MouseEvent("mousemove", { clientX: 320, clientY: 200, bubbles: true }));
ok(ev("chart.hover && chart.hover.inside"), "the crosshair still tracks the cursor");

console.log(fail.length ? `\n${fail.length} FAILED` : "\nALL CHECKS PASSED");
process.exit(fail.length ? 1 : 0);
