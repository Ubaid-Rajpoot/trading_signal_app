/* TDIGM v7 page.
 *
 * Three panes, one table, one chart, and exactly one toast slot. The rules this
 * file obeys, in order of importance:
 *
 *   1. Nothing on this page may ever cover the page. Toasts come from
 *      `TdigmNotify` (see /notify.js), which shows one at a time and at most one
 *      a minute; a server message that is not actionable never becomes a toast,
 *      no matter how many of them arrive.
 *   2. There is one source of truth for the table: the archive in SQLite, read
 *      from /api/signals, overlaid with the live snapshot. A restart re-reads it,
 *      so a stored signal is still on the page after the process is gone.
 *   3. Every number is either measured here or labelled as what it is. Nothing
 *      is invented to fill a column.
 *   4. If the socket dies the page says so once, in the banner, and keeps
 *      working over HTTP - it does not toast every retry.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var NF = window.TdigmNotify;
  var quiet = localStorage.getItem("tdigm.quiet") !== "0";
  var PREFS_KEY = "tdigm.ui.preferences.v2";

  function defaultFilters() {
    return { search: "", tfs: new Set(), side: "both", grades: new Set(), sessions: new Set(), state: "all", sort: "new" };
  }

  function restoredPreferences() {
    var raw = {};
    try { raw = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch (e) { raw = {}; }
    var f = defaultFilters();
    var saved = raw.filters || {};
    f.search = typeof saved.search === "string" ? saved.search : "";
    f.side = ["both", "long", "short"].indexOf(saved.side) >= 0 ? saved.side : "both";
    f.state = typeof saved.state === "string" ? saved.state : "all";
    f.sort = typeof saved.sort === "string" ? saved.sort : "new";
    f.tfs = new Set(Array.isArray(saved.tfs) ? saved.tfs : []);
    f.grades = new Set(Array.isArray(saved.grades) ? saved.grades : []);
    f.sessions = new Set(Array.isArray(saved.sessions) ? saved.sessions : []);
    return { filters: f, selected: typeof raw.selected === "string" ? raw.selected : null };
  }

  var restored = restoredPreferences();

  // ------------------------------------------------------------------ model
  var model = {
    rows: new Map(),        // key -> row (archive + live overlay)
    live: new Set(),        // keys the engine currently holds
    order: [],              // keys, newest first (recomputed on render)
    selected: null,         // key
    preferredSelected: restored.selected,
    filters: restored.filters,
    state: null,            // last snapshot
    health: null,
    archive: {},
    results: {},
    counts: {},
    chartKey: "",
    chartBusy: false,
    wsOk: false,
    lastMessage: 0,
    toastTimer: 0,
    chartTimer: 0,
    medianRr: null,
  };

  function savePreferences() {
    var f = model.filters;
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify({
        selected: model.selected || model.preferredSelected || null,
        filters: {
          search: f.search, side: f.side, state: f.state, sort: f.sort,
          tfs: Array.from(f.tfs), grades: Array.from(f.grades), sessions: Array.from(f.sessions),
        },
      }));
    } catch (e) { /* private storage/full quota must never break the scanner */ }
  }

  var queue = NF.createQueue({ minGapMs: quiet ? 60_000 : 8_000 });
  var toastQueue = queue;

  // ------------------------------------------------------------------ format
  function num(v, digits) {
    if (v === null || v === undefined || v === "") return "—";
    var n = Number(v);
    if (!isFinite(n)) return "—";
    var abs = Math.abs(n);
    if (digits === undefined) {
      if (abs >= 1000) digits = 2;
      else if (abs >= 1) digits = 3;
      else if (abs >= 0.01) digits = 5;
      else digits = 7;
    }
    return n.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: digits });
  }

  function clock(ms) {
    if (!ms) return "—";
    var d = new Date(Number(ms));
    if (isNaN(d.getTime())) return "—";
    var pad = function (x) { return String(x).padStart(2, "0"); };
    return pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }

  function ago(ms) {
    var s = Math.max(0, Math.round((Date.now() - Number(ms || 0)) / 1000));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    if (s < 86400) return Math.round(s / 3600) + "h";
    return Math.round(s / 86400) + "d";
  }

  function sideOf(row) { return String(row.side || "").toUpperCase() === "SHORT" ? "short" : "long"; }

  function stateOf(row) {
    if (row.state) return String(row.state).toLowerCase();
    return row.valid === false ? "expired" : "open";
  }

  function normaliseRow(raw) {
    return {
      key: raw.key || (raw.symbol + "|" + raw.timeframe + "|" + (raw.created_ms || raw.time)),
      symbol: raw.symbol || "",
      timeframe: raw.timeframe || "",
      side: String(raw.side || "").toUpperCase(),
      grade: raw.grade || "",
      state: stateOf(raw),
      valid: raw.valid !== false,
      entry: raw.entry,
      stop: raw.stop,
      target: raw.target,
      rr: raw.rr,
      r_now: raw.r_now,
      score: raw.score,
      session: raw.session || "",
      created_ms: Number(raw.created_ms || raw.time || 0),
      touched_ms: Number(raw.touched_ms || 0),
      closed_ms: Number(raw.closed_ms || 0),
      zone: raw.zone || null,
      zones: raw.zones || (raw.zone ? [raw.zone] : []),
      mitigation_kind: raw.mitigation_kind || "",
      mitigation_zone: raw.mitigation_zone || null,
      state_reason: raw.state_reason || "",
      target_rr: raw.target_rr,
      lifecycle_version: Number(raw.lifecycle_version || 1),
      mitigated: !!raw.mitigated,
      swept_level: raw.swept_level,
      swept_side: raw.swept_side,
      sweep_ms: Number(raw.sweep_ms || (raw.setup || {}).sweep_time || 0),
      bos_level: raw.bos_level,
      bos_ms: Number(raw.bos_ms || raw.created_ms || raw.time || 0),
      break_kind: raw.break_kind,
      hvb_side: raw.hvb_side,
      hvb_ms: Number(raw.hvb_ms || raw.created_ms || raw.time || 0),
      chain_valid: raw.chain_valid !== false,
      chain_errors: raw.chain_errors || [],
      volume_ratio: raw.volume_ratio,
      reasons: raw.reasons || raw.confluences || [],
      evidence: raw.evidence || [],
      trap: raw.trap || null,
      shift: raw.shift || null,
      ac: raw.ac || raw.imbalance || null,
      fee_r: raw.fee_r,
      htf_bias: raw.htf_bias,
      premium: raw.premium,
      stored: !!raw.stored,
      grade_reasons: raw.grade_reasons || [],
      missing: raw.missing || [],
      raw: raw,
    };
  }

  function absorbRows(list, live) {
    if (!Array.isArray(list)) return 0;
    var added = 0;
    for (var i = 0; i < list.length; i++) {
      var row = normaliseRow(list[i] || {});
      if (!row.key || !row.symbol) continue;
      if (live) {
        model.live.add(row.key);
        model.rows.set(row.key, Object.assign({}, model.rows.get(row.key) || {}, row));
      } else if (!model.rows.has(row.key)) {
        model.rows.set(row.key, row);
        added++;
      } else if (!model.live.has(row.key)) {
        // the archive is the record; the live copy wins while the engine holds it
        model.rows.set(row.key, Object.assign({}, model.rows.get(row.key), row));
      }
    }
    return added;
  }

  // ------------------------------------------------------------------ filters
  function matches(row) {
    var f = model.filters;
    if (f.side !== "both" && sideOf(row) !== f.side) return false;
    if (f.tfs.size && !f.tfs.has(row.timeframe)) return false;
    if (f.grades.size && !f.grades.has(row.grade || "?")) return false;
    if (f.sessions.size && !f.sessions.has(row.session || "?")) return false;
    if (f.state === "open" && !row.valid) return false;
    if (f.state !== "all" && f.state !== "open" && row.state !== f.state) return false;
    if (f.search) {
      var hay = (row.symbol + " " + row.timeframe + " " + row.side).toUpperCase();
      if (hay.indexOf(f.search.toUpperCase()) < 0) return false;
    }
    return true;
  }

  function sorted() {
    var out = [];
    model.rows.forEach(function (row) { if (matches(row)) out.push(row); });
    var by = model.filters.sort;
    out.sort(function (a, b) {
      if (by === "rr") return (Number(b.rr) || 0) - (Number(a.rr) || 0);
      if (by === "symbol") return a.symbol.localeCompare(b.symbol) || b.created_ms - a.created_ms;
      if (by === "moving") return Math.abs(Number(b.r_now) || 0) - Math.abs(Number(a.r_now) || 0);
      return b.created_ms - a.created_ms;
    });
    return out;
  }

  // ------------------------------------------------------------------- table
  function cell(html, cls) {
    var td = document.createElement("td");
    if (cls) td.className = cls;
    td.innerHTML = html;
    return td;
  }

  function renderTable() {
    var rows = sorted();
    var body = $("rows");
    var frag = document.createDocumentFragment();
    var limit = Math.min(rows.length, 400);
    for (var i = 0; i < limit; i++) {
      var r = rows[i];
      var tr = document.createElement("tr");
      tr.dataset.key = r.key;
      if (r.key === model.selected) tr.className = "sel";
      tr.appendChild(cell(clock(r.created_ms), "c-time"));
      tr.appendChild(cell(escapeHtml(r.symbol), "c-sym"));
      tr.appendChild(cell(escapeHtml(r.timeframe), "c-tf"));
      tr.appendChild(cell(sideOf(r), "c-side side " + sideOf(r)));
      tr.appendChild(cell(escapeHtml(r.grade || "—"), "c-grade grade " + escapeHtml(r.grade || "")));
      tr.appendChild(cell(num(r.entry), "c-num"));
      tr.appendChild(cell(num(r.stop), "c-num"));
      tr.appendChild(cell(num(r.target), "c-num"));
      tr.appendChild(cell(r.rr ? "1:" + Number(r.rr).toFixed(1) : "—", "c-num"));
      var now = r.r_now;
      tr.appendChild(cell(now === null || now === undefined ? "—" : (Number(now) > 0 ? "+" : "") + Number(now).toFixed(2) + "R",
        "c-num now " + (Number(now) > 0 ? "pos" : Number(now) < 0 ? "neg" : "")));
      tr.appendChild(cell(stateOf(r), "c-state state " + stateOf(r)));
      frag.appendChild(tr);
    }
    body.replaceChildren(frag);
    $("list-count").textContent = rows.length;
    $("empty").hidden = rows.length > 0;
    if (!rows.length) {
      $("empty-why").textContent = model.rows.size
        ? "No stored signal matches these filters. Clear them to see all " + model.rows.size + "."
        : (model.state ? "The engine has not confirmed a setup yet. It is still building its liquidity map." : "Waiting for the engine…");
    }
    var meta = [];
    if (model.counts && model.counts.total) meta.push(model.counts.total + " in the archive");
    if (model.live.size) meta.push(model.live.size + " live");
    $("list-meta").textContent = meta.join(" · ") || "waiting for the engine…";
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ------------------------------------------------------------------ facets
  function chipRow(host, values, set, label) {
    host.replaceChildren();
    values.forEach(function (value) {
      var b = document.createElement("button");
      b.className = "chip" + (set.has(value) ? " on" : "");
      b.textContent = label ? label(value) : value;
      b.onclick = function () {
        if (set.has(value)) set.delete(value); else set.add(value);
        chipRow(host, values, set, label);
        savePreferences();
        renderTable();
      };
      host.appendChild(b);
    });
  }

  function buildFacets() {
    var tfs = (model.state && model.state.timeframes) || [];
    if (!tfs.length) {
      var seen = new Set();
      model.rows.forEach(function (r) { if (r.timeframe) seen.add(r.timeframe); });
      tfs = Array.from(seen);
    }
    tfs.sort(function (a, b) { return rankTf(a) - rankTf(b) || a.localeCompare(b); });
    chipRow($("chips-tf"), tfs, model.filters.tfs);

    var grades = ["A", "B"];
    chipRow($("chips-grade"), grades, model.filters.grades);

    var sessions = [];
    var ss = new Set();
    model.rows.forEach(function (r) { if (r.session) ss.add(r.session); });
    sessions = Array.from(ss).sort();
    $("chips-session").parentElement.hidden = sessions.length === 0;
    chipRow($("chips-session"), sessions, model.filters.sessions);
  }

  function rankTf(tf) {
    var table = { "1m": 1, "3m": 2, "5m": 3, "15m": 4, "30m": 5, "1h": 6, "2h": 7, "4h": 8, "6h": 9, "12h": 10, "1d": 11, "1w": 12 };
    return table[tf] || 50;
  }

  // ------------------------------------------------------------------ header
  function renderHeader() {
    var s = model.state;
    var pill = $("source-pill");
    var label = "connecting";
    pill.className = "pill";
    if (s) {
      var source = s.feed && s.feed.source ? s.feed.source : s.source;
      var status = s.feed && s.feed.status ? s.feed.status : (s.feed && s.feed.connected ? "live" : "");
      label = [source || "engine", status].filter(Boolean).join(" · ");
      pill.className = "pill " + (source === "replay" ? "replay" : model.wsOk ? "live" : "down");
    } else if (!model.wsOk) {
      pill.className = "pill down";
    }
    $("source-text").textContent = label;
    if (s) {
      $("strategy-tag").textContent = s.strategy === "book" ? "book engine" : "TDIGM setup";
      $("stat-symbols").textContent = (s.symbols ? s.symbols.length : 0) + " symbols · " + (s.warming || 0) + " warming" + (s.failed ? " · " + s.failed + " failed" : "");
      var tf = s.timeframes && s.timeframes.length ? s.timeframes.join(" ") : "";
      $("stat-candle").textContent = (s.candles_seen !== null && s.candles_seen !== undefined ? num(s.candles_seen, 0) + " candles" : "") + (tf ? " · " + tf : "");
    }
    if (model.health) {
      var n = model.health.notifications || {};
      var noise = (n.filtered || 0) + (n.suppressed || 0);
      $("status-noise").textContent = noise + " notice(s) hidden";
      $("status-noise").title = (n.filtered || 0) + " rejected setup notices and " + (n.suppressed || 0)
        + " duplicate/chatter notices hidden. No market frames or confirmed signals are dropped here.";
      $("status-counts").textContent = (model.health.signals || 0) + " signals · " + (model.health.stored || 0)
        + " stored · " + (model.health.setups_valid || 0) + " valid now · "
        + (model.health.setups_unmitigated || 0) + " unmitigated";
      renderFunnel();
      $("status-feed").textContent = "feed: " + (model.health.feed || "—") + " · " + num(model.health.frames, 0) + " frames";
    }
    $("status-memory").textContent = s && s.uptime ? "up " + ago(Date.now() - s.uptime * 1000) : "";
  }

  function renderStats() {
    var res = model.results || {};
    var all = res.all || {};
    var counts = model.counts || {};
    var byState = counts.by_state || {};

    var verdict = all.verdict || "not enough trades";
    var word = verdict === "not proven" ? "not proven"
      : verdict === "not enough trades" ? "too few trades" : verdict;
    var cls = verdict === "positive" ? "positive" : verdict === "negative" ? "negative" : "unproven";
    var badge = $("record-verdict");
    badge.className = "verdict " + cls;
    badge.textContent = word;
    badge.title = "95% interval on R per trade: "
      + (all.ci_low === undefined ? "—" : all.ci_low + " to " + all.ci_high)
      + ". '" + word + "' means exactly that - nothing about it is rounded in the scanner's favour.";

    function row(label, value, title) {
      return '<div' + (title ? ' title="' + escapeHtml(title) + '"' : '') + '><span class="muted">'
        + escapeHtml(label) + "</span><b>" + escapeHtml(value) + "</b></div>";
    }
    var rows = [];
    rows.push(row("signals stored", num(counts.total, 0), "every plan ever recorded, across restarts"));
    rows.push(row("mitigated", num(all.mitigated, 0), "order-block or eligible aggressive-breaker touches under the current lifecycle"));
    rows.push(row("decided", num(all.decided, 0), "mitigated trades closed at fixed 4R or at the stop"));
    rows.push(row("4R win rate", all.win_rate == null ? "—" : (all.win_rate * 100).toFixed(1) + "%",
      "4R wins / (4R wins + stopped trades), after mitigation only. Break-even before fees is 20%."));
    var rpt = all.r_per_trade;
    rows.push(row("R / trade", rpt == null ? "—" : (rpt > 0 ? "+" : "") + Number(rpt).toFixed(3),
      "measured only after mitigation: +4R for a target, -1R for a stop"));
    rows.push(row("95% interval", all.ci_low == null ? "—"
      : (all.ci_low > 0 ? "+" : "") + Number(all.ci_low).toFixed(2) + " … "
        + (all.ci_high > 0 ? "+" : "") + Number(all.ci_high).toFixed(2),
      "if this includes zero, the average is indistinguishable from luck"));
    rows.push(row("open now", num(byState.active || 0, 0), "live and not yet resolved"));
    var byTf = (res.by_timeframe) || {};
    var names = Object.keys(byTf).sort();
    if (names.length) {
      rows.push('<div class="muted" style="font-size:10px;letter-spacing:.06em;margin-top:4px">BY TIMEFRAME</div>');
      names.forEach(function (tf) {
        var g = byTf[tf];
        if (!g || !g.scored) return;
        var tone = g.verdict === "positive" ? "pos" : g.verdict === "negative" ? "neg" : "";
        rows.push('<div title="' + escapeHtml(g.verdict) + ' · 95% interval '
          + g.ci_low + " … " + g.ci_high + '"><span class="muted">' + escapeHtml(tf) + " · "
          + g.scored + "</span><b class='" + tone + "'>"
          + (g.r_per_trade > 0 ? "+" : "") + Number(g.r_per_trade).toFixed(2) + "R</b></div>");
      });
    }
    var stats = $("record-body");
    if (stats) stats.innerHTML = rows.join("");
    var note = $("record-note");
    if (note) {
      var n = all.scored || 0;
      note.innerHTML = n
        ? "<b>" + n + "</b> scored trade(s). A track record this size carries a standard error of about "
          + (all.se == null ? "?" : Number(all.se).toFixed(2)) + "R per trade, so the interval above is the honest"
          + " reading — not the average."
        : "No trade has closed yet. The record appears as plans resolve; nothing is projected.";
    }
  }

  // ------------------------------------------------------------------- story
  function renderStory(row) {
    var host = $("story");
    if (!row) { host.innerHTML = ""; return; }
    var bits = [];
    var chain = [];
    if (row.swept_level !== null && row.swept_level !== undefined) {
      chain.push((row.swept_side === "low" ? "low sweep" : "high sweep") + " @ " + num(row.swept_level)
        + (row.sweep_ms ? " [" + clock(row.sweep_ms) + "]" : ""));
    }
    if (row.bos_level !== null && row.bos_level !== undefined) {
      chain.push("same-leg opposite body break @ " + num(row.bos_level) + " [" + clock(row.created_ms) + "]");
    }
    if (row.hvb_side) chain.push("same-candle HVB " + row.hvb_side);
    if (row.trap) chain.push("trap " + (row.trap.side || "") + " @ " + num(row.trap.level));
    if (row.ac) chain.push("imbalance " + num(row.ac.bottom) + "–" + num(row.ac.top));
    if (row.shift) chain.push("shift " + (row.shift.kind || "MS") + " @ " + num(row.shift.level));
    if (row.target) chain.push("draw @ " + num(row.target));
    if (chain.length) bits.push('<div class="chain">' + escapeHtml(chain.join("  →  ")) + "</div>");
    if (row.chain_valid === false) {
      bits.push('<div class="chain-invalid"><b>QUARANTINED</b> · ' + escapeHtml((row.chain_errors || []).join(" · ")) + "</div>");
    }
    if (row.lifecycle_version < 2) {
      bits.push('<div class="chain-invalid"><b>LEGACY LIFECYCLE</b> · excluded from the mitigation-first 4R win rate until deep replay regrades it</div>');
    }
    var stateLabel = stateOf(row).toUpperCase();
    var stateWhy = row.state_reason || (row.state === "pending"
      ? "waiting for order-block or eligible aggressive-breaker mitigation; no trade or 4R target exists yet"
      : "no lifecycle reason was stored for this legacy row");
    bits.push('<div class="nums"><span>state <b>' + escapeHtml(stateLabel) + "</b> · "
      + escapeHtml(stateWhy) + "</span></div>");
    var nums = [];
    ["entry", "stop", "target"].forEach(function (k) {
      if (row[k] !== null && row[k] !== undefined) nums.push(k + " <b>" + escapeHtml(num(row[k])) + "</b>");
    });
    if (row.rr) nums.push("armed <b>1:" + Number(row.rr).toFixed(1) + " after mitigation</b>");
    else nums.push("target <b>arms at 1:" + Number(row.target_rr || 4).toFixed(0) + " after mitigation</b>");
    if (row.mitigation_kind) nums.push("filled from <b>" + escapeHtml(row.mitigation_kind.replace(/_/g, " ")) + "</b>");
    if (row.r_now !== null && row.r_now !== undefined) nums.push("now <b>" + (Number(row.r_now) > 0 ? "+" : "") + Number(row.r_now).toFixed(2) + "R</b>");
    if (row.fee_r !== null && row.fee_r !== undefined) nums.push("fee <b>" + Number(row.fee_r).toFixed(2) + "R</b>");
    if (row.score !== null && row.score !== undefined) nums.push("score <b>" + escapeHtml(row.score) + "</b>");
    if (nums.length) bits.push('<div class="nums">' + nums.join("") + "</div>");
    if (row.grade) {
      var held = (row.grade_reasons || []).length;
      var miss = (row.missing || []);
      var story = '<div class="nums" title="A description of the story, not a quality rank: '
        + 'measured on the archive, plans with the fullest stories have not done better than incomplete ones.">'
        + "<span>story <b>" + escapeHtml(row.grade) + "</b> · " + held + " of " + (held + miss.length)
        + " structural checks";
      if (miss.length) story += ' · missing: <b>' + escapeHtml(miss.join(", ")) + "</b>";
      story += "</span></div>";
      bits.push(story);
    }
    var bucket = bucketRecord(row);
    if (bucket) {
      bits.push('<div class="nums" title="Every mitigated trade in this instrument-timeframe bucket, '
        + 'at fixed 1:4. Unfilled invalidations are not losses. The interval is what the sample supports.">'
        + "<span>" + escapeHtml(row.timeframe) + " " + escapeHtml(sideOf(row)) + " so far: <b>"
        + bucket.decided + "</b> decided · <b>" + (bucket.r_per_trade > 0 ? "+" : "")
        + Number(bucket.r_per_trade || 0).toFixed(2) + "R</b>"
        + " each · 95% " + (bucket.ci_low > 0 ? "+" : "") + Number(bucket.ci_low).toFixed(2)
        + "…" + (bucket.ci_high > 0 ? "+" : "") + Number(bucket.ci_high).toFixed(2)
        + " <b>" + escapeHtml(bucket.verdict) + "</b></span></div>");
    }
    var why = (row.reasons && row.reasons.length ? row.reasons : row.evidence) || [];
    if (why.length) bits.push('<div class="why">' + why.slice(0, 8).map(function (w) { return "<div>" + escapeHtml(w) + "</div>"; }).join("") + "</div>");
    if (!bits.length) bits.push('<div class="muted">This setup carries no narrative: it came from the archive before the current engine, so there is nothing to explain. Its numbers above are the record.</div>');
    host.innerHTML = bits.join("");
  }

  /** The archive's own record for this signal's bucket (timeframe x side). */
  function bucketRecord(row) {
    var by = (model.results && model.results.by_timeframe) || {};
    var group = by[row.timeframe];
    if (!group) return null;
    var all = (model.results && model.results.by_side) || {};
    var sideBucket = all[row.side];
    if (!sideBucket) return group.verdict ? group : null;
    // the tightest honest thing we can say: how this timeframe has done overall
    return group.verdict ? group : sideBucket;
  }

  // ----------------------------------------------------- what it is watching
  function renderWatch(data) {
    var rows = (data && data.watching) || [];
    var host = $("watch-list");
    var box = $("watch");
    if (!rows.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    var shown = rows.slice(0, 12);
    host.innerHTML = shown.map(function (w) {
      var st, cls, what, need;
      if (w.mode === "sweep") {
        var s = w.structure || {};
        st = "swept";
        cls = "st-trap";
        what = (s.swept_side === "high" ? "took high " : "took low ") + num(s.swept_level);
        need = "body close " + (s.watching_side === "low" ? "below " : "above ") + num(s.watching)
          + " with matching HVB";
      } else {
        st = w.armed ? "armed" : "trap";
        cls = w.armed ? "st-armed" : "st-trap";
        what = w.armed
          ? "gap " + num(w.armed.bottom) + "–" + num(w.armed.top)
            + " · " + Math.round((w.armed.fill || 0) * 100) + "% filled"
          : (w.trap.side === "high" ? "took high " : "took low ") + num(w.trap.level) + " (" + w.trap.name + ")";
        need = w.armed ? "needs the reverse shift" : "needs a displacement";
      }
      return '<div class="watch-row"><span class="sym">' + escapeHtml(w.symbol) + "</span>"
        + "<span>" + escapeHtml(w.timeframe) + "</span>"
        + '<span class="' + cls + '">' + st + "</span>"
        + '<span class="need">' + escapeHtml(what) + " — " + escapeHtml(need) + "</span></div>";
    }).join("");
    $("watch-count").textContent = rows.length + " tracked";
  }

  function refreshWatch() {
    fetch("/api/watch?limit=60")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        renderWatch(data);
        model.funnel = data.funnel || null;
        if (model.funnel) renderFunnel();
      })
      .catch(function () {});
  }

  function renderFunnel() {
    var f = model.funnel;
    if (!f) return;
    var drops = f.drops || {};
    var worst = Object.keys(drops).sort(function (a, b) { return drops[b] - drops[a]; })[0];
    if (f.mode === "sweep") {
      $("status-funnel").textContent = (f.sweeps || 0) + " sweeps → " + (f.setups || 0)
        + " body breaks → " + (f.plans || 0) + " TDIGM signals"
        + (worst ? " · top rejection: " + worst + " (" + drops[worst] + ")" : "");
    } else {
      $("status-funnel").textContent = (f.traps || 0) + " traps → " + (f.displacements || 0)
        + " displacements → " + (f.plans || 0) + " plans"
        + (worst ? " · mostly refused: " + worst + " (" + drops[worst] + ")" : "");
    }
  }

  // ------------------------------------------------------------------- chart
  // The interaction model intentionally mirrors the chart in SMC-Radar:
  // wheel zoom, drag pan, right-axis price scaling, crosshair/OHLC and empty
  // right margin.  The selected setup—not today's candle—is the focus.
  var chartView = {
    data: null, selectedKey: "", span: 120, right: 0, zoomY: 1, panY: 0,
    hover: null, drag: null, focus: 0,
  };

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function timeframeMs(tf) {
    return { "1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000,
      "30m": 1800000, "1h": 3600000, "2h": 7200000, "4h": 14400000,
      "1d": 86400000, "1w": 604800000 }[tf] || 60000;
  }

  function barAtTime(candles, ms) {
    if (!ms || !candles.length) return -1;
    var lo = 0, hi = candles.length - 1, found = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (Number(candles[mid].ct || candles[mid].t) >= Number(ms)) { found = mid; hi = mid - 1; }
      else lo = mid + 1;
    }
    return found;
  }

  function selectRow(row) {
    model.selected = row ? row.key : null;
    model.preferredSelected = model.selected;
    savePreferences();
    Array.prototype.forEach.call(document.querySelectorAll("tr.sel"), function (tr) { tr.classList.remove("sel"); });
    var tr = row ? document.querySelector('tr[data-key="' + CSS.escape(row.key) + '"]') : null;
    if (tr) tr.classList.add("sel");
    if (!row) return;
    $("chart-title").textContent = row.symbol + " " + row.timeframe + " · " + sideOf(row);
    $("chart-sub").textContent = clock(row.created_ms) + " UTC · " + stateOf(row) + (row.grade ? " · grade " + row.grade : "");
    renderStory(row);
    loadChart(row, false);
  }

  function loadChart(row, force) {
    if (!row) return;
    var key = row.key;
    model.chartKey = key;
    model.chartBusy = true;
    var url = "/api/chart?symbol=" + encodeURIComponent(row.symbol)
      + "&timeframe=" + encodeURIComponent(row.timeframe)
      + "&bars=600&key=" + encodeURIComponent(row.key)
      + "&at=" + encodeURIComponent(row.created_ms)
      + (force ? "&refresh=1" : "");
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        model.chartBusy = false;
        if (model.chartKey !== key) return;
        var changed = chartView.selectedKey !== key;
        chartView.data = data;
        chartView.selectedKey = key;
        if (changed || force) resetChartView();
        else drawChart();
      })
      .catch(function () {
        model.chartBusy = false;
        $("chart-hint").hidden = false;
        $("chart-hint").textContent = "Could not load candles for this setup.";
      });
  }

  function resetChartView() {
    var data = chartView.data || {};
    var candles = data.candles || [];
    chartView.zoomY = 1;
    chartView.panY = 0;
    chartView.hover = null;
    chartView.span = Math.min(Math.max(60, Math.min(140, candles.length)), Math.max(20, candles.length));
    chartView.focus = clamp(Number(data.focus_index) || 0, 0, Math.max(0, candles.length - 1));
    // Put the break/HVB candle about two thirds across the frame, leaving both
    // the preceding sweep and post-signal lifecycle in sight.
    chartView.right = clamp(
      chartView.focus + Math.round(chartView.span * 0.32),
      Math.min(chartView.span - 1, Math.max(0, candles.length - 1)),
      Math.max(0, candles.length - 1) + Math.round(chartView.span * 0.18)
    );
    drawChart();
  }

  function chartCanvasSize() {
    var canvas = $("chart");
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    var w = Math.max(320, Math.round(canvas.clientWidth || canvas.parentElement.clientWidth || 600));
    var h = Math.max(240, Math.round(canvas.clientHeight || canvas.parentElement.clientHeight || 420));
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    }
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { canvas: canvas, ctx: ctx, w: w, h: h, l: 8, r: 76, t: 16, b: 28 };
  }

  function priceText(v) { return num(v); }

  function drawChart() {
    var box = chartCanvasSize();
    var ctx = box.ctx, w = box.w, h = box.h, padL = box.l, padR = box.r, padT = box.t, padB = box.b;
    ctx.clearRect(0, 0, w, h);
    var data = chartView.data || {};
    var candles = data.candles || [];
    var hint = $("chart-hint");
    if (!candles.length) {
      hint.hidden = false;
      hint.textContent = data.history_error
        ? "Historical window unavailable: " + data.history_error
        : "No candles yet for " + (data.symbol || "") + " " + (data.timeframe || "") + ".";
      return;
    }
    hint.hidden = true;

    var len = candles.length;
    chartView.span = clamp(chartView.span, 20, Math.max(20, len));
    var spanBars = Math.min(chartView.span, len);
    var future = Math.round(spanBars * 0.18);
    chartView.right = clamp(chartView.right, spanBars - 1, len - 1 + future);
    var left = chartView.right - spanBars + 1;
    var i0 = Math.max(0, Math.floor(left));
    var i1 = Math.min(len - 1, Math.ceil(chartView.right));
    var plotW = w - padL - padR;
    var plotH = h - padT - padB;
    var candleW = plotW / spanBars;
    function x(i) { return padL + (i - left + 0.5) * candleW; }
    var bodyW = Math.max(1, Math.min(13, candleW * 0.68));

    var selected = data.selected || null;
    var rawSetup = selected && selected.setup || {};
    var sweepTime = selected ? Number(selected.sweep_ms || rawSetup.sweep_time || 0) : 0;
    var breakTime = selected ? Number(selected.created_ms || rawSetup.break_time || 0) : 0;
    var sweepIndex = barAtTime(candles, sweepTime);
    var breakIndex = barAtTime(candles, breakTime);

    var lo = Infinity, hi = -Infinity;
    for (var i = i0; i <= i1; i++) { lo = Math.min(lo, Number(candles[i].l)); hi = Math.max(hi, Number(candles[i].h)); }
    if (!isFinite(lo) || !isFinite(hi)) return;
    if (selected && breakIndex >= i0 && breakIndex <= i1) {
      [selected.entry, selected.stop, selected.target, selected.swept_level, selected.bos_level].forEach(function (v) {
        v = Number(v); if (isFinite(v) && v > 0) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      });
      (selected.zones || []).forEach(function (zone) {
        var zl = Number(zone.low), zh = Number(zone.high);
        if (isFinite(zl) && isFinite(zh)) { lo = Math.min(lo, zl); hi = Math.max(hi, zh); }
      });
    }
    var rawSpan = hi - lo || Math.max(1, Math.abs(hi) * 0.01);
    var centre = (hi + lo) / 2 + chartView.panY * rawSpan;
    var half = (rawSpan * 1.08 / 2) / chartView.zoomY;
    var pLo = centre - half, pHi = centre + half;
    function y(v) { return padT + (pHi - Number(v)) / (pHi - pLo) * plotH; }

    // TradingView/SMC-Radar grid: time and price axes are part of the chart, not
    // guessed from the signal list.
    ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    for (var g = 0; g <= 4; g++) {
      var price = pLo + (pHi - pLo) * g / 4;
      var gy = y(price);
      ctx.strokeStyle = "#1b2434"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(w - padR, gy); ctx.stroke();
      ctx.fillStyle = "#6d7b93"; ctx.fillText(priceText(price), w - padR + 7, gy);
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    var ticks = Math.max(3, Math.min(7, Math.round(plotW / 105)));
    var step = timeframeMs(data.timeframe);
    for (var t = 0; t <= ticks; t++) {
      var ti = Math.round(left + (spanBars - 1) * t / ticks);
      if (ti < 0) continue;
      var base = ti < len ? candles[ti].t : candles[len - 1].t + (ti - len + 1) * step;
      var tx = x(ti);
      ctx.strokeStyle = "#151d2b"; ctx.beginPath(); ctx.moveTo(tx, padT); ctx.lineTo(tx, h - padB); ctx.stroke();
      var d = new Date(base);
      var label = String(d.getUTCDate()).padStart(2, "0") + " "
        + d.toLocaleString("en-US", { month: "short", timeZone: "UTC" }) + " "
        + String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0");
      ctx.fillStyle = "#6d7b93"; ctx.fillText(label, tx, h - padB + 7);
    }

    // The exact setup window is unmistakable: amber sweep candle/level, a
    // shaded time range, then a blue body-break + HVB candle.
    if (selected && sweepIndex >= 0 && breakIndex >= 0) {
      var sx = x(sweepIndex), bx = x(breakIndex);
      ctx.fillStyle = "rgba(255,179,71,.055)";
      ctx.fillRect(Math.min(sx, bx) - bodyW, padT, Math.max(bodyW * 2, Math.abs(bx - sx) + bodyW * 2), plotH);
      ctx.strokeStyle = "rgba(255,179,71,.34)"; ctx.setLineDash([3, 4]);
      ctx.strokeRect(Math.min(sx, bx) - bodyW, padT, Math.max(bodyW * 2, Math.abs(bx - sx) + bodyW * 2), plotH);
      ctx.setLineDash([]);
      ctx.fillStyle = "#ffb347"; ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.fillText("SETUP WINDOW", Math.min(sx, bx) + 4, padT + 4);

      var swept = Number(selected.swept_level || rawSetup.swept_level);
      if (isFinite(swept)) {
        ctx.strokeStyle = "rgba(255,179,71,.9)"; ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(Math.max(padL, sx - candleW * 10), y(swept)); ctx.lineTo(bx, y(swept)); ctx.stroke();
        ctx.setLineDash([]); ctx.fillStyle = "#ffb347"; ctx.textBaseline = "bottom";
        ctx.fillText((selected.swept_side === "low" ? "swept low " : "swept high ") + priceText(swept), Math.max(padL + 2, sx - candleW * 9), y(swept) - 2);
      }

      var entry = Number(selected.entry), stop = Number(selected.stop), target = Number(selected.target);
      var mitigationIndex = selected.touched_ms ? barAtTime(candles, selected.touched_ms) : -1;
      var tradeStart = mitigationIndex >= 0 ? x(mitigationIndex) : bx;
      var tradeEnd = Math.min(w - padR, tradeStart + Math.max(candleW * 12, plotW * 0.24));
      if (selected.mitigated && isFinite(entry) && isFinite(stop)) {
        ctx.fillStyle = "rgba(255,92,122,.075)";
        ctx.fillRect(tradeStart, Math.min(y(entry), y(stop)), Math.max(2, tradeEnd - tradeStart), Math.abs(y(stop) - y(entry)));
      }
      if (selected.mitigated && isFinite(entry) && isFinite(target)) {
        ctx.fillStyle = "rgba(46,230,168,.075)";
        ctx.fillRect(tradeStart, Math.min(y(entry), y(target)), Math.max(2, tradeEnd - tradeStart), Math.abs(y(target) - y(entry)));
      }

      // Both legitimate mitigation choices are explicit. The breaker is the
      // paired endpoint candle on the left of an aggressive displacement.
      (selected.zones || []).forEach(function (zone, zi) {
        var zl = Number(zone.low), zh = Number(zone.high);
        if (!isFinite(zl) || !isFinite(zh) || zh <= zl) return;
        var sourceIndex = barAtTime(candles, Number(zone.time || breakTime));
        var zx = sourceIndex >= 0 ? x(sourceIndex) : bx;
        var endIndex = selected.touched_ms ? barAtTime(candles, selected.touched_ms) : -1;
        var zEnd = endIndex >= 0 ? x(endIndex) + bodyW : tradeEnd;
        var breaker = zone.type === "breaker_block";
        ctx.fillStyle = breaker ? "rgba(180,135,255,.13)" : "rgba(103,169,255,.10)";
        ctx.strokeStyle = breaker ? "rgba(180,135,255,.78)" : "rgba(103,169,255,.68)";
        ctx.fillRect(zx, y(zh), Math.max(bodyW, zEnd - zx), Math.max(2, y(zl) - y(zh)));
        ctx.strokeRect(zx, y(zh), Math.max(bodyW, zEnd - zx), Math.max(2, y(zl) - y(zh)));
        ctx.fillStyle = breaker ? "#b487ff" : "#67a9ff";
        ctx.textAlign = "left"; ctx.textBaseline = "bottom";
        ctx.fillText(breaker ? "BREAKER" : "ORDER BLOCK", zx + 3, y(zh) - 2 - zi * 10);
      });
    }

    // Candles use the same colours and spacing as SMC-Radar.
    for (var ci = i0; ci <= i1; ci++) {
      var c = candles[ci];
      var bull = Number(c.c) >= Number(c.o);
      var colour = bull ? "#2ee6a8" : "#ff5c7a";
      ctx.strokeStyle = colour; ctx.globalAlpha = c.closed === false ? 1 : 0.84; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x(ci), y(c.h)); ctx.lineTo(x(ci), y(c.l)); ctx.stroke();
      var top = Math.min(y(c.o), y(c.c));
      var bottom = Math.max(y(c.o), y(c.c));
      ctx.fillStyle = colour; ctx.fillRect(x(ci) - bodyW / 2, top, bodyW, Math.max(1, bottom - top));
    }
    ctx.globalAlpha = 1;

    function outlineCandle(index, colour, label, above) {
      if (index < i0 || index > i1 || !candles[index]) return;
      var c = candles[index];
      var top = y(c.h), bottom = y(c.l);
      ctx.strokeStyle = colour; ctx.lineWidth = 2;
      ctx.strokeRect(x(index) - bodyW * 0.9, top - 3, bodyW * 1.8, Math.max(8, bottom - top + 6));
      ctx.fillStyle = colour; ctx.textAlign = "center";
      ctx.textBaseline = above ? "bottom" : "top";
      ctx.font = "bold 10px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.fillText(label, x(index), above ? top - 7 : bottom + 7);
    }
    if (selected) {
      outlineCandle(sweepIndex, "#ffb347", selected.swept_side === "low" ? "LOW SWEEP" : "HIGH SWEEP", true);
      outlineCandle(breakIndex, "#67a9ff", "BODY BREAK + HVB", false);
    }

    // Entry and 4R begin at mitigation, not at the body-break close. Pending
    // rows show only a planned fill and invalidation stop.
    if (selected && breakIndex >= 0) {
      var touchedIndex = selected.touched_ms ? barAtTime(candles, selected.touched_ms) : -1;
      var startX = Math.max(padL, x(touchedIndex >= 0 ? touchedIndex : breakIndex));
      var entryLabel = selected.mitigated ? "entry after mitigation" : "planned mitigation fill";
      var breakX = Math.max(padL, x(breakIndex));
      [[selected.entry, "#dfe6f2", entryLabel, [], startX],
        [selected.stop, "#ff5c7a", "stop", [5, 4], breakX],
        [selected.target, "#2ee6a8", "4R target", [5, 4], startX],
        [selected.bos_level, "#b487ff", "body break", [2, 3], breakX]]
        .forEach(function (line) {
          var value = Number(line[0]); if (!isFinite(value)) return;
          var yy = y(value); if (yy < padT || yy > h - padB) return;
          var lineStart = line[4];
          ctx.strokeStyle = line[1]; ctx.lineWidth = 1; ctx.setLineDash(line[3]);
          ctx.beginPath(); ctx.moveTo(lineStart, yy); ctx.lineTo(w - padR, yy); ctx.stroke(); ctx.setLineDash([]);
          ctx.fillStyle = line[1]; ctx.textAlign = "left"; ctx.textBaseline = "bottom";
          ctx.fillText(line[2] + " " + priceText(value), lineStart + 4, yy - 2);
        });
    }

    // Last visible price and crosshair/OHLC.
    var last = candles[Math.min(i1, len - 1)];
    if (last) {
      var ly = y(last.c);
      if (ly >= padT && ly <= h - padB) {
        ctx.strokeStyle = "rgba(103,169,255,.45)"; ctx.beginPath(); ctx.moveTo(padL, ly); ctx.lineTo(w - padR, ly); ctx.stroke();
        ctx.fillStyle = "#67a9ff"; ctx.fillRect(w - padR + 2, ly - 8, padR - 4, 16);
        ctx.fillStyle = "#07101c"; ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillText(priceText(last.c), w - padR + 6, ly);
      }
    }

    var hoverBar = last;
    if (chartView.hover && chartView.hover.inside) {
      var hv = chartView.hover;
      ctx.strokeStyle = "rgba(200,215,240,.38)"; ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(hv.x, padT); ctx.lineTo(hv.x, h - padB); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(padL, hv.y); ctx.lineTo(w - padR, hv.y); ctx.stroke(); ctx.setLineDash([]);
      var hoverPrice = pHi - (hv.y - padT) / plotH * (pHi - pLo);
      ctx.fillStyle = "#162033"; ctx.fillRect(w - padR + 2, hv.y - 7, padR - 4, 14);
      ctx.fillStyle = "#c8d7f0"; ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.fillText(priceText(hoverPrice), w - padR + 5, hv.y);
      hoverBar = candles[hv.i] || last;
    }
    if (hoverBar) {
      var when = new Date(hoverBar.t).toISOString().slice(0, 16).replace("T", " ") + " UTC";
      $("chart-ohlc").innerHTML = escapeHtml(when) + " · O <b>" + escapeHtml(priceText(hoverBar.o))
        + "</b> H <b>" + escapeHtml(priceText(hoverBar.h)) + "</b> L <b>" + escapeHtml(priceText(hoverBar.l))
        + "</b> C <b class=\"" + (hoverBar.c >= hoverBar.o ? "pos" : "neg") + "\">" + escapeHtml(priceText(hoverBar.c)) + "</b>";
    }

    var audit = $("chart-audit");
    if (selected) {
      if (selected.chain_valid === false) {
        audit.className = "chart-audit bad";
        audit.textContent = "QUARANTINED · " + (selected.chain_errors || []).join(" · ");
      } else if (data.history_error || sweepIndex < 0 || breakIndex < 0) {
        audit.className = "chart-audit warn";
        audit.textContent = "Chain recorded, but its historical candle window is unavailable"
          + (data.history_error ? " · " + data.history_error : "");
      } else {
        audit.className = "chart-audit ok";
        audit.textContent = "✓ paired-leg H/L sweep → same-leg opposite body break + same-candle " + String(selected.hvb_side || "HVB").toUpperCase();
      }
    } else { audit.className = "chart-audit"; audit.textContent = "select a setup"; }
    $("chart-tf").textContent = candles.length + " historical bars"
      + (data.historical_window ? " · setup-centred" : " · latest")
      + (data.history_error ? " · history fallback" : "");
  }

  function chartCoords(ev) {
    var rect = $("chart").getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top, w: rect.width || 1, h: rect.height || 1 };
  }

  function attachChartInteractions() {
    var canvas = $("chart");
    canvas.addEventListener("wheel", function (ev) {
      if (!chartView.data || !(chartView.data.candles || []).length) return;
      ev.preventDefault();
      var p = chartCoords(ev);
      if (p.x > p.w - 76) chartView.zoomY = clamp(chartView.zoomY * (ev.deltaY > 0 ? 0.92 : 1.08), 0.25, 16);
      else {
        var old = chartView.span;
        chartView.span = clamp(Math.round(old * (ev.deltaY > 0 ? 1.18 : 0.84)), 20, chartView.data.candles.length);
        var frac = clamp((p.x - 8) / Math.max(1, p.w - 84), 0, 1);
        var anchor = chartView.right - old + 1 + frac * old;
        chartView.right = anchor + chartView.span - 1 - frac * chartView.span;
      }
      drawChart();
    }, { passive: false });
    canvas.addEventListener("mousedown", function (ev) {
      if (!chartView.data) return;
      var p = chartCoords(ev);
      chartView.drag = p.x > p.w - 76
        ? { mode: "scale", y: p.y, zoom: chartView.zoomY }
        : { mode: "pan", x: p.x, y: p.y, right: chartView.right, panY: chartView.panY };
      canvas.classList.toggle("panning", chartView.drag.mode === "pan");
      ev.preventDefault();
    });
    window.addEventListener("mousemove", function (ev) {
      if (!chartView.data || !(chartView.data.candles || []).length) return;
      var p = chartCoords(ev);
      if (chartView.drag) {
        if (chartView.drag.mode === "scale") chartView.zoomY = clamp(chartView.drag.zoom * (1 + (p.y - chartView.drag.y) / 220), 0.25, 16);
        else {
          chartView.right = chartView.drag.right - (p.x - chartView.drag.x) / Math.max(1, p.w - 84) * chartView.span;
          chartView.panY = chartView.drag.panY + (p.y - chartView.drag.y) / Math.max(1, p.h - 44) / Math.max(0.25, chartView.zoomY);
        }
        drawChart(); return;
      }
      if (p.x >= 0 && p.x < p.w - 76 && p.y >= 0 && p.y <= p.h) {
        var left = chartView.right - chartView.span + 1;
        var index = Math.round(left + (p.x - 8) / Math.max(1, p.w - 84) * chartView.span);
        chartView.hover = { x: p.x, y: p.y, i: clamp(index, 0, chartView.data.candles.length - 1), inside: true };
        drawChart();
      }
    });
    window.addEventListener("mouseup", function () { chartView.drag = null; canvas.classList.remove("panning"); });
    canvas.addEventListener("mouseleave", function () { if (!chartView.drag) { chartView.hover = null; drawChart(); } });
    canvas.addEventListener("dblclick", resetChartView);
    $("btn-chart-in").onclick = function () { if (!chartView.data) return; chartView.span = clamp(Math.round(chartView.span * 0.8), 20, (chartView.data.candles || []).length); drawChart(); };
    $("btn-chart-out").onclick = function () { if (!chartView.data) return; chartView.span = clamp(Math.round(chartView.span * 1.25), 20, (chartView.data.candles || []).length); drawChart(); };
    $("btn-chart-reset").onclick = resetChartView;
    $("btn-chart-full").onclick = function () {
      var panel = $("chart-panel");
      if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen();
      else if (panel.requestFullscreen) panel.requestFullscreen().catch(function () { panel.classList.toggle("fs"); });
      else panel.classList.toggle("fs");
      setTimeout(drawChart, 80);
    };
  }

  // ------------------------------------------------------- notifications :: ui
  function showToast(event) {
    var box = $("toast");
    clearTimeout(model.toastTimer);
    $("toast-title").textContent = event.title || "signal";
    $("toast-detail").textContent = NF.describe(event);
    box.hidden = false;
    box.dataset.key = event.key || "";
    model.toastTimer = setTimeout(function () { box.hidden = true; }, 7000);
  }

  function renderInbox() {
    var host = $("inbox-list");
    var events = toastQueue.list(80);
    if (!events.length) {
      host.innerHTML = '<div class="inbox-empty">Nothing yet. Only grade A signals and real errors arrive here — everything else is counted, not shown.</div>';
    } else {
      host.innerHTML = events.map(function (e) {
        var cls = "inbox-item " + (e.severity || "info");
        var when = e.time ? ago(e.time) + " ago" : "";
        var title = e.title || e.category || "";
        var detail = [e.detail, e.count > 1 ? "×" + e.count : ""].filter(Boolean).join(" · ");
        return '<div class="' + cls + '"><span class="bullet"></span><div><div class="t">' +
          escapeHtml(title) + (e.grade ? ' <span class="muted">· ' + escapeHtml(e.grade) + "</span>" : "") +
          '</div><div class="d">' + escapeHtml(detail) + '</div></div><div class="when">' + escapeHtml(when) + "</div></div>";
      }).join("");
    }
    var unread = toastQueue.unread;
    var badge = $("inbox-badge");
    badge.hidden = unread <= 0;
    badge.textContent = unread > 99 ? "99+" : String(unread);
    $("btn-inbox").classList.toggle("quiet", unread <= 0);
  }

  function pushEvent(event, wantToast) {
    if (!event) return;
    var result = toastQueue.push(event, { toast: !!wantToast });
    if (!result.event) return;
    renderInbox();
    if (result.toast) showToast(result.event);
  }

  function loadNotifications() {
    fetch("/api/notifications?limit=120")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        (data.events || []).slice().reverse().forEach(function (event) {
          // `toast:false` on the replay of history: the page was just opened,
          // it must not fire a toast for something that happened ten minutes ago
          pushEvent(event, false);
        });
      })
      .catch(function () {});
  }

  // ------------------------------------------------------------ state & poll
  function applyState(snapshot) {
    if (!snapshot) return;
    model.state = snapshot;
    var added = absorbRows(snapshot.signals, true);
    if (snapshot.archive) model.archive = snapshot.archive;
    renderHeader();
    renderTable();
    if (added) buildFacets();
    if (!model.selected) {
      var first = model.rows.get(model.preferredSelected) || sorted()[0];
      if (first) selectRow(first);
    } else {
      var row = model.rows.get(model.selected);
      if (row) {
        renderStory(row);
        if (model.chartKey !== row.key) loadChart(row, false);
      }
    }
  }

  function refreshArchive() {
    fetch("/api/signals?limit=800")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var added = absorbRows(data.signals, false);
        model.counts = data.counts || {};
        model.results = data.results || {};
        var rrs = (data.signals || []).map(function (r) { return Number(r.rr); })
          .filter(function (v) { return isFinite(v) && v > 0; }).sort(function (a, b) { return a - b; });
        if (rrs.length) model.medianRr = rrs[Math.floor(rrs.length / 2)].toFixed(1);
        renderStats();
        renderTable();
        if (added) buildFacets();
        if (!model.selected) {
          var first = model.rows.get(model.preferredSelected) || sorted()[0];
          if (first) selectRow(first);
        }
      })
      .catch(function () {});
  }

  function refreshHealth() {
    fetch("/api/health")
      .then(function (r) { return r.json(); })
      .then(function (data) { model.health = data; renderHeader(); })
      .catch(function () {});
  }

  var archiveTimer = 0;
  function soonArchive() {
    clearTimeout(archiveTimer);
    archiveTimer = setTimeout(refreshArchive, 1200);
  }

  // ------------------------------------------------------------------- alarm
  function banner(text) {
    var el = $("banner");
    if (!text) { el.hidden = true; el.textContent = ""; return; }
    if (el.hidden || el.textContent !== text) { el.textContent = text; el.hidden = false; }
  }

  function checkAlarm() {
    if (!model.state) return;
    var h = model.health || {};
    var age = model.lastMessage ? (Date.now() - model.lastMessage) / 1000 : null;
    if (h.feed === "stopped" || (h.failed && h.failed > 0 && h.symbols === 0)) {
      banner("The feed stopped. The page is still reading the archive, but no new candles are arriving. Check the server log.");
    } else if (!model.wsOk && age !== null && age > 20) {
      banner("Socket disconnected — falling back to HTTP polling, so the table may lag by a few seconds. It will reconnect by itself.");
    } else {
      banner("");
    }
  }

  // ---------------------------------------------------------------- websocket
  function connect() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + location.host + "/ws";
    var ws;
    var retry = 0;
    var pingTimer = 0;

    function open() {
      ws = new WebSocket(url);
      ws.onopen = function () {
        model.wsOk = true;
        retry = 0;
        model.lastMessage = Date.now();
        checkAlarm();
        clearInterval(pingTimer);
        pingTimer = setInterval(function () {
          try { ws.send("ping"); } catch (e) { /* the close handler takes over */ }
        }, 25000);
      };
      ws.onmessage = function (ev) {
        model.lastMessage = Date.now();
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (!msg || !msg.type) return;
        if (msg.type === "state") { applyState(msg); refreshHealth(); }
        else if (msg.type === "notify") {
          pushEvent(msg.event, msg.toast === true);
          if (msg.event && msg.event.category === "signal") soonArchive();
        } else if (msg.type === "setup") {
          soonArchive();
        }
        // `log`, `alert`, `filtered` are *only* ever handled by the server's
        // policy. They are never rendered here, which is what makes the page
        // incapable of cluttering itself.
      };
      ws.onclose = function () {
        model.wsOk = false;
        clearInterval(pingTimer);
        checkAlarm();
        retry = Math.min(retry + 1, 8);
        setTimeout(open, Math.min(1000 * Math.pow(1.7, retry), 20000));
      };
      ws.onerror = function () { try { ws.close(); } catch (e) {} };
    }
    open();
  }

  function poll() {
    fetch("/api/state")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        model.lastMessage = model.lastMessage || Date.now();
        applyState(data);
        refreshHealth();
      })
      .catch(function () { checkAlarm(); });
  }

  // -------------------------------------------------------------------- wiring
  function wire() {
    $("search").addEventListener("input", function (e) {
      model.filters.search = e.target.value.trim();
      savePreferences();
      renderTable();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && document.activeElement !== $("search")) {
        e.preventDefault();
        $("search").focus();
      }
      if (e.key === "Escape") {
        $("inbox").hidden = true;
        $("search").blur();
      }
    });

    Array.prototype.forEach.call($("seg-side").children, function (btn) {
      btn.onclick = function () {
        model.filters.side = btn.dataset.side;
        Array.prototype.forEach.call($("seg-side").children, function (b) { b.classList.toggle("on", b === btn); });
        savePreferences();
        renderTable();
      };
    });

    $("state-select").onchange = function (e) { model.filters.state = e.target.value; savePreferences(); renderTable(); };
    $("sort-select").onchange = function (e) { model.filters.sort = e.target.value; savePreferences(); renderTable(); };

    $("btn-reset").onclick = function () {
      model.filters = defaultFilters();
      $("search").value = "";
      $("state-select").value = "all";
      $("sort-select").value = "new";
      Array.prototype.forEach.call($("seg-side").children, function (b) { b.classList.toggle("on", b.dataset.side === "both"); });
      buildFacets();
      savePreferences();
      renderTable();
    };

    $("rows").addEventListener("click", function (e) {
      var tr = e.target.closest("tr[data-key]");
      if (!tr) return;
      var row = model.rows.get(tr.dataset.key);
      if (row) selectRow(row);
    });

    $("btn-refresh-chart").onclick = function () {
      var row = model.selected ? model.rows.get(model.selected) : null;
      if (row) loadChart(row, true);
      else refreshArchive();
    };

    $("btn-inbox").onclick = function () { $("inbox").hidden = !$("inbox").hidden; };
    $("btn-close-inbox").onclick = function () { $("inbox").hidden = true; };
    $("toast-open").onclick = function () { $("inbox").hidden = false; };
    $("btn-read").onclick = function () {
      toastQueue.markRead();
      renderInbox();
      fetch("/api/notifications/read", { method: "POST" }).catch(function () {});
    };

    var quietBtn = $("btn-quiet");
    function paintQuiet() {
      $("quiet-text").textContent = quiet ? "quiet on" : "quiet off";
      quietBtn.title = quiet
        ? "Quiet: at most one toast a minute, one on screen at a time. Everything else waits in the inbox."
        : "Loud: a toast for every actionable event, still one at a time with no duplicates. The inbox is still the record.";
    }
    paintQuiet();
    quietBtn.onclick = function () {
      quiet = !quiet;
      localStorage.setItem("tdigm.quiet", quiet ? "1" : "0");
      toastQueue.setMinGap(quiet ? 60_000 : 8_000);
      renderInbox();
      paintQuiet();
    };

    attachChartInteractions();
    var resizeTimer = 0;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(drawChart, 120);
    });
  }

  // --------------------------------------------------------------------- boot
  function paintSavedPreferences() {
    $("search").value = model.filters.search;
    $("state-select").value = model.filters.state;
    if ($("state-select").value !== model.filters.state) model.filters.state = "all";
    $("sort-select").value = model.filters.sort;
    if ($("sort-select").value !== model.filters.sort) model.filters.sort = "new";
    Array.prototype.forEach.call($("seg-side").children, function (b) {
      b.classList.toggle("on", b.dataset.side === model.filters.side);
    });
  }

  function boot() {
    paintSavedPreferences();
    wire();
    renderInbox();
    renderHeader();
    buildFacets();
    renderTable();
    refreshArchive();
    loadNotifications();
    refreshHealth();
    connect();
    poll();
    setInterval(poll, 6000);
    setInterval(refreshHealth, 8000);
    refreshWatch();
    setInterval(refreshWatch, 5000);
    setInterval(refreshArchive, 20000);
    setInterval(loadNotifications, 12000);
    setInterval(checkAlarm, 5000);
    setInterval(function () { if (!model.wsOk) checkAlarm(); }, 3000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
