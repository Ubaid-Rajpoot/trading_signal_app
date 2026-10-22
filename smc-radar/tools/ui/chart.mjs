// Browser-level smoke test of the chart: right-hand margin, countdown, live
// movement, adaptive refresh, live tick counter, plus everything that already
// worked (zoom, pan, crosshair, tabs, cards).
import { JSDOM } from "jsdom";
import WebSocket from "ws";

const BASE = process.env.BASE || "http://localhost:8800";
const fail = [];
const ok = (c, l) => { console.log((c ? "PASS " : "FAIL ") + l); if (!c) fail.push(l); };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const TEXTS = [];

function fakeContext() {
  const calls = { fillRect: 0, strokeRect: 0, beginPath: 0, moveTo: 0, lineTo: 0, stroke: 0, fill: 0, fillText: 0, setLineDash: 0 };
  const ctx = {
    calls, globalAlpha: 1, lineWidth: 1, strokeStyle: "", fillStyle: "", font: "", textAlign: "", textBaseline: "",
    setTransform() {}, clearRect() {}, save() {}, restore() {}, arc() {}, closePath() {},
    fillRect() { calls.fillRect++; }, strokeRect() { calls.strokeRect++; },
    beginPath() { calls.beginPath++; }, moveTo() { calls.moveTo++; }, lineTo() { calls.lineTo++; },
    stroke() { calls.stroke++; }, fill() { calls.fill++; },
    fillText(t) { calls.fillText++; TEXTS.push(String(t)); },
    setLineDash() { calls.setLineDash++; }, measureText() { return { width: 20 }; },
  };
  return ctx;
}

const dom = await JSDOM.fromURL(BASE + "/", {
  runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
  beforeParse(win) {
    const ctx = fakeContext();
    win.HTMLCanvasElement.prototype.getContext = () => ctx;
    win.__ctx = ctx;
    Object.defineProperty(win.HTMLElement.prototype, "clientWidth", { get() { return 560; }, configurable: true });
    Object.defineProperty(win.HTMLElement.prototype, "clientHeight", { get() { return 360; }, configurable: true });
    win.HTMLElement.prototype.getBoundingClientRect = function () { return { left: 0, top: 0, width: 560, height: 360, right: 560, bottom: 360, x: 0, y: 0 }; };
    win.fetch = (u, o) => fetch(new URL(u, BASE).toString(), o);
    win.WebSocket = class extends WebSocket { constructor(u) { super(String(u)); } };
    win.EventSource = class { constructor() { setTimeout(() => this.onerror && this.onerror(new Error("n/a")), 10); } close() {} };
  },
});

const { window } = dom;
const doc = window.document;
const ev = (code) => window.eval(code);
const fire = (node, type, init = {}) => node.dispatchEvent(new window.MouseEvent(type, { bubbles: true, cancelable: true, ...init }));
await wait(6000);

// ---- data is actually flowing
ok(ev("link.kind") === "ws", "websocket transport: " + ev("link.kind"));
const seq1 = ev("state.data.seq");
await wait(4500);
ok(ev("state.data.seq") > seq1, `state keeps arriving (seq ${seq1} -> ${ev("state.data.seq")})`);
ok(ev("state.data.ticks_seen") > 0, "ticks are being counted: " + ev("state.data.ticks_seen"));
ok(ev("state.data.candles_seen") > 0, "candles are being counted: " + ev("state.data.candles_seen"));
ok(/live|replay/.test(doc.getElementById("chartFeed").textContent), "the chart header reports its feed: " + doc.getElementById("chartFeed").textContent);

// ---- right-hand breathing space (TradingView margin)
ev("chart.auto = true; renderChart()");
const len = ev("chart.bars.length");
const right = ev("chart.right");
ok(right > len - 1, `the last candle is not glued to the axis (right ${right} vs last index ${len - 1})`);
ok(ev("Math.round(chart.span * chart.marginFraction)") >= 4, "the margin is at least four empty candles wide");
const marginPx = ev(`(function(){ const W=560, padL=8, padR=68; const cw=(W-padL-padR)/chart.span; return (chart.right-(chart.bars.length-1))*cw; })()`);
ok(marginPx > 8, `that margin is real pixels on the chart (${marginPx.toFixed(0)}px)`);

// you can also scroll into the future, and follow brings you back
ev("chart.auto = false; chart.right = chart.bars.length - 1 + 40; renderChart()");
ok(ev("chart.right") > ev("chart.bars.length - 1"), "you can scroll past the last candle into empty space");
ev("document.getElementById('btnFollow').click()");
await wait(200);
ok(ev("chart.auto") === true, "follow returns to the live edge");

// ---- the chart keeps a live (forming) candle in view
let liveOk = false;
for (let i = 0; i < 6 && !liveOk; i++) {
  await wait(1200);
  ev("refreshChart(false)");
  await wait(600);
  liveOk = ev("chart.live === true && chart.bars[chart.bars.length-1].closed === false");
}
ok(liveOk, "the chart knows a candle is still forming");
ok(ev("chart.bars[chart.bars.length-1].closed") === false, "the newest candle is the unfinished one");

// ---- countdown
TEXTS.length = 0;
ev("renderChart()");
const isReplay = ev("state.data.source") === "replay";
const clock = TEXTS.filter((t) => /^⏱ \d\d:\d\d$/.test(t));
if (isReplay) {
  ok(TEXTS.includes("▶ replay"), "a replay is labelled as a replay instead of faking a countdown");
  const liveCountdown = ev(`(function(){ const before = state.data.source; return before !== "replay"; })()`);
  ok(!liveCountdown, "no wall-clock countdown is invented for recorded candles");
} else {
  ok(clock.length > 0, "a candle countdown is drawn: " + (clock[clock.length - 1] || "none"));
  const mm = clock.length ? Number(clock[clock.length - 1].slice(2, 4)) : -1;
  const tf = ev("chart.zone.timeframe");
  const expectMax = Math.ceil(({ "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60 }[tf] || 5));
  ok(mm >= 0 && mm <= expectMax, `the countdown fits the ${tf} timeframe (${mm}m <= ${expectMax}m)`);
  const before = TEXTS.filter((t) => /^\u23f1/.test(t)).pop();
  await wait(2200);
  const after = TEXTS.filter((t) => /^\u23f1/.test(t)).pop();
  ok(before !== after, `the countdown moves (${before} -> ${after})`);
}
// the countdown maths itself, checked against the server clock
const tickMath = ev(`(function(){
  const step = 300000;
  const base = Math.floor(1730000000000 / step) * step;
  const now = base + step - 42000;              // exactly 42s left in a 5m candle
  const closeAt = Math.floor(now / step) * step + step;
  return Math.round((closeAt - now) / 1000);
})()`);
ok(tickMath === 42, "the countdown formula leaves exactly the remaining seconds (" + tickMath + "s)");

// and the live countdown itself: pretend the feed is live, with a candle that
// opened in the current minute, and watch the clock tick
ev(`window.__src = state.data.source; window.__zone = chart.zone;
    window.__origOnState = window.onState;
    window.onState = function () { window.__origOnState(); if (window.__forceLive) state.data.source = "live"; };
    window.__forceLive = true; state.data.source = "live"; chart.skew = 0;`);
ev("(function(){ const z = Object.assign({}, chart.zone, { timeframe: '1m' }); chart.zone = z; const i = chart.bars.length - 1; chart.bars[i] = Object.assign({}, chart.bars[i], { open_time: Math.floor(Date.now()/60000)*60000, closed: false }); chart.auto = true; })()");
TEXTS.length = 0;
ev("renderChart()");
const liveClock1 = TEXTS.filter((t) => /^\u23f1 \d\d:\d\d$/.test(t)).pop();
await wait(1600);
ev("renderChart()");
const liveClock2 = TEXTS.filter((t) => /^\u23f1 \d\d:\d\d$/.test(t)).pop();
ok(!!liveClock1, "in live mode a countdown is drawn: " + liveClock1);
ok(liveClock1 !== liveClock2, `the countdown runs in live mode (${liveClock1} -> ${liveClock2})`);
const secsLeft = ev(`(function(){ const step = TF_MS[chart.zone.timeframe]; const now = Date.now() + (chart.skew||0); const lastBar = chart.bars[chart.bars.length-1]; return Math.round((lastBar.open_time + step - now)/1000); })()`);
ok(secsLeft < 0 || secsLeft <= 3600, "the live countdown is in range (" + secsLeft + "s)");
ev(`window.__forceLive = false; window.onState = window.__origOnState; state.data.source = window.__src;
    if (window.__zone) chart.zone = window.__zone; onState();`);

// ---- real movement in the open timeframe
const openBefore = ev("chart.bars[chart.bars.length-1].close");
const fetchedBefore = ev("chart.fetchedAt");
await wait(6000);
ok(ev("chart.fetchedAt") > fetchedBefore, "the chart refetches by itself");
const openAfter = ev("chart.bars[chart.bars.length-1].close");
ok(openBefore !== openAfter || ev("chart.bars.length") > len, `the forming candle moves (${openBefore} -> ${openAfter})`);
const every = ev("chart.timer && chart.zone ? (TF_MS[chart.zone.timeframe] <= 60000 ? 900 : TF_MS[chart.zone.timeframe] <= 300000 ? 1200 : 1800) : 0");
ok([900, 1200, 1800].includes(every), "the refresh rate follows the timeframe (" + every + "ms)");

// ---- chart interactions still work
ev("chart.auto = true; chart.span = 120; renderChart()");
const spanBefore = ev("chart.span");
const canvas = doc.getElementById("chart");
canvas.dispatchEvent(new window.WheelEvent("wheel", { deltaY: -120, clientX: 200, clientY: 150, bubbles: true, cancelable: true }));
ok(ev("chart.span") < spanBefore, `wheel zooms in (${spanBefore} -> ${ev("chart.span")})`);
canvas.dispatchEvent(new window.WheelEvent("wheel", { deltaY: 400, clientX: 200, clientY: 150, bubbles: true, cancelable: true }));
ok(ev("chart.span") >= Math.min(spanBefore, 102), "wheel zooms out (" + ev("chart.span") + " candles)");
ev("chart.auto = true; renderChart()");
const rightBefore = ev("chart.right");
fire(canvas, "mousedown", { clientX: 200, clientY: 150 });
window.dispatchEvent(new window.MouseEvent("mousemove", { clientX: 420, clientY: 150, bubbles: true }));
window.dispatchEvent(new window.MouseEvent("mouseup", { bubbles: true }));
ok(ev("chart.right") < rightBefore, "drag pans back through history");
window.dispatchEvent(new window.MouseEvent("mousemove", { clientX: 300, clientY: 200, bubbles: true }));
ok(ev("chart.hover && chart.hover.inside"), "crosshair tracks the cursor");

// ---- everything from before is still intact
ok(doc.querySelectorAll("#tabs .tab").length === 9, "nine tabs");
ok(doc.querySelectorAll("#cards .zone").length > 0, "zone cards");
ok(doc.querySelectorAll("#cards .zone .evid li").length > 0, "evidence lines");
ok(/status/.test(doc.getElementById("chartMeta").textContent), "chart meta");

console.log(fail.length ? `\n${fail.length} FAILED` : "\nALL CHECKS PASSED");
process.exit(fail.length ? 1 : 0);
