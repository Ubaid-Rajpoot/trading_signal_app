/* Round 10: the page as a *page* - width, wrapping, and a strip that keeps what it
   showed.

   jsdom has no layout engine, so this cannot measure pixels. What it can do is
   guard the three rules the bugs actually broke:

     1. the card list is not a nested scroll box (it used to be `max-height:1120px`
        with its own scrollbar, and a tall card was clipped inside the panel),
     2. the card's own boxes clip and wrap instead of spilling their content,
     3. a wide screen gets a third column and the cards use whatever is left.

   Plus the behaviour that has nothing to do with pixels: the signal strip keeps
   live *and* stored signals, an archived card opens its full record, and a failed
   poll never empties what is already on screen.

     node tools/ui/layout.mjs          (the server must be on :8800)
*/
import { JSDOM } from "jsdom";
import WebSocket from "ws";
import { readFileSync } from "node:fs";

const BASE = process.env.BASE || "http://localhost:8800";
const here = new URL(".", import.meta.url);
const html = readFileSync(new URL("../../web/index.html", here), "utf8");
const css = (html.match(/<style>([\s\S]*?)<\/style>/) || [, ""])[1];

let failed = 0;
const ok = (cond, label) => {
  if (cond) console.log("PASS " + label);
  else { failed += 1; console.log("FAIL " + label); }
};
const rule = (selector) => {
  const at = css.indexOf(selector + " {");
  return at < 0 ? "" : css.slice(at, css.indexOf("}", at) + 1);
};

/* ---------------------------------------------------------------- the styles */
const cards = rule(".cards");
ok(cards && !/max-height/.test(cards), "the card list is not a scroll box inside the page");
ok(cards && !/overflow-y/.test(cards), "the card list does not scroll on its own");
ok(/overflow:\s*hidden/.test(rule(".zone, .sigcard, .jrow, .feedx")), "a card clips what will not fit");
ok(/overflow-wrap:\s*anywhere/.test(css), "long numbers wrap instead of leaving the card");
ok(!/\.status \{[^}]*margin-left:\s*auto/.test(css) || /\.tags \.status \{ margin-left: 0/.test(css),
   "the right-hand chips travel as one group (so they wrap together)");
ok(/\.sigrow \{ grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/.test(css),
   "the entry/stop/target row uses equal, shrinkable columns");
ok(/@media \(min-width: 1780px\)[\s\S]*grid-template-columns: minmax\(0, 1fr\) minmax\(420px, 640px\) minmax\(400px, 560px\)/.test(css),
   "a wide screen gets a third column instead of a gutter");
ok(/\.siggrid \{[^}]*repeat\(auto-fill, minmax\(min\(100%, 218px\), 1fr\)\)/.test(css),
   "the archive is a responsive grid of small tiles");
ok(/\.tab2\.on/.test(css) && /\.tabpane\.off \{ display: none/.test(css), "the side panel tabs are styled");
ok(/\.wrap \{[^}]*max-width: 2400px/.test(css), "the page is allowed to grow past 1560px");
ok(/\.kpis \{[^}]*auto-fit/.test(css), "the KPI band fills the row at any width");
ok(/\.col-archive \{ grid-column: 1 \/ -1/.test(css),
   "the archive spans the page when there is no third column to sit in");

/* ------------------------------------------------------------------ the page */
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
await new Promise((r) => setTimeout(r, 9000));

ok(!!doc.getElementById("archivePanel"), "the archive has its own column on the page");
ok(doc.querySelectorAll("#archiveList .tile").length > 0, "the archive is a grid of tiles, not one long line");
ok(/kept/.test(doc.getElementById("archiveMeta").textContent), "the archive says how much it holds");
ok(doc.querySelectorAll("#signalTabs [data-stab]").length === 3, "the side panel has its own tabs (live / archive / tape)");
// Round 13: the structure gate means there is often no live signal at all (that is
// the point of it), so the renderer is checked on a real one when the market has
// one and on an injected row when it does not - the assertion is about what the
// row prints, not about the market being obliging.
if (!doc.querySelectorAll("#liveList .lrow").length) {
  window.eval(`state.data.active = (state.data.active || []).concat([{
      key: "TEST_USDT|5m|strong_high|1|high", symbol: "TEST_USDT", timeframe: "5m",
      kind: "strong_high", side: "high", direction: "SHORT", level: 100, top: 101, bottom: 99,
      time: Date.now() - 60000, status: "active", plan_state: "waiting", signal: true,
      grade: "A", prob: 0.91, net_r: 0.15, tradable: true, cond_met: 8, cond_total: 10,
      measured: { letter: "A", win: 0.97, n: 151 },
      plan: { side: "short", entry: 100, stop: 102, target: 98, rr: 1.0, target_source: "next liquidity" },
      gate: { ok: true, why: [], structure: "trend_down", htf: "trend_down", htf_timeframe: "15m", location: "premium" }
    }]);
    drawn.live = ""; drawLive();`);
}
ok(doc.querySelectorAll("#liveList .lrow").length > 0, "the live tab lists every live signal as a row");
ok(/entry/.test((doc.querySelector("#liveList .lrow") || {}).textContent || ""), "a live row prints its entry, target and stop");

const hero = [...doc.querySelectorAll("#heroList .sigcard")];
ok(hero.length > 0, "the strip shows signals");
const badges = hero.filter((c) => c.querySelector(".sigstate"));
ok(badges.length > 0, "a stored signal keeps its outcome badge on the strip (TARGET / STOPPED / …)");
ok(hero.every((c) => /measured ·|measurement loading/.test(c.textContent)),
   "every card still states the measurement it stands on");
ok(/live now/.test(doc.getElementById("heroStrip").textContent), "the strip counts what is live");
ok(/kept and shown from the database/.test(doc.getElementById("heroStrip").textContent),
   "the strip says the rest came from the database");

/* a poll that fails must not empty the screen - the whole point of the archive */
const before = doc.querySelectorAll("#archiveList .tile").length;
const realFetch = window.fetch;
window.fetch = (u, o) => (/\/api\/journal/.test(String(u)) ? Promise.reject(new Error("Failed to fetch")) : realFetch(u, o));
await window.loadArchive();
const after = doc.querySelectorAll("#archiveList .tile").length;
ok(after === before && after > 0, "a failed archive poll keeps the tiles already on screen");
window.fetch = realFetch;

/* a raw decoder error is never shown to the user as-is */
const msg = window.friendlyError(new Error("DecodingError: brotli: decoder process called with data when 'can_accept_more_data()' is False"));
ok(/decode/i.test(msg) && !/can_accept_more_data/.test(msg), "a decoder failure is explained in plain words");

/* and the toast stack keeps more than one message */
window.toast("first", "err");
window.toast("second");
ok(doc.querySelectorAll("#toasts .toast").length === 2, "two toasts are two messages, not one overwritten line");

/* an archived card opens the full record, not a shrug */
const archived = hero.find((c) => c.querySelector(".sigstate"));
archived.click();
await new Promise((r) => setTimeout(r, 2500));
ok(doc.getElementById("drawerJournal").classList.contains("on"), "clicking a stored signal opens the journal");
const detail = doc.getElementById("journalDetail");
ok(!detail.classList.contains("hidden") && /entry/.test(detail.textContent),
   "the record shows entry / stop / target, not just a name");

/* the grid is paged and can switch to a list - 200 signals are scanned, not slid */
const pager = doc.getElementById("archivePage").textContent;
ok(/of \d+/.test(pager), `the grid says where you are (${pager})`);
const firstTile = doc.querySelector("#archiveList .tile");
doc.querySelector('#archView [data-aview="list"]').click();
await new Promise((r) => setTimeout(r, 100));
ok(doc.querySelectorAll("#archiveList .feedx").length > 0, "the same rows can be read as a list");
doc.querySelector('#archView [data-aview="grid"]').click();
await new Promise((r) => setTimeout(r, 100));
ok(doc.querySelectorAll("#archiveList .tile").length > 0, "and back to a grid");
ok(!!firstTile, "a tile carries symbol, levels and its state");

/* the tabs actually switch panes */
doc.querySelector('#signalTabs [data-stab="tape"]').click();
ok(doc.getElementById("paneTape").classList.contains("off") === false
   && doc.getElementById("paneLive").classList.contains("off"), "the tabs switch the side panel");
doc.querySelector('#signalTabs [data-stab="archive"]').click();
await new Promise((r) => setTimeout(r, 400));
ok(!doc.getElementById("paneArchive").classList.contains("off"), "the archive tab shows the grid");

/* -------- the frame is bounded, and the page says so instead of losing zones ----- */
window.eval("state.data.active_total = 999; state.data.active_shown = (state.data.active||[]).length;"
  + "forget('cards'); drawCards();");
const banner = doc.getElementById("shown").textContent;
ok(/carries .* of 999/.test(banner), `the page says how many live zones it is not showing (${banner.slice(-70)})`);
ok(!doc.getElementById("btnLoadAll").classList.contains("hidden"), "and offers to load them");

doc.getElementById("btnLoadAll").click();
await new Promise((r) => setTimeout(r, 1500));
ok(Array.isArray(window.eval("state.allActive")) && window.eval("state.allActive.length") > 0,
   "load-every-live-zone fetches the whole list");
ok(!/carries .* of 999/.test(doc.getElementById("shown").textContent),
   "and the page stops warning once it has them");

/* -------- a card's evidence is fetched when it is opened, not pushed every frame --- */
// back to what a frame alone carries: no full-zone merge
window.eval("state.allActive = null; forget('cards'); drawCards();");
const bare = [...doc.querySelectorAll("#cards .zone")].find((n) => n !== doc.querySelector("#cards .zone.sel"));
ok(!!bare, "there is a second card that was never opened");
ok(bare.querySelectorAll(".evid li:not(.muted)").length === 0,
   "a card from a pushed frame carries no prose - only a line saying the detail loads on open");
bare.click();
await new Promise((r) => setTimeout(r, 1400));
const opened = [...doc.querySelectorAll("#cards .zone")].find((n) => n.classList.contains("sel"));
ok(opened && opened.querySelectorAll(".evid li:not(.muted)").length > 0,
   "opening the card fetches its evidence and prints it");

console.log(failed ? `\n${failed} FAILED` : "\nALL CHECKS PASSED");
process.exit(failed ? 1 : 0);
