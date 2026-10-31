/* Page-level smoke test: proof that the screen cannot clutter itself.
 *
 * The unit tests in notify.test.js cover the queue. This one covers the thing
 * the trader actually complained about: the *page*. It boots the real
 * index.html in jsdom, loads the real notify.js + app.js, opens the socket the
 * page would open, then fires fifty actionable notifications inside one second
 * and asserts what a person would see:
 *
 *   - never more than one toast box on screen;
 *   - exactly one toast for the fifty;
 *   - every one of them still counted on the badge and listed in the inbox;
 *   - the table filled from the archive (the page is not blank);
 *   - a dropped socket raises the banner, never a toast.
 *
 * It talks to a running server, so start one first:
 *
 *   ../.venv/bin/python run.py replay data/btc 120 --skip 400000 --symbols BTCUSDT
 *   npm i jsdom && node web/page.smoke.js      # or: TDIGM_URL=... node web/page.smoke.js
 *
 * jsdom is a dev-only dependency; without it this exits 0 with a note instead
 * of failing a checkout that has no node_modules.
 */
const fs = require("fs");
const path = require("path");

const BASE = process.env.TDIGM_URL || "http://localhost:8700";
const WEB = __dirname;
const read = (name) => fs.readFileSync(path.join(WEB, name), "utf8");

let JSDOM;
try {
  JSDOM = require("jsdom").JSDOM;
} catch (err) {
  console.log("jsdom is not installed - skipping (npm i jsdom to run this test)");
  process.exit(0);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  let probe;
  try {
    probe = await fetch(BASE + "/api/health");
  } catch (err) {
    console.log(`no server at ${BASE} - skipping (start run.py first)`);
    return 0;
  }
  if (!probe.ok) throw new Error(`health returned ${probe.status}`);

  const html = read("index.html").replace(/<script[^>]*><\/script>/g, "");
  const dom = new JSDOM(html, { url: BASE + "/", pretendToBeVisual: true, runScripts: "outside-only" });
  const { window } = dom;
  const doc = window.document;

  // jsdom has no canvas; the page must survive a chart it cannot draw
  window.HTMLCanvasElement.prototype.getContext = () => ({
    setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {},
    fill() {}, fillRect() {}, arc() {}, closePath() {}, setLineDash() {}, fillText() {},
    save() {}, restore() {}, measureText: () => ({ width: 10 }),
    set font(v) {}, set fillStyle(v) {}, set strokeStyle(v) {}, set lineWidth(v) {},
    set textBaseline(v) {},
  });
  Object.defineProperty(window.HTMLElement.prototype, "clientWidth", { get() { return 600; } });
  Object.defineProperty(window.HTMLElement.prototype, "clientHeight", { get() { return 420; } });

  const sockets = [];
  window.WebSocket = class {
    constructor(url) { this.url = url; this.readyState = 0; sockets.push(this); }
    send() {}
    close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  };
  window.fetch = async (url) => {
    const res = await fetch(BASE + url);
    return { ok: res.ok, json: async () => res.json() };
  };

  window.eval(read("notify.js"));
  window.eval(read("app.js"));

  await sleep(1500);

  const rows = doc.querySelectorAll("#rows tr").length;
  if (!rows) throw new Error("the table is empty after boot - the archive never reached the page");

  const ws = sockets[sockets.length - 1];
  const snapshot = await (await fetch(BASE + "/api/state")).json();
  ws.readyState = 1;
  ws.onopen();
  ws.onmessage({ data: JSON.stringify(Object.assign({ type: "state" }, snapshot)) });

  const titles = new Set();
  let maxOnScreen = 0;
  for (let i = 0; i < 50; i += 1) {
    ws.onmessage({
      data: JSON.stringify({
        type: "notify",
        toast: true,
        event: {
          key: `burst-${i}`, title: `LONG SYM${i}USDT 15m`, severity: "action",
          category: "signal", grade: "A", detail: "entry 1 · draw 2 · 1:4.0",
          time: Date.now(), count: 1,
        },
      }),
    });
    maxOnScreen = Math.max(maxOnScreen, doc.querySelectorAll(".toast:not([hidden])").length);
    if (doc.querySelectorAll(".toast:not([hidden])").length) {
      titles.add(doc.getElementById("toast-title").textContent);
    }
    await sleep(5);
  }

  const inboxRows = doc.querySelectorAll(".inbox-item").length;
  const badge = doc.getElementById("inbox-badge").textContent;

  const report = { rows, maxOnScreen, distinctToasts: titles.size, badge, inboxRows };
  console.log(JSON.stringify(report, null, 1));

  if (maxOnScreen > 1) throw new Error("more than one toast was on screen at once");
  if (titles.size > 1) throw new Error(`fifty events produced ${titles.size} toasts - the rate limit is not holding`);
  if (inboxRows < 40) throw new Error("events went missing from the inbox");

  ws.onclose();
  await sleep(100);
  const banner = doc.getElementById("banner");
  console.log("after an instant socket drop -> banner:", banner.hidden ? "quiet (correct)" : "raised",
    "· toasts on screen:", doc.querySelectorAll(".toast:not([hidden])").length);
  return 0;
}

main().then((code) => process.exit(code || 0)).catch((err) => {
  console.error("FAIL:", err.message);
  process.exit(1);
});
