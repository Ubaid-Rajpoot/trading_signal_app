// Transport checks: the map must keep updating over ANY of the three paths.
//   1. websocket  (the normal case)
//   2. server-sent events, when the websocket is blocked
//   3. plain polling, when even EventSource is unavailable
//
//   node transports.mjs            (BASE=http://localhost:8800)
import { JSDOM } from "jsdom";
import WebSocket from "ws";
import { EventSource } from "eventsource";

const BASE = process.env.BASE || "http://localhost:8800";
const fail = [];
const ok = (c, l) => { console.log((c ? "PASS " : "FAIL ") + l); if (!c) fail.push(l); };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function makeDom(mode) {
  return JSDOM.fromURL(BASE + "/", {
    runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(win) {
      const ctx = new Proxy({}, { get: (t, k) => (k in t ? t[k] : () => ({ width: 10 })), set: (t, k, v) => ((t[k] = v), true) });
      win.HTMLCanvasElement.prototype.getContext = () => ctx;
      Object.defineProperty(win.HTMLElement.prototype, "clientWidth", { get() { return 560; }, configurable: true });
      Object.defineProperty(win.HTMLElement.prototype, "clientHeight", { get() { return 360; }, configurable: true });
      win.HTMLElement.prototype.getBoundingClientRect = function () {
        return { left: 0, top: 0, width: 560, height: 360, right: 560, bottom: 360, x: 0, y: 0 };
      };
      win.fetch = (u, o) => fetch(new URL(u, BASE).toString(), o);
      if (mode === "poll") {
        // a hostile network: websockets throw, EventSource does not exist
        win.WebSocket = class { constructor() { throw new Error("websockets blocked"); } };
        delete win.EventSource;
      } else if (mode === "sse") {
        win.WebSocket = class { constructor() { throw new Error("websockets blocked"); } };
        win.EventSource = class extends EventSource {
          constructor(url) { super(new URL(url, BASE).toString()); }
        };
      } else {
        win.WebSocket = class extends WebSocket { constructor(url) { super(String(url)); } };
        win.EventSource = class { constructor() { setTimeout(() => this.onerror && this.onerror(new Error("n/a")), 10); } close() {} };
      }
    },
  });
}

async function check(mode, expected) {
  const dom = await makeDom(mode);
  const w = dom.window;
  await wait(7000);
  const kind = w.eval("link.kind");
  ok(kind === expected, `[${mode}] transport is ${kind}`);
  const seq1 = w.eval("state.data && state.data.seq");
  await wait(4500);
  const seq2 = w.eval("state.data && state.data.seq");
  ok(typeof seq1 === "number" && seq2 > seq1, `[${mode}] the map keeps advancing without a restart (seq ${seq1} -> ${seq2})`);
  ok(w.document.querySelectorAll("#cards .zone").length > 0, `[${mode}] zone cards render`);
  ok(w.eval("chart.bars.length") > 50, `[${mode}] the chart got its candles (${w.eval("chart.bars.length")})`);
  ok(w.eval("state.data.ticks_seen") > 0, `[${mode}] ticks are counted (${w.eval("state.data.ticks_seen")})`);
  w.close();
}

await check("ws", "ws");
await check("sse", "sse");
await check("poll", "poll");

console.log(fail.length ? `\n${fail.length} FAILED` : "\nALL CHECKS PASSED");
process.exit(fail.length ? 1 : 0);
