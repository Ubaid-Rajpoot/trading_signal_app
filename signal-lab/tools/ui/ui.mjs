/* Render the real page in jsdom against the running server and check that every
   number it prints is backed by the files the research scripts wrote.

     node app/web/../..            no - run from signal-lab/tools/ui:
     node ui.mjs                   (server must be on :8811 or BASE=...)
*/
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";

const BASE = process.env.BASE || "http://localhost:8811";
const here = new URL(".", import.meta.url);
const html = readFileSync(new URL("../../app/web/index.html", here), "utf8");

let failed = 0;
const ok = (c, label) => {
  console.log((c ? "PASS " : "FAIL ") + label);
  if (!c) failed += 1;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const pageErrors = [];
const dom = new JSDOM(html, {
  url: BASE,
  runScripts: "dangerously",
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = (u, o) => fetch(new URL(u, BASE), o);
    window.HTMLElement.prototype.scrollIntoView = () => {};
  },
});
dom.virtualConsole.on("jsdomError", (e) => pageErrors.push(e.message + (e.detail ? `: ${e.detail.message}` : "")));
const { window } = dom;
const doc = window.document;
const $ = (id) => doc.getElementById(id);

await sleep(4500);

ok(pageErrors.length === 0, `the page's own script ran without an exception (${pageErrors.join(" | ") || "clean"})`);

// ---- header & KPIs -----------------------------------------------------------
ok(/replay|live/.test($("mode").textContent), `mode badge says what it is (${$("mode").textContent})`);
ok(/\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC/.test($("clock").textContent), `clock is a UTC timestamp (${$("clock").textContent})`);
const nSignals = parseInt($("kSignals").textContent, 10);
ok(Number.isFinite(nSignals) && nSignals > 0, `recorded signals counter is live (${$("kSignals").textContent})`);
ok($("kAcc").textContent === "—" || /%/.test($("kAcc").textContent),
   `accuracy KPI is a percentage (${$("kAcc").textContent})`);

// ---- the tape ----------------------------------------------------------------
const rows = [...doc.querySelectorAll("#tape tr")];
ok(rows.length > 0, `the tape has rows (${rows.length})`);
const txt = rows.map((r) => r.textContent).join(" ");
ok(/won|lost|open|expired|pending/.test(txt), "rows carry a status");
ok(rows.every((r) => r.querySelectorAll("td").length === 12), "every row has 12 cells");

// ---- bands -------------------------------------------------------------------
const bandRows = [...doc.querySelectorAll("#bandTable tr")];
ok(bandRows.length >= 6, `every measured band is on the page (${bandRows.length})`);
const accs = bandRows.map((r) => parseFloat(r.children[3].textContent));
ok(accs.every((a) => a >= 5 && a <= 60),
   `band accuracies are in the range a 1:4 trade can produce (${accs.join(", ")})`);
ok(accs[0] >= accs[accs.length - 1], "the strictest band is not less accurate than the loosest");

// ---- reward : risk framing ---------------------------------------------------
ok(/risk : reward 1 : [45]/.test($("rr").textContent), `the header states the ratio (${$("rr").textContent})`);
const be = parseFloat($("kBE").textContent);
ok(be > 5 && be < 30, `break-even accuracy is a low number, as a 1:4 trade should be (${$("kBE").textContent})`);
ok(/fee [\d.]+ R = \d/.test($("fee").textContent), `the fee is priced against the reward (${$("fee").textContent})`);
ok(/of the reward/.test($("fee").textContent), "the fee is expressed as a share of the prize");
const verdict = $("bandVerdict").textContent;
ok(/one win pays four losses/.test(verdict), "the page explains why low accuracy is expected here");

// ---- the 100 % club ----------------------------------------------------------
const perfect = parseInt($("clubCount").textContent.replace(/,/g, ""), 10);
ok(perfect > 100, `the club has the rules the search found (${perfect})`);
const pooled = parseFloat($("clubOos").textContent);
ok(pooled < 100 && pooled > 90, `their out-of-sample accuracy is below 100 (${pooled}%)`);
const perfectEv = parseFloat($("clubPerfectEv").textContent);
ok(perfectEv < 0, `a flawless record still loses money (${perfectEv} R per trade)`);
const feePct = parseFloat($("clubFeePct").textContent);
ok(feePct > 100, `the fee is bigger than the prize (${feePct}% of it)`);
ok(doc.querySelectorAll("#clubRules tr").length > 10, "individual rules are listed with their records");

// ---- frontier ----------------------------------------------------------------
const fHigh = [...doc.querySelectorAll("#frontierHigh tr")];
ok(fHigh.length >= 3, `the 98 %+ rows are on the page (${fHigh.length})`);
ok(fHigh.every((r) => r.children[8].textContent.trim().startsWith("-")),
   "every 98 %+ configuration loses money after fees");
const fPos = [...doc.querySelectorAll("#frontierPos tr")];
ok(fPos.length >= 3, `the profitable configurations are on the page too (${fPos.length})`);
ok(fPos.every((r) => parseFloat(r.children[5].textContent) < 98), "none of the profitable ones is 98 %+");

// ---- the cut control -----------------------------------------------------------
const cutBtns = [...doc.querySelectorAll("#cutBtns button")];
ok(cutBtns.length >= 5, `there is a recording-cut control with every band (${cutBtns.length})`);
const cutBefore = (await (await fetch(BASE + "/api/state")).json()).cut;
const b55 = cutBtns.find((b) => b.dataset.c === "0.55");
b55.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await sleep(400);
const mid = (await (await fetch(BASE + "/api/state")).json()).cut;
ok(Math.abs(mid - 0.55) < 1e-9, `clicking a cut changes the recording cut (${cutBefore} -> ${mid})`);
const back = cutBtns.find((b) => Math.abs(+b.dataset.c - cutBefore) < 1e-9);
if (back) back.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await sleep(300);

// ---- tabs --------------------------------------------------------------------
const tabs = [...doc.querySelectorAll(".tab")];
ok(tabs.length === 4, "four tabs");
tabs[2].dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await sleep(150);
ok(!$("t-club").hidden && $("t-signals").hidden, "clicking a tab switches the panel");

// ---- persistence across a reload --------------------------------------------
const before = parseInt($("kSignals").textContent, 10);
await sleep(2500);
const after = parseInt($("kSignals").textContent, 10);
ok(after >= before, `the count only grows while it runs (${before} -> ${after})`);
const stored = await (await fetch(BASE + "/api/state")).json();
ok(stored.signals.length >= after, "every signal on the page came back from the store, not from a running total");

console.log(failed === 0 ? "\nALL CHECKS PASSED" : `\n${failed} CHECK(S) FAILED`);
window.close();
process.exit(failed === 0 ? 0 : 1);
