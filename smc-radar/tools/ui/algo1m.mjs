// "BTC 1m algo candle high is not being shown" - the visibility guard.
// Checks that the page, with its default view, puts 1m algo-candle-high zones
// on screen (active ones where they exist, and the recently mitigated ones),
// with the time they formed, and that nothing about the detection changed.
import { JSDOM } from "jsdom";
import WebSocket from "ws";

const BASE = process.env.BASE || "http://localhost:8800";
const fail = [];
const ok = (c, l) => { console.log((c ? "PASS " : "FAIL ") + l); if (!c) fail.push(l); };
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const dom = await JSDOM.fromURL(BASE + "/", {
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
    win.WebSocket = class extends WebSocket { constructor(u) { super(String(u)); } };
    win.EventSource = class { constructor() { setTimeout(() => this.onerror && this.onerror(new Error("n/a")), 10); } close() {} };
  },
});
const w = dom.window;
const doc = w.document;
const ev = (code) => w.eval(code);
await wait(7000);

// --- the server really has 1m algo-candle-high zones for BTC
const all = await (await fetch(BASE + "/api/zones?status=all&limit=4000")).json();
const algo1m = all.items.filter((z) => z.timeframe === "1m" && ["algo_bear", "very_strong_algo_bear"].includes(z.kind));
console.log("server: 1m algo-candle-high zones =", algo1m.length, "| active =", algo1m.filter((z) => z.status === "active").length);
ok(algo1m.length > 0, "the engine still detects 1m algo candles at highs on BTC");

// --- the page shows them by default
ok(ev('document.getElementById("showMode").value') === "recent", "default view is 'active + recently mitigated'");
await wait(1500);
ev('document.getElementById("showMode").dispatchEvent(new window.Event("change"))');
await wait(1500);

// pick the "Algo candle · high" tab
const tab = [...doc.querySelectorAll("#tabs .tab")].find((t) => t.textContent.trim().startsWith("Algo candle · high"));
ok(!!tab, "the Algo candle · high tab exists");
tab.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
await wait(900);

// limit to the 1m timeframe
const chip = [...doc.querySelectorAll("#tfChips .chip")].find((c) => c.textContent.trim() === "1m");
ok(!!chip, "the 1m timeframe chip exists");
chip.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
await wait(900);

const cards = [...doc.querySelectorAll("#cards .zone")];
console.log("cards on screen:", cards.length);
ok(cards.length > 0, "the list is not empty: 1m algo-candle-high zones are on screen");
console.log("history loaded:", ev("(state.historyItems||[]).length"), "zones");
const kinds = new Set(cards.map((c) => c.querySelector(".kindtag").textContent.trim()));
ok([...kinds].every((k) => k === "Algo candle · high" || k === "Very strong AC · high"), "only high-side algo kinds: " + [...kinds]);
ok(cards.every((c) => /1m/.test(c.querySelector(".tf").textContent)), "only 1m zones");
ok(cards.every((c) => /formed/.test(c.textContent)), "every card says when it formed");
// widen the window: a replay plays history, so "recent" spans replay hours
ev('document.getElementById("recentWindow").value = "1440"; document.getElementById("recentWindow").dispatchEvent(new window.Event("change"))');
await wait(1500);
const wideCards = [...doc.querySelectorAll("#cards .zone")];
ok(wideCards.length > cards.length, `the window widens the list (${cards.length} -> ${wideCards.length} cards)`);
const withMitigation = wideCards.filter((c) => /mitigated/.test(c.textContent));
console.log("of which show their mitigation:", withMitigation.length);
ok(withMitigation.length > 0, "mitigated zones keep their card with the time and price (" + withMitigation.length + ")");
ok(/recently mitigated/.test(doc.getElementById("shown").textContent), "the header counts active vs recently mitigated: " + doc.getElementById("shown").textContent);

// --- switching back to "active only" is one click, and still works
ev('document.getElementById("showMode").value = "active"; document.getElementById("showMode").dispatchEvent(new window.Event("change"))');
await wait(800);
const activeOnly = [...doc.querySelectorAll("#cards .zone")];
ok(activeOnly.every((c) => !c.classList.contains("resolved")), "the active-only view still exists");

console.log(fail.length ? `\n${fail.length} FAILED` : "\nALL CHECKS PASSED");
process.exit(fail.length ? 1 : 0);
